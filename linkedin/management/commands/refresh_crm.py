"""Refresh the canonical lightweight CRM and publish safe Google Sheet views."""
from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import uuid
from contextlib import contextmanager
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from gspread.exceptions import APIError, WorksheetNotFound
from gspread.utils import ValueRenderOption


@contextmanager
def _suppress_google_api_request_logging():
    """Keep Gmail query URLs, which contain contact emails, out of CRM logs."""
    logger = logging.getLogger("googleapiclient.discovery")
    previous_level = logger.level
    logger.setLevel(max(previous_level, logging.WARNING))
    try:
        yield
    finally:
        logger.setLevel(previous_level)


class Command(BaseCommand):
    help = (
        "Refresh Gmail/meeting context, canonical Opportunities and Actions, "
        "then safely publish People, Opportunities, Pipeline, sender Followups, "
        "and Recovery. Defaults to a no-persistent-write dry-run."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Persist DB changes and publish the CRM workbook.",
        )
        parser.add_argument(
            "--skip-gmail-context",
            action="store_true",
            help="Use stored Gmail/Gemini context without refreshing it.",
        )
        parser.add_argument(
            "--skip-granola",
            action="store_true",
            help="Use cached context and Gemini fallback without calling Granola.",
        )
        parser.add_argument(
            "--skip-people",
            action="store_true",
            help="Do not run the narrow People publisher.",
        )
        parser.add_argument("--gmail-since-days", type=int, default=365)
        parser.add_argument("--granola-max-notes", type=int)
        parser.add_argument(
            "--backup-dir",
            default="artifacts/crm-backups",
            help="Gitignored directory for pre-write workbook backups.",
        )

    def handle(self, *args, **options):
        from linkedin.crm_lock import CrmRefreshAlreadyRunning, crm_refresh_lock

        apply = bool(options["apply"])
        try:
            with crm_refresh_lock():
                if apply:
                    # Keep every canonical DB mutation provisional until the
                    # final Sheets API call and verification succeeds.  Sheet
                    # writes are recoverable from the pre-write backup; this
                    # outer transaction guarantees a downstream API failure
                    # cannot advance DB merge baselines, Granola watermarks,
                    # Actions, or WorkflowRun telemetry.
                    with transaction.atomic():
                        report = self._refresh(options, dry_run=False)
                        # A blocked report is a failed apply even when every
                        # API call returned normally. Mark rollback *inside*
                        # the atomic block; raising CommandError afterward
                        # preserves the complete sanitized report while no DB
                        # imports/baselines/watermarks leak from a partial run.
                        if report.get("blocked"):
                            transaction.set_rollback(True)
                else:
                    # Exercise the real DB mutation path for an exact plan, but
                    # roll every canonical change back before returning.
                    with transaction.atomic():
                        report = self._refresh(options, dry_run=True)
                        transaction.set_rollback(True)
        except CrmRefreshAlreadyRunning as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(json.dumps(report, indent=2, sort_keys=True, default=str))
        if apply:
            if report.get("blocked"):
                raise CommandError(
                    "CRM refresh completed safe surfaces but one or more human "
                    "merge conflicts/invalid edits require review"
                )
            self.stdout.write(self.style.SUCCESS("CRM refresh applied and verified."))
        else:
            self.stdout.write(self.style.WARNING("Dry-run only: no persistent writes."))

    def _refresh(self, options, *, dry_run: bool) -> dict:
        from crm.models import (
            MeetingNote,
            MeetingNoteSyncState,
            Opportunity,
            OpportunityAction,
        )
        from linkedin.conf import (
            GOOGLE_SHEETS_ID,
            GRANOLA_API_BASE,
            GRANOLA_API_KEY,
            GRANOLA_HTTP_TIMEOUT_SECONDS,
        )
        from linkedin.crm_publish import build_crm_view_rows
        from linkedin.crm_service import bootstrap_opportunities, recalculate_actions
        from linkedin.crm_sheet_import import (
            apply_followup_imports,
            apply_opportunity_imports,
            baseline_by_opportunity_id,
            commit_followup_baselines,
            commit_sheet_baselines,
            read_people_dont_send_lead_ids,
        )
        from linkedin.exceptions import EnrichmentError, GranolaError, SheetsError
        from linkedin.granola import GranolaClient
        from linkedin.granola_sync import (
            GranolaSyncResult,
            rematch_cached_granola_notes,
            sync_granola_meeting_notes,
        )
        from linkedin.legacy_followup_migration import migrate_legacy_followup_tab
        from linkedin.models import WorkflowRun
        from linkedin.management.commands.sync_sheets import run_people_sync
        from linkedin.notifications import crm_sheets, sheets

        if options["gmail_since_days"] <= 0:
            raise CommandError("--gmail-since-days must be positive")
        if options.get("granola_max_notes") is not None and options["granola_max_notes"] <= 0:
            raise CommandError("--granola-max-notes must be positive")
        evaluated_at = timezone.now()

        spreadsheet = sheets._gspread_client()
        _verify_workbook_identity(
            spreadsheet,
            configured_id=GOOGLE_SHEETS_ID,
            require_sales_motion_guard=not dry_run,
        )
        stable_keys = _crm_stable_keys(crm_sheets=crm_sheets, sheets=sheets)
        before_inventory = _inventory_with_stable_keys(
            spreadsheet,
            crm_sheets=crm_sheets,
            stable_keys=stable_keys,
        )
        people_before = _capture_people_preservation_snapshot(
            spreadsheet,
            sheets=sheets,
        )
        report: dict = {
            "mode": "dry-run" if dry_run else "apply",
            "workbook": {
                "title": before_inventory["title"],
                "fingerprint": _fingerprint(GOOGLE_SHEETS_ID),
                "tabs_before": before_inventory["tab_count"],
            },
            "warnings": [],
            "inventory_before": _public_inventory(before_inventory),
        }

        if not options["skip_gmail_context"]:
            try:
                child_stdout = io.StringIO()
                child_stderr = io.StringIO()
                with _suppress_google_api_request_logging():
                    call_command(
                        "sync_gmail_context",
                        since_days=options["gmail_since_days"],
                        # The command-level dry-run is wrapped in a rollback-only
                        # transaction. Persist fetched context inside it so the
                        # downstream Sheet plans are exact, then roll it all back.
                        dry_run=False,
                        # Child output contains contact-level context; keep it out
                        # of routine refresh logs and expose only aggregate state.
                        stdout=child_stdout,
                        stderr=child_stderr,
                    )
                report["gmail_context"] = {
                    "status": "planned_refresh" if dry_run else "refreshed",
                    "warnings_suppressed": len(
                        [line for line in child_stderr.getvalue().splitlines() if line]
                    ),
                }
            except EnrichmentError as exc:
                warning = (
                    "Gmail context unavailable; stored context retained "
                    f"({type(exc).__name__})"
                )
                report["warnings"].append(warning)
                report["gmail_context"] = {"status": "unavailable"}
        else:
            report["gmail_context"] = {"status": "skipped"}

        explicit_lead_ids, explicit_stage_report = (
            _people_explicit_stage_lead_ids(spreadsheet, sheets=sheets)
        )
        report["people_explicit_stage_signals"] = explicit_stage_report

        # Apply inside the outer rollback-only transaction during a dry-run so
        # newly proposed UUIDs/actions participate in the exact Sheet plans.
        bootstrap = bootstrap_opportunities(
            apply=True,
            now=evaluated_at,
            explicit_lead_ids=explicit_lead_ids,
        )
        report["bootstrap"] = bootstrap.counts()

        active_ids = list(
            Opportunity.objects.exclude(
                stage__in=[Opportunity.Stage.CLOSED_WON, Opportunity.Stage.CLOSED_LOST],
            ).values_list("id", flat=True)
        )
        if options["skip_granola"]:
            cached_state = MeetingNoteSyncState.objects.filter(
                source=MeetingNote.Source.GRANOLA,
            ).first()
            granola = GranolaSyncResult(
                status="skipped",
                source_available=bool(
                    cached_state is not None
                    and cached_state.status in {
                        MeetingNoteSyncState.Status.SUCCESS,
                        MeetingNoteSyncState.Status.PARTIAL,
                    }
                ),
            )
        else:
            granola_client, granola_client_error = _build_granola_client(
                api_key=GRANOLA_API_KEY,
                base_url=GRANOLA_API_BASE,
                timeout=GRANOLA_HTTP_TIMEOUT_SECONDS,
                GranolaClient=GranolaClient,
                GranolaError=GranolaError,
            )
            granola = sync_granola_meeting_notes(
                client=granola_client,
                client_error=granola_client_error,
                now=evaluated_at,
                max_notes=options.get("granola_max_notes"),
                active_opportunity_ids=active_ids,
                # Exact dry-runs use the outer rollback-only transaction.
                dry_run=False,
            )
        report["granola"] = granola.counts()
        report["granola"]["raw_warnings_suppressed"] = len(granola.warnings)
        report["warnings"].extend(_sanitized_granola_warnings(granola))
        report["granola_rematch"] = rematch_cached_granola_notes(dry_run=False)

        backup_path = None
        if not dry_run:
            backup_path = crm_sheets.backup_spreadsheet(
                spreadsheet,
                Path(options["backup_dir"]),
                prefix="crm-before-refresh",
            )
            report["backup"] = str(backup_path)
        else:
            report["backup"] = "would create before structural writes"

        # Resolve and inspect canonical tabs without creating tabs or appending
        # headers.  Structural writes are forbidden until the incremental
        # People publisher has run and its preservation invariants pass.
        opportunity_ws, opportunity_tab = crm_sheets.ensure_managed_tab(
            spreadsheet,
            title=crm_sheets.OPPORTUNITIES_TAB,
            required_headers=crm_sheets.OPPORTUNITY_HEADERS,
            dry_run=True,
        )
        pipeline_ws, pipeline_tab = crm_sheets.ensure_managed_tab(
            spreadsheet,
            title=crm_sheets.PIPELINE_TAB,
            required_headers=crm_sheets.PIPELINE_HEADERS,
            dry_run=True,
        )
        recovery_ws, recovery_tab = crm_sheets.ensure_managed_tab(
            spreadsheet,
            title=crm_sheets.RECOVERY_TAB,
            required_headers=crm_sheets.RECOVERY_HEADERS,
            dry_run=True,
        )
        report["managed_tabs"] = {
            item.title: {
                "exists": item.exists,
                "would_create": item.would_create,
                "header_additions": len(item.header_additions),
            }
            for item in (opportunity_tab, pipeline_tab, recovery_tab)
        }

        _assert_people_dnc_headers(people_before, sheets=sheets)
        dont_send_lead_ids = read_people_dont_send_lead_ids(spreadsheet)
        report["people_dont_send_leads"] = len(dont_send_lead_ids)
        preliminary_actions = recalculate_actions(
            apply=False,
            now=evaluated_at,
            dont_send_lead_ids=dont_send_lead_ids,
            granola_available=granola.source_available,
        )
        preliminary_rows = build_crm_view_rows(
            preliminary_actions,
            granola_available=granola.source_available,
            synced_at=evaluated_at,
        )

        opportunity_import_report = None
        opportunity_blocked = False
        opportunity_identity_blockers = 0
        if opportunity_ws is not None:
            initial_opportunity_plan = crm_sheets.OpportunitySheetAdapter(
                opportunity_ws,
            ).plan(
                preliminary_rows.opportunities,
                baseline_by_id=baseline_by_opportunity_id(),
            )
            opportunity_identity_blockers = _opportunity_identity_blocker_count(
                initial_opportunity_plan
            )
            if (
                initial_opportunity_plan.conflicts
                or opportunity_identity_blockers
            ):
                opportunity_import_report = apply_opportunity_imports(
                    (),
                    dry_run=False,
                    now=evaluated_at,
                )
            else:
                opportunity_import_report = apply_opportunity_imports(
                    initial_opportunity_plan.imports,
                    dry_run=False,
                    now=evaluated_at,
                )
            opportunity_blocked = bool(
                initial_opportunity_plan.conflicts
                or opportunity_identity_blockers
                or opportunity_import_report.invalid
            )
            report["opportunity_import_plan"] = initial_opportunity_plan.summary()
            report["opportunity_import"] = opportunity_import_report.counts()
        else:
            report["opportunity_import_plan"] = {
                "title": crm_sheets.OPPORTUNITIES_TAB,
                "appended": len(preliminary_rows.opportunities),
                "imports": 0,
                "conflicts": 0,
            }
            report["opportunity_import"] = {"opportunities_updated": 0, "invalid": 0}
        report["opportunity_identity_blockers"] = opportunity_identity_blockers

        followup_tabs: dict[str, object] = {}
        legacy_followup_owners: set[str] = set()
        for owner in ("Arian", "Athena", "Chuka", "Leili"):
            title = crm_sheets.sender_followups_tab(owner)
            try:
                ws = spreadsheet.worksheet(title)
            except WorksheetNotFound:
                ws = None
            except APIError as exc:
                raise SheetsError(f"failed resolving {title}: {exc}") from exc
            if ws is None:
                followup_tabs[owner] = None
                continue
            headers = [str(value).strip() for value in ws.row_values(1)]
            if crm_sheets.COL_ACTION_ID not in headers:
                legacy_followup_owners.add(owner)
                followup_tabs[owner] = ws
                continue
            followup_tabs[owner] = ws

        # Followup human cells use the same two-pass merge as Opportunities:
        # plan against the last published baseline, import only conflict-free
        # edits, then reserialize/re-plan before any Sheet write.
        initial_followup_plans = {}
        initial_followup_plan_objects = {}
        followup_conflicts = []
        followup_identity_blockers = 0
        followup_import_reports = {}
        initial_linked_owner_blockers: dict[str, int] = {}
        owner_followup_state = {
            owner: {
                "initial_conflicts": 0,
                "invalid_imports": 0,
                "initial_identity_blockers": 0,
                "fresh_conflicts": 0,
                "fresh_imports_remaining": 0,
                "fresh_identity_blockers": 0,
                "publication_conflicts": 0,
                "publication_imports_remaining": 0,
                "publication_identity_blockers": 0,
            }
            for owner in ("Arian", "Athena", "Chuka", "Leili")
        }
        for owner, ws in followup_tabs.items():
            if ws is None or owner in legacy_followup_owners:
                continue
            desired, baselines, retained_telemetry, plan = _sender_followup_plan(
                owner=owner,
                ws=ws,
                due_rows=preliminary_rows.followups_by_owner.get(owner, ()),
                crm_sheets=crm_sheets,
                OpportunityAction=OpportunityAction,
            )
            retained_telemetry.pop("_safe_retire_action_ids", None)
            retained_telemetry.pop("_local_validation_blockers", None)
            linked_blocked_owners = retained_telemetry.pop(
                "_linked_blocked_owners",
                {},
            )
            for linked_owner, count in linked_blocked_owners.items():
                initial_linked_owner_blockers[linked_owner] = (
                    initial_linked_owner_blockers.get(linked_owner, 0)
                    + int(count or 0)
                )
            plan_summary = (
                plan.summary()
                if plan is not None
                else {
                    "title": str(getattr(ws, "title", "")),
                    "key_header": crm_sheets.COL_ACTION_ID,
                    "blocked": True,
                    "local_validation_blockers": (
                        retained_telemetry.get(
                            "duplicate_action_id_rows",
                            0,
                        )
                        + retained_telemetry.get(
                            "unkeyed_nonempty_action_rows",
                            0,
                        )
                        + retained_telemetry.get(
                            "malformed_baseline_action_rows",
                            0,
                        )
                        + retained_telemetry.get(
                            "local_validation_error_rows",
                            0,
                        )
                    ),
                }
            )
            initial_followup_plans[owner] = {
                **plan_summary,
                **retained_telemetry,
            }
            initial_followup_plan_objects[owner] = plan
            conflicts = plan.conflicts if plan is not None else ()
            followup_conflicts.extend(conflicts)
            identity_blockers = _followup_identity_blocker_count(
                retained_telemetry
            )
            followup_identity_blockers += identity_blockers
            owner_followup_state[owner]["initial_conflicts"] = len(conflicts)
            owner_followup_state[owner]["initial_identity_blockers"] = (
                identity_blockers
            )
        _propagate_linked_owner_blockers(
            owner_followup_state,
            initial_linked_owner_blockers,
            field="initial_identity_blockers",
        )
        for owner, plan in initial_followup_plan_objects.items():
            state = owner_followup_state[owner]
            imports = (
                plan.imports
                if (
                    plan is not None
                    and not state["initial_conflicts"]
                    and not state["initial_identity_blockers"]
                )
                else ()
            )
            import_report = apply_followup_imports(
                imports,
                dry_run=False,
                now=evaluated_at,
            )
            followup_import_reports[owner] = import_report
            owner_followup_state[owner]["invalid_imports"] = len(
                import_report.invalid
            )
        followup_import_counts = _sum_followup_import_reports(
            followup_import_reports.values(),
        )
        report["followup_import"] = followup_import_counts
        report["followup_import_plans"] = initial_followup_plans
        report["followup_conflicts"] = len(followup_conflicts)
        report["followup_identity_blockers"] = followup_identity_blockers

        initial_action_report = recalculate_actions(
            apply=True,
            now=evaluated_at,
            dont_send_lead_ids=dont_send_lead_ids,
            granola_available=granola.source_available,
        )
        rows = build_crm_view_rows(
            initial_action_report,
            granola_available=granola.source_available,
            synced_at=evaluated_at,
        )

        # A legacy sender tab can only be replaced after every material row is
        # either imported by stable identity or explicitly reported. The
        # importer never mutates the worksheet and never sends a message.
        legacy_reports: dict[str, dict] = {}
        legacy_review_owners: set[str] = set()
        for owner in sorted(legacy_followup_owners):
            migration = migrate_legacy_followup_tab(
                followup_tabs[owner],
                owner=owner,
                # Do not constrain identity to the new due-now view: a legacy
                # human draft may belong to a uniquely owned waiting/recovery
                # action that should be preserved in DB but intentionally not
                # republished in today's queue. The importer still requires a
                # unique current Action for the stable Lead and explicit owner.
                desired_rows=None,
                # Exact dry-runs exercise these DB writes inside handle()'s
                # rollback-only transaction.
                dry_run=False,
            )
            migration_counts = migration.counts()
            migration_counts["outer_transaction_rollback"] = dry_run
            migration_counts["review_required"] = bool(
                migration.material_rows_skipped
            )
            legacy_reports[owner] = migration_counts
            if migration.material_rows_skipped:
                legacy_review_owners.add(owner)
        report["legacy_followup_migrations"] = legacy_reports
        report["legacy_followup_review_required"] = {
            "owners": len(legacy_review_owners),
            "material_rows": sum(
                int(item.get("material_rows_skipped", 0) or 0)
                for item in legacy_reports.values()
            ),
            "preservation_policy": (
                "complete source tabs archived without deletion on successful "
                "atomic cutover"
            ),
        }

        # Imported Sent/Disqualify state can remove an action, while an
        # imported draft must be reflected in the replacement tab. Recalculate
        # once more from canonical state before any publication.
        final_action_report = initial_action_report
        if legacy_reports:
            final_action_report = recalculate_actions(
                apply=True,
                now=evaluated_at,
                dont_send_lead_ids=dont_send_lead_ids,
                granola_available=granola.source_available,
            )
            rows = build_crm_view_rows(
                final_action_report,
                granola_available=granola.source_available,
                synced_at=evaluated_at,
            )
        report["actions"] = _action_counts_for_run(
            initial_action_report,
            final_action_report if legacy_reports else None,
        )

        # Re-plan every canonical sender tab from the freshly recalculated DB,
        # including stable-ID rows that are no longer due today.  Any import
        # still present means the first pass did not round-trip cleanly, so no
        # sender tab is regenerated.  Baselines from these verified plans are
        # committed only after the corresponding Sheet publication succeeds.
        fresh_followup_plans = {}
        fresh_followup_conflicts = 0
        fresh_followup_imports = 0
        fresh_followup_identity_blockers = 0
        fresh_linked_owner_blockers: dict[str, int] = {}
        for owner, ws in followup_tabs.items():
            if ws is None or owner in legacy_followup_owners:
                continue
            desired, baselines, retained_telemetry, plan = _sender_followup_plan(
                owner=owner,
                ws=ws,
                due_rows=rows.followups_by_owner.get(owner, ()),
                crm_sheets=crm_sheets,
                OpportunityAction=OpportunityAction,
            )
            retained_telemetry.pop("_safe_retire_action_ids", None)
            retained_telemetry.pop("_local_validation_blockers", None)
            linked_blocked_owners = retained_telemetry.pop(
                "_linked_blocked_owners",
                {},
            )
            for linked_owner, count in linked_blocked_owners.items():
                fresh_linked_owner_blockers[linked_owner] = (
                    fresh_linked_owner_blockers.get(linked_owner, 0)
                    + int(count or 0)
                )
            plan_summary = (
                plan.summary()
                if plan is not None
                else {
                    "title": str(getattr(ws, "title", "")),
                    "key_header": crm_sheets.COL_ACTION_ID,
                    "blocked": True,
                }
            )
            fresh_followup_plans[owner] = {
                **plan_summary,
                **retained_telemetry,
            }
            conflicts = plan.conflicts if plan is not None else ()
            imports = plan.imports if plan is not None else ()
            fresh_followup_conflicts += len(conflicts)
            fresh_followup_imports += len(imports)
            identity_blockers = _followup_identity_blocker_count(
                retained_telemetry
            )
            fresh_followup_identity_blockers += identity_blockers
            owner_followup_state[owner]["fresh_conflicts"] = len(conflicts)
            owner_followup_state[owner]["fresh_imports_remaining"] = len(
                imports
            )
            owner_followup_state[owner]["fresh_identity_blockers"] = (
                identity_blockers
            )
        _propagate_linked_owner_blockers(
            owner_followup_state,
            fresh_linked_owner_blockers,
            field="fresh_identity_blockers",
        )
        canonical_blocked_owners = _blocked_followup_owners(
            owner_followup_state,
        )
        followups_blocked = bool(canonical_blocked_owners)
        report["followup_fresh_replan"] = fresh_followup_plans
        report["followup_fresh_conflicts"] = fresh_followup_conflicts
        report["followup_fresh_imports"] = fresh_followup_imports
        report["followup_fresh_identity_blockers"] = (
            fresh_followup_identity_blockers
        )

        people_report, managed, people_gate_blocked = (
            _people_gate_then_activate_managed_tabs(
                spreadsheet=spreadsheet,
                people_before=people_before,
                skip_people=options["skip_people"],
                dry_run=dry_run,
                run_people_sync=run_people_sync,
                crm_sheets=crm_sheets,
                sheets=sheets,
            )
        )
        report["people"] = people_report
        report["people_gate_blocked"] = people_gate_blocked
        (
            (opportunity_ws, opportunity_tab),
            (pipeline_ws, pipeline_tab),
            (recovery_ws, recovery_tab),
        ) = managed
        report["managed_tabs"] = {
            item.title: {
                "exists": item.exists,
                "would_create": item.would_create,
                "header_additions": len(item.header_additions),
            }
            for item in (opportunity_tab, pipeline_tab, recovery_tab)
        }

        fresh_opportunity_identity_blockers = 0
        if opportunity_ws is None:
            report["opportunities"] = {
                "title": crm_sheets.OPPORTUNITIES_TAB,
                "appended": len(rows.opportunities),
                "would_create": True,
                "blocked": opportunity_blocked,
            }
        elif opportunity_blocked:
            report["opportunities"] = {
                "blocked": True,
                "reason": (
                    "invalid human edit, missing stable identity, or "
                    "three-way conflict"
                ),
            }
        else:
            adapter = crm_sheets.OpportunitySheetAdapter(opportunity_ws)
            final_plan = adapter.plan(
                rows.opportunities,
                baseline_by_id=baseline_by_opportunity_id(),
            )
            fresh_opportunity_identity_blockers = (
                _opportunity_identity_blocker_count(final_plan)
            )
            if (
                final_plan.conflicts
                or final_plan.imports
                or fresh_opportunity_identity_blockers
            ):
                opportunity_blocked = True
                report["opportunities"] = {
                    **final_plan.summary(),
                    "blocked": True,
                    "identity_blockers": fresh_opportunity_identity_blockers,
                    "reason": (
                        "human edits or missing stable identities remained "
                        "after fresh re-plan"
                    ),
                }
            else:
                report["opportunities"] = adapter.apply(
                    final_plan,
                    dry_run=dry_run,
                )
                if not dry_run:
                    commit_sheet_baselines(final_plan.baseline_updates)
        report["opportunity_fresh_identity_blockers"] = (
            fresh_opportunity_identity_blockers
        )

        if pipeline_ws is None:
            report["pipeline"] = {
                "title": crm_sheets.PIPELINE_TAB,
                "appended": len(rows.pipeline),
                "would_create": True,
            }
        else:
            pipeline_plan = crm_sheets.pipeline_adapter(pipeline_ws).plan(rows.pipeline)
            report["pipeline"] = crm_sheets.pipeline_adapter(pipeline_ws).apply(
                pipeline_plan,
                dry_run=dry_run,
            )

        if recovery_ws is None:
            report["recovery"] = {
                "title": crm_sheets.RECOVERY_TAB,
                "appended": len(rows.recovery),
                "would_create": True,
            }
        else:
            recovery_plan = crm_sheets.recovery_adapter(recovery_ws).plan(rows.recovery)
            report["recovery"] = crm_sheets.recovery_adapter(recovery_ws).apply(
                recovery_plan,
                dry_run=dry_run,
            )

        # Final no-write sender preflight happens after every other surface and
        # before the first Followups mutation. Each adapter plan is bracketed
        # by identical full snapshots; retirement clears are attached only to
        # that stable, revalidated snapshot. This closes the window where an
        # operator edit could arrive after the earlier merge pass.
        publication_followup_plan_objects = {}
        publication_followup_plans = {}
        publication_followup_conflicts = 0
        publication_followup_imports = 0
        publication_followup_identity_blockers = 0
        publication_linked_owner_blockers: dict[str, int] = {}
        for owner, ws in followup_tabs.items():
            if ws is None or owner in legacy_followup_owners:
                continue
            desired = rows.followups_by_owner.get(owner, ())
            (
                _publication_desired,
                publication_telemetry,
                publication_plan,
            ) = _stable_sender_publication_plan(
                owner=owner,
                ws=ws,
                due_rows=desired,
                crm_sheets=crm_sheets,
                OpportunityAction=OpportunityAction,
            )
            publication_telemetry.pop("_safe_retire_action_ids", None)
            publication_telemetry.pop("_local_validation_blockers", None)
            linked = publication_telemetry.pop(
                "_linked_blocked_owners",
                {},
            )
            for linked_owner, count in linked.items():
                publication_linked_owner_blockers[linked_owner] = (
                    publication_linked_owner_blockers.get(linked_owner, 0)
                    + int(count or 0)
                )
            conflicts = (
                publication_plan.conflicts
                if publication_plan is not None
                else ()
            )
            imports = (
                publication_plan.imports
                if publication_plan is not None
                else ()
            )
            identity_blockers = _followup_identity_blocker_count(
                publication_telemetry
            )
            owner_followup_state[owner]["publication_conflicts"] = len(
                conflicts
            )
            owner_followup_state[owner][
                "publication_imports_remaining"
            ] = len(imports)
            owner_followup_state[owner][
                "publication_identity_blockers"
            ] = identity_blockers
            publication_followup_conflicts += len(conflicts)
            publication_followup_imports += len(imports)
            publication_followup_identity_blockers += identity_blockers
            if publication_plan is not None:
                publication_followup_plan_objects[owner] = publication_plan
            publication_followup_plans[owner] = {
                **(
                    publication_plan.summary()
                    if publication_plan is not None
                    else {
                        "title": str(getattr(ws, "title", "")),
                        "key_header": crm_sheets.COL_ACTION_ID,
                        "blocked": True,
                    }
                ),
                **publication_telemetry,
            }
        _propagate_linked_owner_blockers(
            owner_followup_state,
            publication_linked_owner_blockers,
            field="publication_identity_blockers",
        )
        canonical_blocked_owners = _blocked_followup_owners(
            owner_followup_state,
        )
        followups_blocked = bool(canonical_blocked_owners)
        report["followup_publication_preflight"] = publication_followup_plans
        report["followup_publication_conflicts"] = (
            publication_followup_conflicts
        )
        report["followup_publication_imports"] = publication_followup_imports
        report["followup_publication_identity_blockers"] = (
            publication_followup_identity_blockers
        )

        followup_report = {}
        for owner in ("Arian", "Athena", "Chuka", "Leili"):
            if owner in legacy_followup_owners:
                continue
            if owner in canonical_blocked_owners:
                followup_report[owner] = {
                    "title": crm_sheets.sender_followups_tab(owner),
                    "blocked": True,
                    "reason": "sender-scoped human merge requires review",
                    **owner_followup_state[owner],
                }
                continue
            desired = rows.followups_by_owner.get(owner, ())
            published = self._publish_followup_tab(
                spreadsheet,
                owner=owner,
                desired_rows=desired,
                existing_ws=followup_tabs.get(owner),
                legacy=(owner in legacy_followup_owners),
                retire_action_ids=(),
                precomputed_plan=publication_followup_plan_objects.get(owner),
                dry_run=dry_run,
                crm_sheets=crm_sheets,
                OpportunityAction=OpportunityAction,
                commit_followup_baselines=commit_followup_baselines,
            )
            plan_telemetry = publication_followup_plans.get(owner, {})
            published["due_now_rows"] = len(desired)
            published["retained_canonical_rows_considered"] = (
                plan_telemetry.get("retained_canonical_rows_considered", 0)
            )
            followup_report[owner] = published

        # A first-run legacy worksheet has no Action IDs, so unresolved rows
        # cannot safely enter the canonical queue.  They also no longer need
        # to freeze the CRM: the complete source tabs become dated review
        # archives while fully built replacements take every canonical sender
        # title in one all-or-nothing Sheets batch.
        legacy_owners_implicated_by_canonical = (
            set(legacy_followup_owners) & canonical_blocked_owners
        )
        legacy_archive_reports, legacy_archive_blocked_owners = (
            _publish_legacy_followup_tabs_atomically(
                spreadsheet,
                legacy_worksheets={
                    owner: followup_tabs[owner]
                    for owner in legacy_followup_owners
                },
                desired_rows_by_owner={
                    owner: rows.followups_by_owner.get(owner, ())
                    for owner in legacy_followup_owners
                },
                legacy_reports=legacy_reports,
                dry_run=dry_run,
                crm_sheets=crm_sheets,
                OpportunityAction=OpportunityAction,
                commit_followup_baselines=commit_followup_baselines,
                canonical_blocked_owners=canonical_blocked_owners,
                now=evaluated_at,
            )
        )
        followup_report.update(legacy_archive_reports)
        followups_blocked = bool(
            canonical_blocked_owners or legacy_archive_blocked_owners
        )
        if followups_blocked:
            blocked_summary = _followup_block_summary(
                initial_conflicts=len(followup_conflicts),
                invalid_imports=followup_import_counts["invalid"],
                initial_identity_blockers=followup_identity_blockers,
                fresh_conflicts=fresh_followup_conflicts,
                fresh_imports_remaining=fresh_followup_imports,
                fresh_identity_blockers=fresh_followup_identity_blockers,
                publication_conflicts=publication_followup_conflicts,
                publication_imports_remaining=publication_followup_imports,
                publication_identity_blockers=(
                    publication_followup_identity_blockers
                ),
                owners_blocked=len(
                    canonical_blocked_owners | legacy_archive_blocked_owners
                ),
            )
            if legacy_archive_blocked_owners:
                if legacy_owners_implicated_by_canonical:
                    blocked_summary[
                        "legacy_cohort_canonical_conflicts"
                    ] = len(legacy_owners_implicated_by_canonical)
                    blocked_summary["reason"] = (
                        "canonical foreign-material conflict implicates a "
                        "legacy destination; atomic legacy cohort was not built"
                    )
                else:
                    blocked_summary["legacy_atomic_archive_failures"] = len(
                        legacy_archive_blocked_owners
                    )
                    blocked_summary["reason"] = (
                        "canonical human merge conflict or atomic legacy "
                        "archive failure"
                    )
            followup_report["blocked"] = blocked_summary
        report["followups"] = followup_report
        followup_publish_blocked = any(
            isinstance(item, dict) and item.get("blocked")
            for item in followup_report.values()
        )
        report["blocked"] = bool(
            people_gate_blocked
            or opportunity_blocked
            or followups_blocked
            or followup_publish_blocked
        )

        after_inventory = (
            before_inventory
            if dry_run
            else _inventory_with_stable_keys(
                spreadsheet,
                crm_sheets=crm_sheets,
                stable_keys=stable_keys,
            )
        )
        report["workbook"]["tabs_after"] = (
            before_inventory["tab_count"]
            if dry_run
            else after_inventory["tab_count"]
        )
        if not dry_run:
            report["inventory_after"] = _public_inventory(after_inventory)
        if not dry_run:
            workflow_counts = {
                "blocked": report["blocked"],
                "bootstrap": bootstrap.counts(),
                "people_explicit_stage_signals": explicit_stage_report,
                "actions": report["actions"],
                "people": report["people"],
                "opportunities": report["opportunities"],
                "opportunity_import": report["opportunity_import"],
                "opportunity_merge": {
                    "imports": report["opportunity_import_plan"].get("imports", 0),
                    "conflicts": report["opportunity_import_plan"].get(
                        "conflicts",
                        0,
                    ),
                    "identity_blockers": opportunity_identity_blockers,
                    "fresh_identity_blockers": (
                        fresh_opportunity_identity_blockers
                    ),
                    "invalid": report["opportunity_import"].get("invalid", 0),
                },
                "pipeline": report["pipeline"],
                "recovery": report["recovery"],
                "followups": followup_report,
                "followup_import": report["followup_import"],
                "followup_merge": {
                    "initial_conflicts": len(followup_conflicts),
                    "initial_identity_blockers": followup_identity_blockers,
                    "fresh_conflicts": fresh_followup_conflicts,
                    "fresh_imports_remaining": fresh_followup_imports,
                    "fresh_identity_blockers": fresh_followup_identity_blockers,
                    "publication_conflicts": publication_followup_conflicts,
                    "publication_imports_remaining": (
                        publication_followup_imports
                    ),
                    "publication_identity_blockers": (
                        publication_followup_identity_blockers
                    ),
                },
            }
            WorkflowRun.objects.create(
                name="refresh-crm",
                operator="",
                summary=(
                    "Canonical CRM refresh completed with blocked human merges"
                    if report["blocked"]
                    else "Canonical CRM context/import/recalculate/publish completed"
                ),
                counts=workflow_counts,
            )
        return report

    def _publish_followup_tab(
        self,
        spreadsheet,
        *,
        owner,
        desired_rows,
        existing_ws,
        legacy,
        retire_action_ids,
        precomputed_plan,
        dry_run,
        crm_sheets,
        OpportunityAction,
        commit_followup_baselines,
    ):
        title = crm_sheets.sender_followups_tab(owner)
        if dry_run and (existing_ws is None or legacy):
            return {
                "title": title,
                "appended": len(desired_rows),
                "would_create": existing_ws is None,
                "would_preserve_legacy": legacy,
            }

        if existing_ws is None:
            ws, _tab = crm_sheets.ensure_managed_tab(
                spreadsheet,
                title=title,
                required_headers=crm_sheets.FOLLOWUP_HEADERS,
                dry_run=False,
            )
        elif legacy:
            stamp = timezone.now().strftime("%Y%m%dT%H%M%S")
            temp_title = _unique_tab_title(
                spreadsheet,
                f"CRM {owner} Followups {stamp}",
            )
            ws = spreadsheet.add_worksheet(
                title=temp_title,
                rows=max(100, len(desired_rows) + 20),
                cols=len(crm_sheets.FOLLOWUP_HEADERS),
            )
            crm_sheets.ensure_additive_headers(ws, crm_sheets.FOLLOWUP_HEADERS)
        else:
            ws = existing_ws

        actions = OpportunityAction.objects.filter(
            pk__in=[row[crm_sheets.COL_ACTION_ID] for row in desired_rows],
        )
        baseline = {
            str(action.id): dict(action.sheet_human_snapshot or {})
            for action in actions
        }
        adapter = crm_sheets.followups_adapter(ws)
        plan = precomputed_plan
        if plan is None:
            plan = adapter.plan(desired_rows, baseline_by_id=baseline)
            _retire_safe_followup_rows(
                plan,
                ws=ws,
                action_ids=retire_action_ids,
                crm_sheets=crm_sheets,
            )
        if plan.conflicts or plan.imports:
            return {
                **plan.summary(),
                "blocked": True,
                "reason": "human edits remained after fresh re-plan",
            }
        summary = adapter.apply(plan, dry_run=dry_run)

        if dry_run:
            return summary

        if legacy:
            legacy_title = _unique_tab_title(
                spreadsheet,
                f"{owner} - Followups Legacy {timezone.now():%Y%m%d}",
            )
            _swap_worksheet_titles(
                spreadsheet,
                existing_ws=existing_ws,
                replacement_ws=ws,
                legacy_title=legacy_title,
                canonical_title=title,
            )
            summary["legacy_tab"] = legacy_title
        commit_followup_baselines(plan.baseline_updates)
        return summary


def _publish_legacy_followup_tabs_atomically(
    spreadsheet,
    *,
    legacy_worksheets,
    desired_rows_by_owner,
    legacy_reports,
    dry_run: bool,
    crm_sheets,
    OpportunityAction,
    commit_followup_baselines,
    canonical_blocked_owners=(),
    now=None,
) -> tuple[dict[str, dict], set[str]]:
    """Replace all first-run legacy sender tabs as one atomic cutover.

    The source worksheets are never edited.  On apply, every replacement is
    created under a temporary title, populated, and read back through the same
    stable-ID publication preflight used for ordinary sender tabs.  Only after
    *all* replacements pass does one Sheets ``batchUpdate`` archive all source
    tabs and activate all replacements.  Baselines are committed after that
    atomic title swap, never before it.

    Unresolved legacy material is deliberately not copied or guessed.  The
    complete source worksheet becomes the dated review archive, while the new
    canonical tab contains only canonical Action-ID rows.
    """
    from linkedin.exceptions import SheetsError

    owner_order = ("Arian", "Athena", "Chuka", "Leili")
    owners = tuple(
        owner for owner in owner_order if owner in legacy_worksheets
    ) + tuple(
        sorted(set(legacy_worksheets) - set(owner_order))
    )
    if not owners:
        return {}, set()

    implicated_legacy_owners = set(owners) & set(canonical_blocked_owners)
    if implicated_legacy_owners:
        # A material row on a canonical old-owner tab can point at an Action
        # now owned by a legacy destination.  Activating that destination while
        # the old row remains blocked would expose the same Action on two
        # sender surfaces.  The legacy cohort is all-or-nothing, so stop before
        # even inventorying titles or creating a temporary worksheet.
        reports = {}
        for owner in owners:
            migration = dict(legacy_reports.get(owner, {}) or {})
            material_rows = int(
                migration.get("material_rows_skipped", 0) or 0
            )
            reports[owner] = {
                "title": crm_sheets.sender_followups_tab(owner),
                "atomic_archive": True,
                "blocked": True,
                "status": "blocked_by_canonical_foreign_material",
                "failure_phase": "canonical_conflict_preflight",
                "reason": (
                    "legacy cohort includes a destination implicated by a "
                    "material canonical sender conflict; no replacement was built"
                ),
                "legacy_preserved": True,
                "legacy_source_untouched": True,
                "replacement_built": False,
                "title_swap_attempted": False,
                "baseline_committed": False,
                "implicated_legacy_owners": len(implicated_legacy_owners),
                "review_required": bool(material_rows),
                "material_rows_skipped": material_rows,
                "material_skip_reasons": dict(
                    migration.get("material_skip_reasons", {}) or {}
                ),
                "due_now_rows": len(desired_rows_by_owner.get(owner, ())),
            }
        return reports, set(owners)

    current_time = now or timezone.now()
    phase = "preflight"
    replacements: dict[str, object] = {}
    replacement_plans: dict[str, object] = {}
    reports: dict[str, dict] = {}
    archive_titles: dict[str, str] = {}
    temp_titles: dict[str, str] = {}

    try:
        reserved_titles = {
            str(worksheet.title)
            for worksheet in crm_sheets.retry_sheet_read(
                spreadsheet.worksheets,
                context=(
                    "failed inventorying titles for atomic legacy archive"
                ),
            )
        }

        source_ids: set[int] = set()
        for owner in owners:
            source = legacy_worksheets[owner]
            canonical_title = crm_sheets.sender_followups_tab(owner)
            if str(getattr(source, "title", "")) != canonical_title:
                raise SheetsError(
                    "legacy followup source moved after discovery"
                )
            source_id = getattr(source, "id", None)
            if source_id is None or source_id in source_ids:
                raise SheetsError(
                    "legacy followup sources lack unique worksheet IDs"
                )
            source_ids.add(source_id)
            archive_titles[owner] = _reserve_unique_tab_title(
                reserved_titles,
                f"{owner} - Followups Legacy {current_time:%Y%m%d}",
            )
            temp_titles[owner] = _reserve_unique_tab_title(
                reserved_titles,
                f"CRM {owner} Followups {current_time:%Y%m%dT%H%M%S} "
                f"{uuid.uuid4().hex[:8]}",
            )

        _validate_legacy_replacement_payloads(
            owners=owners,
            desired_rows_by_owner=desired_rows_by_owner,
            crm_sheets=crm_sheets,
            OpportunityAction=OpportunityAction,
        )

        for owner in owners:
            migration = dict(legacy_reports.get(owner, {}))
            material_rows = int(migration.get("material_rows_skipped", 0) or 0)
            reports[owner] = {
                "title": crm_sheets.sender_followups_tab(owner),
                "archive_title": archive_titles[owner],
                "atomic_archive": True,
                "legacy_preserved": True,
                "review_required": bool(material_rows),
                "material_rows_skipped": material_rows,
                "material_skip_reasons": dict(
                    migration.get("material_skip_reasons", {}) or {}
                ),
                "due_now_rows": len(desired_rows_by_owner.get(owner, ())),
            }

        if dry_run:
            for owner in owners:
                reports[owner].update({
                    "status": "planned_atomic_archive",
                    "blocked": False,
                    "would_build_replacement": True,
                    "would_archive_legacy": True,
                    "would_activate_replacement": True,
                    "outer_transaction_rollback": True,
                })
            return reports, set()

        # Sheet content writes happen only on temporary tabs.  A failure here
        # cannot alter any source title or cell.  Any created temporary tab is
        # intentionally retained as an inspectable orphan and reported below.
        phase = "replacement_build"
        for owner in owners:
            desired_rows = tuple(desired_rows_by_owner.get(owner, ()))
            try:
                replacement = spreadsheet.add_worksheet(
                    title=temp_titles[owner],
                    rows=max(100, len(desired_rows) + 20),
                    cols=len(crm_sheets.FOLLOWUP_HEADERS),
                )
            except APIError as exc:
                raise SheetsError(
                    "failed creating a temporary canonical followup tab"
                ) from exc
            replacements[owner] = replacement
            crm_sheets.ensure_additive_headers(
                replacement,
                crm_sheets.FOLLOWUP_HEADERS,
            )

            action_ids = [
                row[crm_sheets.COL_ACTION_ID] for row in desired_rows
            ]
            actions = OpportunityAction.objects.filter(pk__in=action_ids)
            baseline_by_id = {
                str(action.id): dict(action.sheet_human_snapshot or {})
                for action in actions
            }
            adapter = crm_sheets.followups_adapter(replacement)
            initial_plan = adapter.plan(
                desired_rows,
                baseline_by_id=baseline_by_id,
            )
            if (
                initial_plan.conflicts
                or initial_plan.imports
                or initial_plan.duplicate_keys
                or initial_plan.unkeyed_nonempty_rows
            ):
                raise SheetsError(
                    "temporary canonical followup plan was not conflict-free"
                )
            build_summary = adapter.apply(initial_plan, dry_run=False)

            phase = "replacement_validation"
            published_baseline = {
                update.stable_id: dict(update.values)
                for update in initial_plan.baseline_updates
            }
            before = crm_sheets.SheetSnapshot.read(
                replacement,
                required_headers=crm_sheets.FOLLOWUP_HEADERS,
                key_header=crm_sheets.COL_ACTION_ID,
            )
            verified_plan = adapter.plan(
                desired_rows,
                remove_missing=False,
                baseline_by_id=published_baseline,
            )
            after = crm_sheets.SheetSnapshot.read(
                replacement,
                required_headers=crm_sheets.FOLLOWUP_HEADERS,
                key_header=crm_sheets.COL_ACTION_ID,
            )
            if (
                _followup_snapshot_signature(before)
                != _followup_snapshot_signature(after)
                or verified_plan.conflicts
                or verified_plan.imports
                or verified_plan.appends
                or verified_plan.changes
                or verified_plan.duplicate_keys
                or verified_plan.unkeyed_nonempty_rows
            ):
                raise SheetsError(
                    "temporary canonical followup tab failed read-back validation"
                )
            replacement_plans[owner] = verified_plan
            reports[owner].update({
                **build_summary,
                "title": crm_sheets.sender_followups_tab(owner),
                "replacement_built": True,
                "replacement_verified": True,
            })
            phase = "replacement_build"

        # Revalidate canonical DB ownership/target identity once all temporary
        # tabs are complete and immediately before the title cutover.
        phase = "canonical_identity_revalidation"
        _validate_legacy_replacement_payloads(
            owners=owners,
            desired_rows_by_owner=desired_rows_by_owner,
            crm_sheets=crm_sheets,
            OpportunityAction=OpportunityAction,
        )

        # All source/replacement pairs are now ready.  This is the first and
        # only operation that changes legacy titles.
        phase = "atomic_title_swap"
        swaps = tuple(
            {
                "existing_ws": legacy_worksheets[owner],
                "replacement_ws": replacements[owner],
                "legacy_title": archive_titles[owner],
                "canonical_title": crm_sheets.sender_followups_tab(owner),
            }
            for owner in owners
        )
        _swap_legacy_followup_titles(spreadsheet, swaps=swaps)
    except (APIError, SheetsError):
        # No baseline has been committed and, because the only source-title
        # mutation is the atomic batch above, a failed batch leaves every old
        # canonical tab untouched.  Temporary tabs are safe, inspectable
        # orphans rather than data loss.
        for owner in owners:
            migration = dict(legacy_reports.get(owner, {}) or {})
            material_rows = int(
                migration.get("material_rows_skipped", 0) or 0
            )
            existing = reports.setdefault(
                owner,
                {
                    "title": crm_sheets.sender_followups_tab(owner),
                    "atomic_archive": True,
                    "legacy_preserved": True,
                    "review_required": bool(material_rows),
                    "material_rows_skipped": material_rows,
                    "material_skip_reasons": dict(
                        migration.get("material_skip_reasons", {}) or {}
                    ),
                    "due_now_rows": len(
                        desired_rows_by_owner.get(owner, ())
                    ),
                },
            )
            existing.update({
                "blocked": True,
                "status": "atomic_archive_failed",
                "failure_phase": phase,
                "legacy_source_untouched": True,
                "baseline_committed": False,
            })
            if owner in archive_titles:
                existing["planned_archive_title"] = archive_titles[owner]
            replacement = replacements.get(owner)
            if replacement is not None:
                existing["orphan_temp_tab"] = str(
                    getattr(replacement, "title", temp_titles.get(owner, ""))
                )
        return reports, set(owners)

    # Baselines describe the now-live canonical replacements and therefore
    # cannot advance until the all-owner title batch has succeeded.
    for owner in owners:
        commit_followup_baselines(
            replacement_plans[owner].baseline_updates
        )
        reports[owner].update({
            "blocked": False,
            "status": "archived_and_activated",
            "legacy_tab": archive_titles[owner],
            "baseline_committed": True,
        })
    return reports, set()


def _validate_legacy_replacement_payloads(
    *,
    owners,
    desired_rows_by_owner,
    crm_sheets,
    OpportunityAction,
) -> None:
    """Fail closed on canonical identity/owner conflicts before temp writes."""
    from linkedin.exceptions import SheetsError

    rows_by_action_id: dict[str, tuple[str, dict]] = {}
    action_uuid_by_id: dict[str, uuid.UUID] = {}
    for owner in owners:
        local_ids: set[str] = set()
        for row in desired_rows_by_owner.get(owner, ()):
            action_id = str(row.get(crm_sheets.COL_ACTION_ID, "") or "").strip()
            try:
                canonical_uuid = uuid.UUID(action_id)
            except (TypeError, ValueError, AttributeError) as exc:
                raise SheetsError(
                    "canonical replacement row has an invalid Action ID"
                ) from exc
            canonical_id = str(canonical_uuid)
            if canonical_id != action_id.casefold():
                raise SheetsError(
                    "canonical replacement row has a noncanonical Action ID"
                )
            if action_id in local_ids or action_id in rows_by_action_id:
                raise SheetsError(
                    "canonical replacement would duplicate an Action across sender tabs"
                )
            local_ids.add(action_id)
            rows_by_action_id[action_id] = (owner, dict(row))
            action_uuid_by_id[action_id] = canonical_uuid
            row_owner = str(row.get(crm_sheets.COL_OWNER, "") or "").strip()
            if row_owner != owner:
                raise SheetsError(
                    "canonical replacement row conflicts with its sender owner"
                )

    if not rows_by_action_id:
        return
    queryset = OpportunityAction.objects.filter(
        pk__in=tuple(action_uuid_by_id.values())
    ).select_related("opportunity__owner")
    actions = {str(action.id): action for action in queryset}
    if set(actions) != set(rows_by_action_id):
        raise SheetsError(
            "canonical replacement references a missing Action"
        )
    for action_id, (owner, row) in rows_by_action_id.items():
        action = actions[action_id]
        action_owner = str(
            getattr(getattr(action.opportunity, "owner", None), "handle", "")
            or ""
        ).strip()
        if action_owner != owner:
            raise SheetsError(
                "canonical replacement Action ownership changed during preflight"
            )
        if action.target_lead_id is None:
            raise SheetsError(
                "canonical replacement Action has no durable target Lead"
            )
        if str(row.get(crm_sheets.COL_LEAD_ID, "") or "").strip() != str(
            action.target_lead_id
        ):
            raise SheetsError(
                "canonical replacement row conflicts with the Action target Lead"
            )
        if str(row.get(crm_sheets.COL_OPPORTUNITY_ID, "") or "").strip() != str(
            action.opportunity_id
        ):
            raise SheetsError(
                "canonical replacement row conflicts with its Opportunity"
            )


def _reserve_unique_tab_title(reserved_titles: set[str], base: str) -> str:
    """Return and reserve a valid unique title without another API read."""
    candidate = base[:100]
    suffix = 2
    while candidate in reserved_titles:
        marker = f" {suffix}"
        candidate = f"{base[:100 - len(marker)]}{marker}"
        suffix += 1
    reserved_titles.add(candidate)
    return candidate


def _swap_worksheet_titles(
    spreadsheet,
    *,
    existing_ws,
    replacement_ws,
    legacy_title: str,
    canonical_title: str,
) -> None:
    """Rename one legacy/replacement pair in an atomic Sheets batch."""
    _swap_legacy_followup_titles(
        spreadsheet,
        swaps=(
            {
                "existing_ws": existing_ws,
                "replacement_ws": replacement_ws,
                "legacy_title": legacy_title,
                "canonical_title": canonical_title,
            },
        ),
    )


def _swap_legacy_followup_titles(spreadsheet, *, swaps) -> None:
    """Archive every legacy tab and activate every replacement atomically.

    Google Sheets ``batchUpdate`` applies all requests atomically.  All legacy
    titles are released first inside the same request body, then every fully
    prepared replacement takes its canonical title.  This prevents a partial
    first-run cutover and avoids a window where an Action could appear under
    both an old and a new sender title.
    """
    from linkedin.exceptions import SheetsError

    swaps = tuple(swaps)
    if not swaps:
        return
    requests = [
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": swap["existing_ws"].id,
                    "title": swap["legacy_title"],
                },
                "fields": "title",
            },
        }
        for swap in swaps
    ]
    requests.extend(
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": swap["replacement_ws"].id,
                    "title": swap["canonical_title"],
                },
                "fields": "title",
            },
        }
        for swap in swaps
    )
    try:
        spreadsheet.batch_update({"requests": requests})
    except APIError as exc:
        raise SheetsError(
            "failed to atomically preserve and replace legacy followup tabs"
        ) from exc


def recover_failed_crm_sheet_titles(
    spreadsheet,
    *,
    legacy_titles_by_owner,
    expected_sheet_ids_by_title,
    failed_at,
    crm_sheets,
) -> dict:
    """Restore legacy sender titles after a rolled-back Sheet publication.

    This is a deliberately narrow recovery primitive for a refresh that wrote
    generated Sheet surfaces but rolled its enclosing database transaction
    back.  The caller must supply the exact dated legacy titles and the exact
    worksheet IDs observed during a separate structural inspection.  The
    helper re-inventories the workbook, rejects any title/ID drift or
    collision, then performs one atomic ``spreadsheets.batchUpdate`` containing
    title-only ``updateSheetProperties`` requests.

    Generated Opportunities, Pipeline, Recovery, and sender Followups are kept
    under unique ``Failed CRM`` titles for inspection.  The dated legacy sender
    tabs regain their canonical titles.  No worksheet is deleted and no cell,
    dimension, format, or other sheet property is edited.
    """
    from linkedin.exceptions import SheetsError

    owners = ("Arian", "Athena", "Chuka", "Leili")
    legacy_titles_by_owner = dict(legacy_titles_by_owner)
    if set(legacy_titles_by_owner) != set(owners):
        raise SheetsError(
            "failed CRM title recovery requires the exact four sender archives"
        )

    canonical_followups = {
        owner: crm_sheets.sender_followups_tab(owner) for owner in owners
    }
    for owner in owners:
        legacy_title = str(legacy_titles_by_owner[owner] or "")
        pattern = rf"{re.escape(canonical_followups[owner])} Legacy \d{{8}}(?: \d+)?"
        if re.fullmatch(pattern, legacy_title) is None:
            raise SheetsError(
                "failed CRM title recovery received an invalid legacy title"
            )

    # Release every sender canonical title before assigning one to a legacy
    # source.  The other generated surfaces can be archived in the same batch.
    generated_titles = (
        *(canonical_followups[owner] for owner in owners),
        crm_sheets.OPPORTUNITIES_TAB,
        crm_sheets.PIPELINE_TAB,
        crm_sheets.RECOVERY_TAB,
    )
    legacy_titles = tuple(legacy_titles_by_owner[owner] for owner in owners)
    required_titles = (*generated_titles, *legacy_titles)
    if len(set(required_titles)) != len(required_titles):
        raise SheetsError(
            "failed CRM title recovery source titles are not unique"
        )

    expected_sheet_ids_by_title = dict(expected_sheet_ids_by_title)
    if set(expected_sheet_ids_by_title) != set(required_titles):
        raise SheetsError(
            "failed CRM title recovery requires exact worksheet ID coverage"
        )
    expected_ids = tuple(expected_sheet_ids_by_title.values())
    if any(
        isinstance(sheet_id, bool)
        or not isinstance(sheet_id, int)
        or sheet_id < 0
        for sheet_id in expected_ids
    ) or len(set(expected_ids)) != len(expected_ids):
        raise SheetsError(
            "failed CRM title recovery worksheet IDs are invalid or duplicated"
        )

    worksheets = tuple(
        crm_sheets.retry_sheet_read(
            spreadsheet.worksheets,
            context="failed inventorying worksheets for CRM title recovery",
        )
    )

    worksheets_by_title = {}
    observed_ids: set[int] = set()
    for worksheet in worksheets:
        title = str(getattr(worksheet, "title", "") or "")
        sheet_id = getattr(worksheet, "id", None)
        if not title:
            raise SheetsError(
                "failed CRM title recovery found a worksheet without a title"
            )
        if title in worksheets_by_title:
            raise SheetsError(
                "failed CRM title recovery found duplicate worksheet titles"
            )
        if (
            isinstance(sheet_id, bool)
            or not isinstance(sheet_id, int)
            or sheet_id < 0
            or sheet_id in observed_ids
        ):
            raise SheetsError(
                "failed CRM title recovery found invalid worksheet IDs"
            )
        worksheets_by_title[title] = worksheet
        observed_ids.add(sheet_id)

    for title in required_titles:
        worksheet = worksheets_by_title.get(title)
        if worksheet is None:
            raise SheetsError(
                "failed CRM title recovery source title is missing"
            )
        if worksheet.id != expected_sheet_ids_by_title[title]:
            raise SheetsError(
                "failed CRM title recovery source worksheet changed"
            )

    reserved_titles = set(worksheets_by_title)
    stamp = failed_at.strftime("%Y%m%dT%H%M%S")
    failed_titles_by_canonical = {
        title: _reserve_unique_tab_title(
            reserved_titles,
            f"Failed CRM {stamp} {title}",
        )
        for title in generated_titles
    }

    requests = [
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": expected_sheet_ids_by_title[title],
                    "title": failed_titles_by_canonical[title],
                },
                "fields": "title",
            },
        }
        for title in generated_titles
    ]
    requests.extend(
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": expected_sheet_ids_by_title[
                        legacy_titles_by_owner[owner]
                    ],
                    "title": canonical_followups[owner],
                },
                "fields": "title",
            },
        }
        for owner in owners
    )
    try:
        spreadsheet.batch_update({"requests": requests})
    except APIError as exc:
        raise SheetsError(
            "failed applying atomic CRM title recovery"
        ) from exc

    return {
        "renamed_tabs": len(requests),
        "failed_outputs": dict(failed_titles_by_canonical),
        "restored_followups": dict(canonical_followups),
    }


def _verify_workbook_identity(
    spreadsheet,
    *,
    configured_id: str,
    require_sales_motion_guard: bool = False,
) -> None:
    from linkedin.exceptions import SheetsError

    live_id = str(getattr(spreadsheet, "id", ""))
    if not configured_id or live_id != configured_id:
        raise SheetsError("opened workbook does not match GOOGLE_SHEETS_ID")
    sales_motion_id = os.getenv("SALES_MOTION_VERSIONS_GOOGLE_SHEETS_ID", "").strip()
    if not sales_motion_id:
        sales_motion_id = os.getenv("SALES_MOTION_SHEETS_ID", "").strip()
    if require_sales_motion_guard and not sales_motion_id:
        raise SheetsError(
            "refusing live CRM apply without a configured Sales Motion "
            "workbook identity guard"
        )
    if sales_motion_id and live_id == sales_motion_id:
        raise SheetsError("refusing to use the Sales Motion workbook as the CRM")


def _crm_stable_keys(*, crm_sheets, sheets) -> dict[str, str]:
    """Return every managed surface and its non-name stable identity."""
    keys = {
        sheets.GOOGLE_SHEETS_TAB_NAME: sheets.COL_LINKEDIN_URL,
        crm_sheets.OPPORTUNITIES_TAB: crm_sheets.COL_OPPORTUNITY_ID,
        crm_sheets.PIPELINE_TAB: crm_sheets.COL_OPPORTUNITY_ID,
        crm_sheets.RECOVERY_TAB: crm_sheets.COL_OPPORTUNITY_ID,
    }
    for owner in ("Arian", "Athena", "Chuka", "Leili"):
        keys[crm_sheets.sender_followups_tab(owner)] = crm_sheets.COL_ACTION_ID
    return keys


def _build_granola_client(
    *,
    api_key,
    base_url,
    timeout,
    GranolaClient,
    GranolaError,
):
    """Return a usable client or the typed setup error for Gemini fallback."""
    if not api_key:
        return None, None
    try:
        return GranolaClient(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
        ), None
    except GranolaError as exc:
        # The sync persists failure telemetry and falls back to Gemini when it
        # receives this typed constructor/configuration error.
        return None, exc


def _sanitized_granola_warnings(result) -> list[str]:
    """Return aggregate warning categories without provider-controlled text.

    Granola exception messages and note IDs are external input.  The refresh
    report already carries ``result.counts()``; warning strings therefore add
    only fixed labels and integers derived from trusted counters.
    """
    categories = (
        ("metadata_failures", "metadata failures"),
        ("transcript_failures", "transcript failures"),
        ("pending_details", "pending note details"),
        ("unavailable", "unavailable notes"),
        ("ambiguous", "ambiguous matches"),
        ("unmatched", "unmatched notes"),
    )
    warnings = [
        f"Granola {label}: {int(getattr(result, field, 0) or 0)}"
        for field, label in categories
        if int(getattr(result, field, 0) or 0) > 0
    ]
    raw_count = len(getattr(result, "warnings", ()) or ())
    if raw_count:
        warnings.append(
            f"Granola provider warning messages suppressed: {raw_count}"
        )
    return warnings


def _inventory_with_stable_keys(
    spreadsheet,
    *,
    crm_sheets,
    stable_keys: dict[str, str],
) -> dict:
    """Inventory all tabs and label the stable key audited for each surface."""
    inventory = crm_sheets.inventory_spreadsheet(
        spreadsheet,
        stable_keys=stable_keys,
    )
    for tab in inventory.get("tabs", ()):
        tab["stable_key_header"] = stable_keys.get(str(tab.get("title", "")), "")
    return inventory


def _people_gate_then_activate_managed_tabs(
    *,
    spreadsheet,
    people_before,
    skip_people: bool,
    dry_run: bool,
    run_people_sync,
    crm_sheets,
    sheets,
) -> tuple[dict, tuple[tuple[object | None, object], ...], bool]:
    """Run/verify People before any canonical-tab structural mutation."""
    if skip_people:
        people_gate_blocked = False
        if dry_run:
            people_report = {"status": "skipped"}
        else:
            # --skip-people skips publication, not the mandatory preservation
            # assertion protecting later structural writes.
            people_after = _capture_people_preservation_snapshot(
                spreadsheet,
                sheets=sheets,
            )
            verification = sheets.verify_people_preserved(
                people_before,
                people_after,
            )
            people_report = {
                "status": "skipped_and_verified",
                **verification.as_dict(),
            }
    else:
        # Preflight the exact narrow publisher before permitting its first
        # worksheet write.  True row errors and duplicate stable Lead IDs are
        # unsafe; legacy duplicate LinkedIn URLs remain visible telemetry but
        # do not imply that a DB Lead would be omitted.
        people_preflight = run_people_sync(
            dry_run=True,
            # The narrow publisher may print contact names for direct CLI use;
            # the CRM orchestrator retains only aggregate counts.
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            lock_held=True,
        )
        people_gate_blocked = bool(
            int(people_preflight.get("errored", 0) or 0)
            or int(people_preflight.get("duplicate_lead_ids", 0) or 0)
        )
        if people_gate_blocked and not dry_run:
            from linkedin.exceptions import SheetsError

            raise SheetsError(
                "People publisher preflight reported errors or duplicate "
                "stable Lead IDs; no worksheet writes were attempted"
            )
        people_result = (
            people_preflight
            if dry_run
            else run_people_sync(
                dry_run=False,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
                lock_held=True,
            )
        )
        live_gate_blocked = bool(
            int(people_result.get("errored", 0) or 0)
            or int(people_result.get("duplicate_lead_ids", 0) or 0)
        )
        people_gate_blocked = people_gate_blocked or live_gate_blocked
        people_result["rows_before"] = people_before.row_count
        people_result["gate_blocked"] = people_gate_blocked
        if not dry_run:
            people_result["preflight"] = people_preflight
        if dry_run:
            people_report = people_result
        else:
            people_after = _capture_people_preservation_snapshot(
                spreadsheet,
                sheets=sheets,
            )
            verification = sheets.verify_people_preserved(
                people_before,
                people_after,
            )
            people_report = {
                **people_result,
                "status": "published_and_verified",
                **verification.as_dict(),
            }
            if people_gate_blocked:
                from linkedin.exceptions import SheetsError

                raise SheetsError(
                    "People publisher reported errors or duplicate stable "
                    "Lead IDs; preservation passed but downstream structural "
                    "publication is blocked"
                )

    # The gate above is the first permitted live workbook write.  Only after
    # it succeeds may canonical/derived tabs be created or receive headers.
    managed = tuple(
        crm_sheets.ensure_managed_tab(
            spreadsheet,
            title=title,
            required_headers=headers,
            dry_run=dry_run,
        )
        for title, headers in (
            (crm_sheets.OPPORTUNITIES_TAB, crm_sheets.OPPORTUNITY_HEADERS),
            (crm_sheets.PIPELINE_TAB, crm_sheets.PIPELINE_HEADERS),
            (crm_sheets.RECOVERY_TAB, crm_sheets.RECOVERY_HEADERS),
        )
    )
    return people_report, managed, people_gate_blocked


def _capture_people_preservation_snapshot(spreadsheet, *, sheets):
    """Resolve People from the identity-verified workbook and use its verifier."""
    from linkedin.exceptions import SheetsError
    from linkedin.notifications.crm_sheets import retry_sheet_read

    try:
        ws = retry_sheet_read(
            lambda: spreadsheet.worksheet(sheets.GOOGLE_SHEETS_TAB_NAME),
            context="failed resolving People preservation snapshot tab",
        )
    except WorksheetNotFound as exc:
        raise SheetsError(
            "failed reading People preservation snapshot: People tab is missing"
        ) from exc
    return sheets.capture_people_preservation_snapshot(ws)


def _assert_people_dnc_headers(snapshot, *, sheets) -> None:
    """Fail closed when the People suppression surface cannot be read."""
    from linkedin.exceptions import SheetsError

    required = {sheets.COL_LINKEDIN_URL, sheets.COL_OUTREACH_STATUS}
    missing = sorted(required - set(snapshot.headers))
    if missing:
        raise SheetsError(
            "People is missing required Don't-send safety header(s): "
            + ", ".join(missing)
        )


def _people_explicit_stage_lead_ids(spreadsheet, *, sheets) -> tuple[set[int], dict]:
    """Extract conservative legacy bootstrap signals using stable IDs only.

    Every ordinary People row receives Prospecting from the publisher, so a
    merely nonblank Stage is not evidence.  Only advanced legacy values qualify
    and every candidate must carry a canonical numeric Lead ID whose LinkedIn
    URL agrees with the database.  Names and company strings are never identity.
    """
    from collections import Counter, defaultdict

    from crm.models import Lead
    from linkedin.exceptions import SheetsError

    try:
        ws = spreadsheet.worksheet(sheets.GOOGLE_SHEETS_TAB_NAME)
        try:
            values = ws.get_all_values(
                value_render_option=ValueRenderOption.formula,
            )
        except TypeError:
            values = ws.get_all_values()
    except (WorksheetNotFound, APIError) as exc:
        raise SheetsError(f"failed reading People explicit stage signals: {exc}") from exc

    headers = [str(value).strip() for value in (values[0] if values else [])]
    duplicate_headers = [
        header
        for header, count in Counter(header for header in headers if header).items()
        if count > 1
    ]
    if duplicate_headers:
        raise SheetsError(f"People has duplicate headers: {duplicate_headers}")
    required = {
        sheets.COL_LEAD_ID,
        sheets.COL_LINKEDIN_URL,
        sheets.COL_STAGE,
    }
    missing_headers = sorted(required - set(headers))
    empty_report = {
        "status": "unavailable" if missing_headers else "ready",
        "missing_required_headers": len(missing_headers),
        "advanced_stage_rows": 0,
        "eligible_lead_ids": 0,
        "invalid_lead_id_rows": 0,
        "duplicate_lead_id_groups": 0,
        "duplicate_lead_id_rows": 0,
        "missing_leads": 0,
        "disqualified_leads": 0,
        "missing_linkedin_urls": 0,
        "linkedin_url_conflicts": 0,
        "identity_ambiguities": 0,
    }
    if missing_headers:
        return set(), empty_report

    lead_index = headers.index(sheets.COL_LEAD_ID)
    url_index = headers.index(sheets.COL_LINKEDIN_URL)
    stage_index = headers.index(sheets.COL_STAGE)
    advanced_stages = {
        str(sheets.STAGE_QUALIFICATION).casefold(),
        str(sheets.STAGE_MEETING).casefold(),
        str(sheets.STAGE_CLOSING).casefold(),
        str(sheets.STAGE_WON).casefold(),
    }
    candidates: dict[int, list[str]] = defaultdict(list)
    invalid_lead_id_rows = 0
    advanced_stage_rows = 0
    for row in values[1:]:
        raw_stage = str(row[stage_index] if stage_index < len(row) else "").strip()
        if raw_stage.startswith("=") or raw_stage.casefold() not in advanced_stages:
            continue
        advanced_stage_rows += 1
        raw_lead_id = str(
            row[lead_index] if lead_index < len(row) else ""
        ).strip()
        if (
            not raw_lead_id.isdigit()
            or int(raw_lead_id) <= 0
            or str(int(raw_lead_id)) != raw_lead_id
        ):
            invalid_lead_id_rows += 1
            continue
        url = str(row[url_index] if url_index < len(row) else "").strip()
        candidates[int(raw_lead_id)].append(url)

    duplicates = {
        lead_id: urls for lead_id, urls in candidates.items() if len(urls) > 1
    }
    unique_candidates = {
        lead_id: urls[0]
        for lead_id, urls in candidates.items()
        if lead_id not in duplicates
    }
    leads = Lead.objects.in_bulk(unique_candidates)
    eligible: set[int] = set()
    missing_leads = disqualified_leads = 0
    missing_urls = url_conflicts = 0
    for lead_id, sheet_url in unique_candidates.items():
        lead = leads.get(lead_id)
        if lead is None:
            missing_leads += 1
            continue
        if lead.disqualified:
            disqualified_leads += 1
            continue
        sheet_identity = sheets.linkedin_identity_key(sheet_url)
        database_identity = sheets.linkedin_identity_key(lead.linkedin_url)
        if not sheet_identity or not database_identity:
            missing_urls += 1
            continue
        if sheet_identity != database_identity:
            url_conflicts += 1
            continue
        eligible.add(lead_id)

    duplicate_rows = sum(len(urls) for urls in duplicates.values())
    ambiguities = (
        invalid_lead_id_rows
        + len(duplicates)
        + missing_leads
        + disqualified_leads
        + missing_urls
        + url_conflicts
    )
    return eligible, {
        **empty_report,
        "status": "ready",
        "advanced_stage_rows": advanced_stage_rows,
        "eligible_lead_ids": len(eligible),
        "invalid_lead_id_rows": invalid_lead_id_rows,
        "duplicate_lead_id_groups": len(duplicates),
        "duplicate_lead_id_rows": duplicate_rows,
        "missing_leads": missing_leads,
        "disqualified_leads": disqualified_leads,
        "missing_linkedin_urls": missing_urls,
        "linkedin_url_conflicts": url_conflicts,
        "identity_ambiguities": ambiguities,
    }


def _unique_tab_title(spreadsheet, base: str) -> str:
    existing = {worksheet.title for worksheet in spreadsheet.worksheets()}
    candidate = base[:100]
    suffix = 2
    while candidate in existing:
        marker = f" {suffix}"
        candidate = f"{base[:100 - len(marker)]}{marker}"
        suffix += 1
    return candidate


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _public_inventory(inventory: dict) -> dict:
    """Return structural counts without exposing the configured Sheet ID."""
    public = dict(inventory)
    spreadsheet_id = str(public.pop("spreadsheet_id", ""))
    if spreadsheet_id:
        public["spreadsheet_fingerprint"] = _fingerprint(spreadsheet_id)
    return public


def _action_counts_for_run(initial, final=None) -> dict:
    """Keep final placement counts while retaining both passes' mutations."""
    current = final or initial
    counts = current.counts()
    if final is not None:
        for field in (
            "actions_created",
            "actions_completed",
            "actions_superseded",
            "actions_targeted",
            "activity_updated",
        ):
            counts[field] = getattr(initial, field, 0) + getattr(final, field, 0)
        counts["passes"] = {
            "initial": initial.counts(),
            "post_legacy_import": final.counts(),
        }
    return counts


def _followup_block_summary(
    *,
    initial_conflicts: int,
    invalid_imports: int,
    initial_identity_blockers: int,
    fresh_conflicts: int,
    fresh_imports_remaining: int,
    fresh_identity_blockers: int,
    publication_conflicts: int = 0,
    publication_imports_remaining: int = 0,
    publication_identity_blockers: int = 0,
    owners_blocked: int = 0,
) -> dict[str, int | str]:
    """Expose each independent fail-closed reason without collapsing counts."""
    return {
        "blocked": True,
        "reason": "invalid, conflicting, or unsafe human action edit",
        "owners_blocked": owners_blocked,
        "initial_conflicts": initial_conflicts,
        "invalid_imports": invalid_imports,
        "initial_identity_blockers": initial_identity_blockers,
        "fresh_conflicts": fresh_conflicts,
        "fresh_imports_remaining": fresh_imports_remaining,
        "fresh_identity_blockers": fresh_identity_blockers,
        "publication_conflicts": publication_conflicts,
        "publication_imports_remaining": publication_imports_remaining,
        "publication_identity_blockers": publication_identity_blockers,
    }


def _sum_followup_import_reports(reports) -> dict[str, int]:
    fields = (
        "actions_updated",
        "fields_imported",
        "completed",
        "reopened",
        "opportunities_pinned",
        "invalid",
    )
    totals = {field: 0 for field in fields}
    for report in reports:
        counts = report.counts()
        for field in fields:
            totals[field] += int(counts.get(field, 0) or 0)
    return totals


def _blocked_followup_owners(owner_state) -> set[str]:
    """Keep merge failures sender-scoped so other queues still refresh."""
    return {
        owner
        for owner, state in owner_state.items()
        if any(int(value or 0) for value in state.values())
    }


def _opportunity_identity_blocker_count(plan) -> int:
    """Every nonempty row must resolve to this run's canonical Opportunity."""
    return len(plan.unkeyed_nonempty_rows) + len(plan.retained_missing_keys)


def _legacy_followup_blocked_owners(failed_archive_owners) -> set[str]:
    """Only owners whose grouped atomic archive failed remain blocked.

    Unresolved legacy identities stay in their complete dated source archive;
    they are review work, not a reason to guess a recipient or freeze unrelated
    canonical queues.
    """
    return set(failed_archive_owners)


def _propagate_linked_owner_blockers(owner_state, linked_counts, *, field) -> None:
    """Block the destination owner when an old-owner row has human edits."""
    for owner, count in linked_counts.items():
        if owner in owner_state:
            owner_state[owner][field] += int(count or 0)


def _followup_identity_blocker_count(telemetry) -> int:
    """Count every independently reported unsafe stable-identity row."""
    return sum(
        int(telemetry.get(field, 0) or 0)
        for field in (
            "unknown_material_action_rows",
            "foreign_owner_material_action_rows",
            "invalid_material_action_rows",
            "duplicate_action_id_rows",
            "unkeyed_nonempty_action_rows",
            "malformed_baseline_action_rows",
            "local_validation_error_rows",
            "concurrent_sheet_change_rows",
        )
    )


def _sender_followup_plan(
    *,
    owner,
    ws,
    due_rows,
    crm_sheets,
    OpportunityAction,
):
    """Plan one sender independently; deterministic row defects stay local."""
    from linkedin.exceptions import SheetsError

    try:
        desired, baselines, telemetry = _followup_plan_payload(
            owner=owner,
            ws=ws,
            due_rows=due_rows,
            crm_sheets=crm_sheets,
            OpportunityAction=OpportunityAction,
        )
        if telemetry.get("_local_validation_blockers"):
            return desired, baselines, telemetry, None
        plan = crm_sheets.followups_adapter(ws).plan(
            desired,
            remove_missing=False,
            baseline_by_id=baselines,
        )
        return desired, baselines, telemetry, plan
    except SheetsError as exc:
        reason = _sender_followup_validation_reason(
            exc,
            crm_sheets=crm_sheets,
        )
        if not reason:
            # Network/API/workbook failures retain outer-transaction rollback
            # semantics; only deterministic sender-row defects are isolated.
            raise
        telemetry = _empty_followup_telemetry()
        telemetry.update({
            "local_validation_error": reason,
            "local_validation_error_rows": 1,
            "_local_validation_blockers": 1,
        })
        return (), {}, telemetry, None


def _stable_sender_publication_plan(
    *,
    owner,
    ws,
    due_rows,
    crm_sheets,
    OpportunityAction,
):
    """Build a no-write plan only when the sender snapshot stays unchanged."""
    before = crm_sheets.SheetSnapshot.read(
        ws,
        required_headers=crm_sheets.FOLLOWUP_HEADERS,
        key_header=crm_sheets.COL_ACTION_ID,
    )
    desired, baselines, telemetry = _followup_plan_payload(
        owner=owner,
        ws=ws,
        due_rows=due_rows,
        crm_sheets=crm_sheets,
        OpportunityAction=OpportunityAction,
        snapshot=before,
    )
    if telemetry.get("_local_validation_blockers"):
        return desired, telemetry, None
    plan = crm_sheets.followups_adapter(ws).plan(
        desired,
        remove_missing=False,
        baseline_by_id=baselines,
    )
    after = crm_sheets.SheetSnapshot.read(
        ws,
        required_headers=crm_sheets.FOLLOWUP_HEADERS,
        key_header=crm_sheets.COL_ACTION_ID,
    )
    after_desired, after_baselines, after_telemetry = _followup_plan_payload(
        owner=owner,
        ws=ws,
        due_rows=due_rows,
        crm_sheets=crm_sheets,
        OpportunityAction=OpportunityAction,
        snapshot=after,
    )
    if (
        _followup_snapshot_signature(before)
        != _followup_snapshot_signature(after)
        or desired != after_desired
        or baselines != after_baselines
        or _followup_retirement_safety_signature(telemetry)
        != _followup_retirement_safety_signature(after_telemetry)
    ):
        after_telemetry["concurrent_sheet_change_rows"] = 1
        after_telemetry["_local_validation_blockers"] = (
            int(after_telemetry.get("_local_validation_blockers", 0) or 0) + 1
        )
        return after_desired, after_telemetry, None
    telemetry = after_telemetry
    telemetry.setdefault("concurrent_sheet_change_rows", 0)
    _retire_safe_followup_rows(
        plan,
        ws=ws,
        action_ids=telemetry.get("_safe_retire_action_ids", ()),
        crm_sheets=crm_sheets,
        snapshot=after,
    )
    return desired, telemetry, plan


def _followup_snapshot_signature(snapshot) -> tuple:
    return (
        snapshot.live_headers,
        snapshot.rows,
        tuple(sorted(snapshot.rows_by_key.items())),
        snapshot.unkeyed_nonempty_rows,
    )


def _followup_retirement_safety_signature(telemetry) -> tuple:
    return tuple(
        json.dumps(telemetry.get(field), sort_keys=True, default=str)
        for field in (
            "duplicate_action_id_rows",
            "unkeyed_nonempty_action_rows",
            "malformed_baseline_action_rows",
            "invalid_material_action_rows",
            "unknown_material_action_rows",
            "foreign_owner_material_action_rows",
            "baseline_divergent_action_rows",
            "operator_content_action_rows",
            "_safe_retire_action_ids",
            "_linked_blocked_owners",
        )
    )


def _sender_followup_validation_reason(exc, *, crm_sheets) -> str:
    message = str(exc).casefold()
    if "duplicate headers" in message:
        return "duplicate_headers"
    if (
        f"duplicate {str(crm_sheets.COL_ACTION_ID).casefold()} rows"
        in message
    ):
        return "duplicate_action_ids"
    baseline = str(crm_sheets.COL_HUMAN_BASELINE).casefold()
    if baseline in message and (
        "malformed" in message or "must be an object" in message
    ):
        return "malformed_human_baseline"
    return ""


def _empty_followup_telemetry() -> dict[str, object]:
    return {
        "sheet_action_rows": 0,
        "sheet_action_keyed_rows": 0,
        "duplicate_action_id_groups": 0,
        "duplicate_action_id_rows": 0,
        "unkeyed_nonempty_action_rows": 0,
        "malformed_baseline_action_rows": 0,
        "local_validation_error_rows": 0,
        "concurrent_sheet_change_rows": 0,
        "invalid_action_id_rows": 0,
        "invalid_material_action_rows": 0,
        "due_now_rows": 0,
        "retained_canonical_rows_considered": 0,
        "unknown_action_rows": 0,
        "unknown_material_action_rows": 0,
        "foreign_owner_action_rows": 0,
        "foreign_owner_material_action_rows": 0,
        "baseline_divergent_action_rows": 0,
        "safe_retired_action_rows": 0,
        "operator_content_action_rows": 0,
        "_safe_retire_action_ids": (),
        "_local_validation_blockers": 0,
        "_linked_blocked_owners": {},
    }


def _followup_plan_payload(
    *,
    owner: str,
    ws,
    due_rows,
    crm_sheets,
    OpportunityAction,
    snapshot=None,
) -> tuple[tuple[dict, ...], dict[str, dict], dict[str, object]]:
    """Include existing stable-ID rows when planning human-field imports.

    Waiting, Recovery, and handled/history Actions are intentionally absent
    from the due-now payload.  They still need to participate in the
    Sheet->DB three-way merge before the derived tab is regenerated.  Only
    Actions currently owned by this exact sender are eligible for import.
    """
    if snapshot is None:
        snapshot = crm_sheets.SheetSnapshot.read(
            ws,
            required_headers=crm_sheets.FOLLOWUP_HEADERS,
            key_header=crm_sheets.COL_ACTION_ID,
        )
    sheet_human_rows = []
    managed_formula_by_id = {}
    operator_content_by_id = {}
    current_row_by_id = {}
    malformed_baseline_rows = []
    for stable_id, row_numbers in snapshot.rows_by_key.items():
        current = snapshot.row_dict(row_numbers[0])
        current_row_by_id[stable_id] = current
        sheet_human_rows.append({
            crm_sheets.COL_ACTION_ID: stable_id,
            **{
                field: current.get(field, "")
                for field in crm_sheets.FOLLOWUP_HUMAN_FIELDS
            },
        })
        managed_formula_by_id[stable_id] = any(
            str(current.get(field, "") or "").startswith("=")
            for field in crm_sheets.FOLLOWUP_HEADERS
        )
        operator_content_by_id[stable_id] = any(
            _followup_row_has_operator_content(
                snapshot,
                row_number=row_number,
                crm_sheets=crm_sheets,
            )
            for row_number in row_numbers
        )
        malformed_baseline_rows.extend(
            row_number
            for row_number in row_numbers
            if _followup_baseline_is_malformed(
                snapshot.row_dict(row_number),
                crm_sheets=crm_sheets,
            )
        )
    sheet_rows_by_id = {
        str(row.get(crm_sheets.COL_ACTION_ID, "")).strip(): row
        for row in sheet_human_rows
        if str(row.get(crm_sheets.COL_ACTION_ID, "")).strip()
    }
    sheet_action_ids = set(sheet_rows_by_id)
    valid_sheet_action_ids = {
        value for value in sheet_action_ids if _canonical_uuid(value) == value
    }
    invalid_action_ids = sheet_action_ids - valid_sheet_action_ids
    actions = {
        str(action.id): action
        for action in OpportunityAction.objects.select_related(
            "opportunity__owner",
        ).filter(pk__in=valid_sheet_action_ids)
    }
    due = tuple(dict(row) for row in due_rows)
    due_ids = {
        str(row.get(crm_sheets.COL_ACTION_ID, "")).strip()
        for row in due
        if str(row.get(crm_sheets.COL_ACTION_ID, "")).strip()
    }
    retained = []
    foreign_owner = 0
    foreign_owner_material = 0
    baseline_divergent = 0
    unknown_material = 0
    unknown = 0
    invalid_material = 0
    safe_retire_action_ids = []
    linked_blocked_owners: dict[str, int] = {}
    for action_id, sheet_row in sheet_rows_by_id.items():
        if action_id in invalid_action_ids:
            if _followup_row_is_material(
                sheet_row,
                crm_sheets=crm_sheets,
                has_managed_formula=managed_formula_by_id.get(action_id, False),
                has_operator_content=operator_content_by_id.get(action_id, False),
            ):
                invalid_material += 1
            else:
                safe_retire_action_ids.append(action_id)
            continue
        action = actions.get(action_id)
        if action is None:
            unknown += 1
            if _unknown_followup_row_is_material(
                current_row_by_id[action_id],
                crm_sheets=crm_sheets,
                has_managed_formula=managed_formula_by_id.get(action_id, False),
                has_operator_content=operator_content_by_id.get(action_id, False),
            ):
                unknown_material += 1
            else:
                safe_retire_action_ids.append(action_id)
            continue
        action_owner = (
            action.opportunity.owner.handle
            if action.opportunity.owner_id
            else ""
        )
        if action_owner != owner:
            foreign_owner += 1
            database_values = _followup_human_db_row(
                action,
                crm_sheets=crm_sheets,
            )
            baseline_was_divergent = _followup_baseline_diverged(
                current_row_by_id[action_id],
                expected=action.sheet_human_snapshot,
                crm_sheets=crm_sheets,
            )
            if baseline_was_divergent:
                baseline_divergent += 1
            merge = crm_sheets.merge_human_fields(
                sheet_values=sheet_row,
                database_values=database_values,
                baseline_values=(dict(action.sheet_human_snapshot or {}) or None),
                human_fields=crm_sheets.FOLLOWUP_HUMAN_FIELDS,
            )
            if (
                merge.imports
                or merge.conflicts
                or managed_formula_by_id.get(action_id, False)
                or operator_content_by_id.get(action_id, False)
                or baseline_was_divergent
            ):
                foreign_owner_material += 1
                if action_owner:
                    linked_blocked_owners[action_owner] = (
                        linked_blocked_owners.get(action_owner, 0) + 1
                    )
            else:
                safe_retire_action_ids.append(action_id)
            continue
        if action_id not in due_ids:
            retained.append(_followup_human_db_row(action, crm_sheets=crm_sheets))

    all_action_ids = due_ids | {
        str(row[crm_sheets.COL_ACTION_ID]) for row in retained
    }
    all_actions = {
        str(action.id): action
        for action in OpportunityAction.objects.filter(pk__in=all_action_ids)
    }
    baselines = {
        action_id: dict(action.sheet_human_snapshot or {})
        for action_id, action in all_actions.items()
    }
    duplicate_action_id_rows = sum(
        len(duplicate.rows) for duplicate in snapshot.duplicate_keys
    )
    unkeyed_nonempty_action_rows = len(snapshot.unkeyed_nonempty_rows)
    local_validation_blockers = (
        duplicate_action_id_rows
        + len(malformed_baseline_rows)
        + unkeyed_nonempty_action_rows
    )
    telemetry = {
        "sheet_action_rows": len(sheet_action_ids),
        "sheet_action_keyed_rows": sum(
            len(rows) for rows in snapshot.rows_by_key.values()
        ),
        "duplicate_action_id_groups": len(snapshot.duplicate_keys),
        "duplicate_action_id_rows": duplicate_action_id_rows,
        "unkeyed_nonempty_action_rows": unkeyed_nonempty_action_rows,
        "malformed_baseline_action_rows": len(malformed_baseline_rows),
        "invalid_action_id_rows": len(invalid_action_ids),
        "invalid_material_action_rows": invalid_material,
        "due_now_rows": len(due),
        "retained_canonical_rows_considered": len(retained),
        "unknown_action_rows": unknown,
        "unknown_material_action_rows": unknown_material,
        "foreign_owner_action_rows": foreign_owner,
        "foreign_owner_material_action_rows": foreign_owner_material,
        "baseline_divergent_action_rows": baseline_divergent,
        "safe_retired_action_rows": len(safe_retire_action_ids),
        "operator_content_action_rows": sum(operator_content_by_id.values()),
        "_safe_retire_action_ids": tuple(safe_retire_action_ids),
        "_local_validation_blockers": local_validation_blockers,
        "_linked_blocked_owners": linked_blocked_owners,
    }
    return (*due, *retained), baselines, telemetry


def _canonical_uuid(value: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return ""


def _followup_baseline_is_malformed(row, *, crm_sheets) -> bool:
    """Validate the portable baseline without exposing its human contents."""
    raw = str(row.get(crm_sheets.COL_HUMAN_BASELINE, "") or "").strip()
    if not raw:
        return False
    try:
        return not isinstance(json.loads(raw), dict)
    except json.JSONDecodeError:
        return True


def _followup_baseline_diverged(row, *, expected, crm_sheets) -> bool:
    """Detect semantic edits to a published portable human baseline."""
    raw = str(row.get(crm_sheets.COL_HUMAN_BASELINE, "") or "").strip()
    if not raw:
        return bool(expected)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return True
    if not isinstance(parsed, dict):
        return True
    actual = {
        field: str(parsed.get(field, "") or "")
        for field in crm_sheets.FOLLOWUP_HUMAN_FIELDS
    }
    published = {
        field: str((expected or {}).get(field, "") or "")
        for field in crm_sheets.FOLLOWUP_HUMAN_FIELDS
    }
    return actual != published


def _followup_row_has_operator_content(
    snapshot,
    *,
    row_number: int,
    crm_sheets,
) -> bool:
    """Detect content outside managed headers, including blank header cells."""
    raw_row = snapshot.rows[row_number - 2]
    managed = set(crm_sheets.FOLLOWUP_HEADERS)
    for index, value in enumerate(raw_row):
        header = (
            snapshot.live_headers[index]
            if index < len(snapshot.live_headers)
            else ""
        )
        if header not in managed and str(value or "").strip():
            return True
    return False


def _followup_row_is_material(
    row,
    *,
    crm_sheets,
    has_managed_formula: bool,
    has_operator_content: bool,
) -> bool:
    return bool(
        has_managed_formula
        or has_operator_content
        or any(
            str(row.get(field, "") or "").strip()
            for field in crm_sheets.FOLLOWUP_HUMAN_FIELDS
        )
    )


def _unknown_followup_row_is_material(
    row,
    *,
    crm_sheets,
    has_managed_formula: bool,
    has_operator_content: bool,
) -> bool:
    """Use the portable row baseline when its DB Action no longer exists."""
    if has_managed_formula or has_operator_content:
        return True
    raw_baseline = str(row.get(crm_sheets.COL_HUMAN_BASELINE, "") or "").strip()
    baseline = None
    if raw_baseline:
        try:
            parsed = json.loads(raw_baseline)
        except json.JSONDecodeError:
            return True
        if not isinstance(parsed, dict):
            return True
        baseline = {
            field: str(parsed.get(field, "") or "")
            for field in crm_sheets.FOLLOWUP_HUMAN_FIELDS
        }
    database_values = baseline or {
        field: "" for field in crm_sheets.FOLLOWUP_HUMAN_FIELDS
    }
    merge = crm_sheets.merge_human_fields(
        sheet_values=row,
        database_values=database_values,
        baseline_values=baseline,
        human_fields=crm_sheets.FOLLOWUP_HUMAN_FIELDS,
    )
    return bool(merge.imports or merge.conflicts)


def _followup_human_db_row(action, *, crm_sheets) -> dict:
    """Serialize only DB-backed human fields for a retained Action row."""
    from linkedin.crm_publish import followup_db_human_values

    return {
        crm_sheets.COL_ACTION_ID: str(action.id),
        **followup_db_human_values(action),
    }


def _retire_safe_followup_rows(
    plan,
    *,
    ws,
    action_ids,
    crm_sheets,
    snapshot=None,
) -> None:
    """Clear managed cells for stale rows proven free of human edits.

    Owner reassignment otherwise leaves the same Action UUID on both sender
    tabs forever.  Unknown columns, formatting, and comments remain untouched;
    a row with any managed formula or divergent human field never reaches this
    helper because `_followup_plan_payload` classifies it as a blocker.
    """
    retiring = {str(value) for value in action_ids if str(value)}
    if not retiring:
        return
    if snapshot is None:
        snapshot = crm_sheets.SheetSnapshot.read(
            ws,
            required_headers=crm_sheets.FOLLOWUP_HEADERS,
            key_header=crm_sheets.COL_ACTION_ID,
        )
    existing_changes = {
        (change.row, change.column)
        for change in plan.changes
    }
    for action_id in retiring:
        for row_number in snapshot.rows_by_key.get(action_id, ()):
            current = snapshot.row_dict(row_number)
            for column in crm_sheets.FOLLOWUP_HEADERS:
                old_value = current.get(column, "")
                if not old_value or (row_number, column) in existing_changes:
                    continue
                plan.changes.append(
                    crm_sheets.CellChange(
                        row=row_number,
                        column=column,
                        old_value=old_value,
                        new_value="",
                        kind="clear",
                        stable_id=action_id,
                    )
                )
                existing_changes.add((row_number, column))
