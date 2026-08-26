"""Safely reconcile and publish the concise account-first CRM v2 workbook.

This command intentionally does not refresh Gmail, Gemini, or Granola and it
never sends outreach.  Its default mode executes the exact database mutation
path inside a rolled-back transaction and performs no Sheet writes.  Apply is
gated by a recent private preview whose inputs and aggregate admission results
still match the recomputed evidence universe.
"""
from __future__ import annotations

import hashlib
import io
import json
import stat
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Mapping
from uuid import UUID, uuid4

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from linkedin.exceptions import SheetsError


_PREVIEW_SCHEMA = "openoutreach.crm-v2-preview.v1"
_PREVIEW_MAX_AGE = timedelta(hours=24)
_PREVIEW_CLOCK_SKEW = timedelta(minutes=5)
_DEFAULT_CONTEXT_MAX_AGE_HOURS = 48
_SALES_OWNERS = ("Arian", "Athena", "Chuka", "Leili")
_OBSOLETE_STATIC_TABS = ("Opportunities", "Pipeline", "Recovery")


@dataclass(frozen=True)
class _CutoverState:
    spreadsheet: Any
    token: str
    new_sheet_ids: Mapping[str, int]
    archived_sheet_ids: Mapping[str, int]
    archived_titles: Mapping[str, str]
    cleanup_sheet_ids: tuple[int, ...]


class Command(BaseCommand):
    help = (
        "Reconcile and publish only Active Accounts and Actions. Defaults to "
        "a no-write, rollback-only dry-run and never sends outreach."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Persist DB changes and publish the reviewed v2 Sheet cutover.",
        )
        parser.add_argument(
            "--routine",
            action="store_true",
            help=(
                "Apply an already-cut-over v2 workbook without a preview; "
                "requires both canonical tabs and no legacy canonical tabs."
            ),
        )
        parser.add_argument(
            "--reviewed-preview",
            default="",
            metavar="PATH",
            help=(
                "Recent mode-private preview_crm_v2 JSON required by --apply."
            ),
        )
        parser.add_argument(
            "--sales-motion-account",
            action="append",
            default=[],
            help="Explicit Sales Motion account name; repeatable.",
        )
        parser.add_argument(
            "--manual-pin",
            action="append",
            default=[],
            help="Explicit human account pin; repeatable.",
        )
        parser.add_argument(
            "--owner-override",
            action="append",
            default=[],
            metavar="ACCOUNT=OWNER",
            help="Assign an exact owner for this refresh; repeatable.",
        )
        parser.add_argument(
            "--skip-sales-motion",
            action="store_true",
            help="Do not read authoritative account tabs from Sales Motion.",
        )
        parser.add_argument(
            "--backup-dir",
            default="artifacts/crm-backups",
            help="Gitignored directory for the private full-workbook backup.",
        )
        parser.add_argument(
            "--context-max-age-hours",
            type=int,
            default=_DEFAULT_CONTEXT_MAX_AGE_HOURS,
            help="Age threshold reported for stored Gmail/Gemini context.",
        )

    def handle(self, *args, **options):
        from linkedin.crm_lock import (
            CrmRefreshAlreadyRunning,
            crm_refresh_lock,
        )

        if options["context_max_age_hours"] <= 0:
            raise CommandError("--context-max-age-hours must be positive")
        apply = bool(options["apply"])
        routine = bool(options["routine"])
        self._pending_cutover = None
        if routine and not apply:
            raise CommandError("--routine is only valid with --apply")
        if routine and options["reviewed_preview"]:
            raise CommandError(
                "--routine cannot be combined with --reviewed-preview"
            )
        if apply and not routine and not options["reviewed_preview"]:
            raise CommandError("--apply requires --reviewed-preview PATH")
        if not apply and options["reviewed_preview"]:
            raise CommandError("--reviewed-preview is only valid with --apply")

        try:
            with crm_refresh_lock():
                try:
                    with transaction.atomic():
                        report = self._run(options, apply=apply)
                        if not apply:
                            transaction.set_rollback(True)
                except Exception:
                    if self._pending_cutover is not None:
                        _compensate_cutover(self._pending_cutover)
                        self._pending_cutover = None
                    raise
                if apply and self._pending_cutover is not None:
                    cleanup = _cleanup_archives_after_commit(
                        self._pending_cutover,
                    )
                    report["publication"]["archive_cleanup"] = cleanup
                    self._pending_cutover = None
        except CrmRefreshAlreadyRunning as exc:
            raise CommandError(str(exc)) from exc
        except SheetsError as exc:
            raise CommandError(str(exc)) from exc

        # One aggregate JSON document is the entire stdout contract.  It never
        # includes account/contact names, email addresses, or stable IDs.
        self.stdout.write(json.dumps(report, sort_keys=True, default=str))

    def _run(self, options: Mapping[str, Any], *, apply: bool) -> dict[str, Any]:
        from crm.models import SalesOwner
        from linkedin import conf
        from linkedin.crm_sheet_import import (
            apply_followup_imports,
            apply_opportunity_imports,
            commit_followup_baselines,
            commit_sheet_baselines,
            read_people_dont_send_lead_ids,
        )
        from linkedin.crm_v2_actions import apply_action_reconciliation
        from linkedin.crm_v2_evidence import collect_account_evidence
        from linkedin.crm_v2_reconcile import apply_reconciliation
        from linkedin.crm_pipeline_policy import reconcile_pipeline_triage
        from linkedin.crm_v2_view_builder import build_crm_v2_database_view
        from linkedin.management.commands.preview_crm_v2 import (
            _configured_sales_motion_accounts,
            _parse_owner_overrides,
        )
        from linkedin.management.commands.sync_sheets import run_people_sync
        from linkedin.notifications import crm_sheets, crm_v2_sheets, sheets
        from linkedin.notifications.crm_v2_layout import apply_layout

        evaluated_at = timezone.now()
        sales_motion_accounts = set(options["sales_motion_account"])
        if not options["skip_sales_motion"]:
            sales_motion_accounts.update(_configured_sales_motion_accounts())
        manual_pins = sorted({
            " ".join(str(value or "").split())
            for value in options["manual_pin"]
            if " ".join(str(value or "").split())
        }, key=str.casefold)
        owner_overrides = _parse_owner_overrides(options["owner_override"])
        resolved_inputs = {
            "sales_motion_accounts": sorted(sales_motion_accounts, key=str.casefold),
            "manual_pins": manual_pins,
            "owner_overrides": dict(sorted(owner_overrides.items())),
        }

        preview = None
        if apply and not options["routine"]:
            preview = _load_reviewed_preview(
                Path(options["reviewed_preview"]),
                now=evaluated_at,
            )
            _assert_preview_inputs_match(preview, resolved_inputs)

        try:
            spreadsheet = sheets._gspread_client()
        except Exception as exc:
            raise SheetsError("CRM v2 workbook could not be opened") from exc
        if str(getattr(spreadsheet, "id", "")) != str(conf.GOOGLE_SHEETS_ID):
            raise SheetsError("CRM v2 publisher opened an unexpected workbook")
        worksheets = _worksheet_inventory(spreadsheet, crm_sheets=crm_sheets)
        existing_mode = _publication_mode(worksheets, crm_v2_sheets=crm_v2_sheets)
        if options["routine"]:
            if existing_mode != "in_place":
                raise SheetsError(
                    "--routine requires both canonical CRM v2 tabs"
                )
            if _obsolete_tab_count(worksheets, crm_sheets=crm_sheets):
                raise SheetsError(
                    "--routine requires all legacy canonical CRM tabs to be absent"
                )
        try:
            people_result = run_people_sync(
                dry_run=not apply,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
                lock_held=True,
            )
        except Exception as exc:
            raise SheetsError("People publisher prerequisite failed") from exc
        people_blocked = bool(
            int(people_result.get("errored", 0) or 0)
            or int(people_result.get("duplicate_lead_ids", 0) or 0)
        )
        if people_blocked:
            raise SheetsError(
                "People publisher reported errors or duplicate stable Lead IDs"
            )
        # People is the safety ledger, not an admission source.  Resolve its
        # exact stable Lead IDs before any evidence decision and fail closed if
        # the tab, headers, or identities cannot be read safely.
        dont_send_lead_ids = read_people_dont_send_lead_ids(spreadsheet)

        initial_evidence = collect_account_evidence(
            sales_motion_accounts=resolved_inputs["sales_motion_accounts"],
            manual_account_pins=resolved_inputs["manual_pins"],
            owner_overrides=resolved_inputs["owner_overrides"],
            dont_send_lead_ids=dont_send_lead_ids,
            now=evaluated_at,
        )
        if preview is not None:
            _assert_preview_summary_matches(preview, initial_evidence)

        context = _context_health(
            now=evaluated_at,
            max_age_hours=options["context_max_age_hours"],
            preview_generated_at=(preview["_generated_at"] if preview else None),
        )

        reconcile_report = apply_reconciliation(
            initial_evidence,
            evaluated_at=evaluated_at,
        )
        _assert_reconciliation_safe(reconcile_report)

        preliminary_plans = None
        import_reports = {
            "active_account_edits": 0,
            "action_edits": 0,
            "legacy_opportunity_edits": 0,
            "legacy_followup_edits": 0,
            "legacy_unresolved_rows": 0,
            "invalid_edits": 0,
        }
        retained_legacy_archives: set[str] = set()
        if existing_mode == "first_cutover":
            legacy = _import_legacy_human_state(
                worksheets,
                crm_sheets=crm_sheets,
                apply_opportunity_imports=apply_opportunity_imports,
                apply_followup_imports=apply_followup_imports,
                evaluated_at=evaluated_at,
            )
            import_reports.update({
                "legacy_opportunity_edits": legacy["opportunity_edits"],
                "legacy_followup_edits": legacy["followup_edits"],
                "legacy_unresolved_rows": legacy["unresolved_rows"],
            })
            retained_legacy_archives = set(legacy["retain_archive_titles"])
            if legacy["opportunity_edits"] or legacy["followup_edits"]:
                refreshed = collect_account_evidence(
                    sales_motion_accounts=resolved_inputs["sales_motion_accounts"],
                    manual_account_pins=resolved_inputs["manual_pins"],
                    owner_overrides=resolved_inputs["owner_overrides"],
                    dont_send_lead_ids=dont_send_lead_ids,
                    now=evaluated_at,
                )
                refreshed_reconcile = apply_reconciliation(
                    refreshed,
                    evaluated_at=evaluated_at,
                )
                _assert_reconciliation_safe(refreshed_reconcile)
        # On the first cutover, exact legacy human edits must be imported before
        # v2 creates any generated Action.  Otherwise a newly generated current
        # Action can make the older human next step look invalid even though the
        # human edit is authoritative.  Routine runs already import from the v2
        # tabs below and therefore keep their existing two-pass flow.
        reconciled_evidence, triage_report, action_report, database_view = _refresh_database_view(
            collect_account_evidence=collect_account_evidence,
            apply_reconciliation=apply_reconciliation,
            reconcile_pipeline_triage=reconcile_pipeline_triage,
            apply_action_reconciliation=apply_action_reconciliation,
            build_crm_v2_database_view=build_crm_v2_database_view,
            resolved_inputs=resolved_inputs,
            dont_send_lead_ids=dont_send_lead_ids,
            evaluated_at=evaluated_at,
        )
        triage_reports = [triage_report]
        action_reports = [action_report]
        if existing_mode == "in_place":
            preliminary_plans = _build_plans(
                worksheets[crm_v2_sheets.ACTIVE_ACCOUNTS_TAB],
                worksheets[crm_v2_sheets.ACTIONS_TAB],
                database_view,
                crm_v2_sheets=crm_v2_sheets,
            )
            _assert_cross_plan_imports_consistent(
                preliminary_plans[0],
                preliminary_plans[1],
            )
            active_import = apply_opportunity_imports(
                preliminary_plans[0].imports,
                dry_run=False,
                now=evaluated_at,
            )
            action_import = apply_followup_imports(
                preliminary_plans[1].imports,
                dry_run=False,
                now=evaluated_at,
            )
            invalid_count = len(active_import.invalid) + len(action_import.invalid)
            if invalid_count:
                raise SheetsError(
                    f"CRM v2 has {invalid_count} invalid human Sheet edit(s)"
                )
            import_reports = {
                "active_account_edits": active_import.fields_imported,
                "action_edits": action_import.fields_imported,
                "legacy_opportunity_edits": 0,
                "legacy_followup_edits": 0,
                "legacy_unresolved_rows": 0,
                "invalid_edits": 0,
            }
            if active_import.fields_imported or action_import.fields_imported:
                # Human edits are authoritative.  Recollect and fully re-run
                # the evidence/reconciliation/action path before planning any
                # writes.  A no-import run keeps the first exact pass so its
                # mutation telemetry is not overwritten by a duplicate no-op.
                fresh = collect_account_evidence(
                    sales_motion_accounts=resolved_inputs["sales_motion_accounts"],
                    manual_account_pins=resolved_inputs["manual_pins"],
                    owner_overrides=resolved_inputs["owner_overrides"],
                    dont_send_lead_ids=dont_send_lead_ids,
                    now=evaluated_at,
                )
                imported_reconcile = apply_reconciliation(
                    fresh,
                    evaluated_at=evaluated_at,
                )
                _assert_reconciliation_safe(imported_reconcile)
                reconciled_evidence, triage_report, action_report, database_view = _refresh_database_view(
                    collect_account_evidence=collect_account_evidence,
                    apply_reconciliation=apply_reconciliation,
                    reconcile_pipeline_triage=reconcile_pipeline_triage,
                    apply_action_reconciliation=apply_action_reconciliation,
                    build_crm_v2_database_view=build_crm_v2_database_view,
                    resolved_inputs=resolved_inputs,
                    dont_send_lead_ids=dont_send_lead_ids,
                    evaluated_at=evaluated_at,
                )
                triage_reports.append(triage_report)
                action_reports.append(action_report)

        active_plan = action_plan = None
        if existing_mode == "in_place":
            active_plan, action_plan = _build_plans(
                worksheets[crm_v2_sheets.ACTIVE_ACCOUNTS_TAB],
                worksheets[crm_v2_sheets.ACTIONS_TAB],
                database_view,
                crm_v2_sheets=crm_v2_sheets,
            )
            if active_plan.imports or action_plan.imports:
                raise SheetsError(
                    "CRM v2 human imports remained after fresh database re-plan"
                )

        report: dict[str, Any] = {
            "schema": "openoutreach.crm-v2-refresh.v1",
            "mode": "apply" if apply else "dry-run",
            "status": "applied" if apply else "planned",
            "sends_performed": 0,
            "context": context,
            "people": _people_publish_counts(people_result),
            "people_dont_send_leads": len(dont_send_lead_ids),
            "publication": {
                "mode": existing_mode,
                "gate": (
                    "routine" if options["routine"]
                    else "reviewed" if apply
                    else "dry-run"
                ),
                "managed_tabs": 2,
                "obsolete_tabs_present": _obsolete_tab_count(
                    worksheets,
                    crm_sheets=crm_sheets,
                ),
            },
            "evidence": _evidence_counts(reconciled_evidence),
            "reconciliation": _reconciliation_counts(reconcile_report),
            "pipeline_triage": _pipeline_triage_counts(
                triage_report,
                reports=triage_reports,
            ),
            "actions": _action_counts(
                action_report,
                reports=action_reports,
            ),
            "human_imports": import_reports,
            "sheet_plan": _sheet_plan_counts(
                active_plan,
                action_plan,
                database_view=database_view,
                first_cutover=(existing_mode == "first_cutover"),
            ),
            "backup": {
                "required": apply,
                "created": False,
                "full_workbook": True,
            },
            "baselines": {"committed": False, "rows": 0},
        }
        if not apply:
            return report

        # All database work remains provisional in Command.handle's outer
        # transaction.  This full, private backup is the last gate before the
        # first structural Sheet write.
        crm_sheets.backup_spreadsheet(
            spreadsheet,
            Path(options["backup_dir"]),
            prefix="crm-v2-before-apply",
        )
        report["backup"]["created"] = True
        owner_values = ("Unassigned", *tuple(
            SalesOwner.objects.filter(active=True)
            .order_by("normalized_handle")
            .values_list("handle", flat=True)
        ))

        if existing_mode == "first_cutover":
            active_plan, action_plan, cutover_state = _apply_first_cutover(
                spreadsheet,
                worksheets=worksheets,
                database_view=database_view,
                crm_sheets=crm_sheets,
                crm_v2_sheets=crm_v2_sheets,
                apply_layout=apply_layout,
                owner_values=owner_values,
                retain_archive_titles=retained_legacy_archives,
            )
            self._pending_cutover = cutover_state
            report["publication"]["atomic_cutover"] = True
            report["publication"]["obsolete_tabs_archived"] = (
                _obsolete_tab_count(worksheets, crm_sheets=crm_sheets)
            )
            report["publication"]["obsolete_tabs_deleted"] = 0
        else:
            assert active_plan is not None and action_plan is not None
            active_plan, action_plan, cutover_state = _apply_in_place_staged(
                spreadsheet,
                active_ws=worksheets[crm_v2_sheets.ACTIVE_ACCOUNTS_TAB],
                actions_ws=worksheets[crm_v2_sheets.ACTIONS_TAB],
                database_view=database_view,
                crm_sheets=crm_sheets,
                crm_v2_sheets=crm_v2_sheets,
                apply_layout=apply_layout,
                owner_values=owner_values,
            )
            self._pending_cutover = cutover_state
            report["publication"]["atomic_cutover"] = True
            report["publication"]["obsolete_tabs_archived"] = 0
            report["publication"]["obsolete_tabs_deleted"] = 0

        published_at = timezone.now()
        active_baselines = commit_sheet_baselines(
            active_plan.baseline_updates,
            published_at=published_at,
        )
        action_baselines = commit_followup_baselines(
            action_plan.baseline_updates,
            published_at=published_at,
        )
        report["baselines"] = {
            "committed": True,
            "rows": active_baselines + action_baselines,
        }
        report["sheet_plan"] = _sheet_plan_counts(
            active_plan,
            action_plan,
            database_view=database_view,
            first_cutover=(existing_mode == "first_cutover"),
        )
        return report


def _refresh_database_view(
    *,
    collect_account_evidence,
    apply_reconciliation,
    reconcile_pipeline_triage,
    apply_action_reconciliation,
    build_crm_v2_database_view,
    resolved_inputs,
    dont_send_lead_ids,
    evaluated_at,
):
    reconciled = collect_account_evidence(
        sales_motion_accounts=resolved_inputs["sales_motion_accounts"],
        manual_account_pins=resolved_inputs["manual_pins"],
        owner_overrides=resolved_inputs["owner_overrides"],
        dont_send_lead_ids=dont_send_lead_ids,
        now=evaluated_at,
    )
    reconciliation = apply_reconciliation(
        reconciled,
        evaluated_at=evaluated_at,
    )
    _assert_reconciliation_safe(reconciliation)
    with_ids = collect_account_evidence(
        sales_motion_accounts=resolved_inputs["sales_motion_accounts"],
        manual_account_pins=resolved_inputs["manual_pins"],
        owner_overrides=resolved_inputs["owner_overrides"],
        dont_send_lead_ids=dont_send_lead_ids,
        now=evaluated_at,
    )
    triage_report = reconcile_pipeline_triage(
        with_ids,
        apply=True,
        evaluated_at=evaluated_at,
    )
    if triage_report.issues:
        raise SheetsError(
            f"CRM pipeline triage has {len(triage_report.issues)} "
            "stable-identity issue(s)"
        )
    action_report = apply_action_reconciliation(
        with_ids,
        evaluated_at=evaluated_at,
    )
    if action_report.issues:
        raise SheetsError(
            f"CRM v2 action reconciliation has {len(action_report.issues)} "
            "identity/provenance issue(s)"
        )
    post_action = collect_account_evidence(
        sales_motion_accounts=resolved_inputs["sales_motion_accounts"],
        manual_account_pins=resolved_inputs["manual_pins"],
        owner_overrides=resolved_inputs["owner_overrides"],
        dont_send_lead_ids=dont_send_lead_ids,
        now=evaluated_at,
    )
    return (
        post_action,
        triage_report,
        action_report,
        build_crm_v2_database_view(post_action),
    )


def _build_plans(active_ws, actions_ws, database_view, *, crm_v2_sheets):
    active_plan = crm_v2_sheets.active_accounts_adapter(active_ws).plan(
        database_view.rows.active_accounts,
        baseline_by_id=database_view.active_baselines,
    )
    action_plan = crm_v2_sheets.actions_adapter(actions_ws).plan(
        database_view.rows.actions,
        baseline_by_id=database_view.action_baselines,
    )
    _assert_plan_safe(active_plan)
    _assert_plan_safe(action_plan)
    return active_plan, action_plan


def _assert_plan_safe(plan) -> None:
    if plan.conflicts:
        raise SheetsError(
            f"CRM v2 tab has {len(plan.conflicts)} human merge conflict(s)"
        )
    if plan.duplicate_keys:
        raise SheetsError("CRM v2 tab contains duplicate stable IDs")
    if plan.unkeyed_nonempty_rows:
        raise SheetsError(
            f"CRM v2 tab has {len(plan.unkeyed_nonempty_rows)} unkeyed row(s)"
        )


def _assert_cross_plan_imports_consistent(active_plan, action_plan) -> None:
    """Reject contradictory shared human edits across account/action tabs."""
    from crm.models import OpportunityAction
    from linkedin.notifications import crm_v2_sheets

    shared_fields = {
        crm_v2_sheets.COL_WAITING_UNTIL,
        crm_v2_sheets.COL_MANUAL_PIN,
    }
    active_values = {
        (str(item.stable_id), item.field): str(item.value)
        for item in active_plan.imports
        if item.field in shared_fields
    }
    action_values = {
        (str(item.stable_id), item.field): str(item.value)
        for item in action_plan.imports
        if item.field in shared_fields
    }
    if not active_values or not action_values:
        return
    action_ids = {action_id for action_id, _field in action_values}
    action_to_opportunity = {
        str(action_id): str(opportunity_id)
        for action_id, opportunity_id in (
            OpportunityAction.objects.filter(id__in=action_ids)
            .values_list("id", "opportunity_id")
        )
    }
    for (action_id, field), action_value in action_values.items():
        opportunity_id = action_to_opportunity.get(action_id)
        if opportunity_id is None:
            continue
        active_value = active_values.get((opportunity_id, field))
        if active_value is not None and active_value != action_value:
            raise SheetsError(
                "CRM has contradictory shared human edits across tabs"
            )


def _import_legacy_human_state(
    worksheets,
    *,
    crm_sheets,
    apply_opportunity_imports,
    apply_followup_imports,
    evaluated_at,
) -> dict[str, Any]:
    """Import only exact stable-ID human edits before archiving legacy tabs."""
    from crm.models import Opportunity, OpportunityAction
    from linkedin.crm_publish import followup_db_human_values
    from linkedin.crm_sheet_import import baseline_by_opportunity_id

    opportunity_plan = None
    unresolved_by_title: dict[str, int] = {}
    opportunity_ws = worksheets.get(crm_sheets.OPPORTUNITIES_TAB)
    if opportunity_ws is not None:
        snapshot = crm_sheets.SheetSnapshot.read(
            opportunity_ws,
            required_headers=crm_sheets.OPPORTUNITY_HEADERS,
            key_header=crm_sheets.COL_OPPORTUNITY_ID,
        )
        _assert_snapshot_safe(snapshot)
        valid_ids = _valid_uuid_strings(snapshot.rows_by_key)
        opportunities = list(
            Opportunity.objects.filter(id__in=valid_ids)
            .select_related("account", "owner")
            .prefetch_related("contacts", "actions")
            .order_by("id")
        )
        desired = []
        for opportunity in opportunities:
            current_action = next(
                (
                    action for action in opportunity.actions.all()
                    if action.status in {
                        OpportunityAction.Status.OPEN,
                        OpportunityAction.Status.WAITING,
                    }
                ),
                None,
            )
            desired.append(crm_sheets.opportunity_to_sheet_row(
                opportunity,
                action=current_action,
                synced_at=evaluated_at,
            ))
        opportunity_plan = crm_sheets.OpportunitySheetAdapter(
            opportunity_ws
        ).plan(
            desired,
            baseline_by_id=baseline_by_opportunity_id(),
        )
        _assert_plan_safe(opportunity_plan)
        unresolved = len(set(snapshot.rows_by_key) - {
            str(opportunity.id) for opportunity in opportunities
        })
        if unresolved:
            unresolved_by_title[crm_sheets.OPPORTUNITIES_TAB] = unresolved

    followup_plans = []
    seen_action_ids: set[str] = set()
    for owner in _SALES_OWNERS:
        title = crm_sheets.sender_followups_tab(owner)
        worksheet = worksheets.get(title)
        if worksheet is None:
            continue
        snapshot = crm_sheets.SheetSnapshot.read(
            worksheet,
            required_headers=crm_sheets.FOLLOWUP_HEADERS,
            key_header=crm_sheets.COL_ACTION_ID,
        )
        _assert_snapshot_safe(snapshot)
        duplicate_across_tabs = set(snapshot.rows_by_key) & seen_action_ids
        if duplicate_across_tabs:
            raise SheetsError(
                "Legacy Followups contain duplicate Action IDs across sender tabs"
            )
        seen_action_ids.update(snapshot.rows_by_key)
        valid_ids = _valid_uuid_strings(snapshot.rows_by_key)
        actions = list(
            OpportunityAction.objects.filter(id__in=valid_ids)
            .select_related("opportunity")
            .order_by("id")
        )
        desired = [
            {
                crm_sheets.COL_ACTION_ID: str(action.id),
                **followup_db_human_values(action),
            }
            for action in actions
        ]
        # An absent DB snapshot is meaningfully different from an empty
        # snapshot: omitting it lets the adapter fall back to the portable
        # baseline embedded in the legacy row.  Supplying ``{}`` here would
        # incorrectly make every populated human field look like a fresh edit.
        baselines = {
            str(action.id): dict(action.sheet_human_snapshot)
            for action in actions
            if action.sheet_human_snapshot
        }
        plan = crm_sheets.followups_adapter(worksheet).plan(
            desired,
            remove_missing=False,
            baseline_by_id=baselines,
        )
        _assert_plan_safe(plan)
        unresolved = len(set(snapshot.rows_by_key) - {
            str(action.id) for action in actions
        })
        if unresolved:
            unresolved_by_title[title] = unresolved
        followup_plans.append(plan)

    combined_followup_imports = [
        item for plan in followup_plans for item in plan.imports
    ]
    if opportunity_plan is not None:
        for plan in followup_plans:
            _assert_cross_plan_imports_consistent(opportunity_plan, plan)
    opportunity_result = apply_opportunity_imports(
        opportunity_plan.imports if opportunity_plan is not None else (),
        dry_run=False,
        now=evaluated_at,
    )
    followup_result = apply_followup_imports(
        combined_followup_imports,
        dry_run=False,
        now=evaluated_at,
    )
    invalid = len(opportunity_result.invalid) + len(followup_result.invalid)
    if invalid:
        raise SheetsError(f"Legacy CRM has {invalid} invalid human edit(s)")
    return {
        "opportunity_edits": opportunity_result.fields_imported,
        "followup_edits": followup_result.fields_imported,
        "unresolved_rows": sum(unresolved_by_title.values()),
        "retain_archive_titles": tuple(sorted(unresolved_by_title)),
    }


def _valid_uuid_strings(values) -> tuple[str, ...]:
    valid = []
    for value in values:
        try:
            valid.append(str(UUID(str(value))))
        except (TypeError, ValueError, AttributeError):
            continue
    return tuple(valid)


def _assert_snapshot_safe(snapshot) -> None:
    if snapshot.header_additions:
        raise SheetsError("Legacy CRM tab is missing required stable/human headers")
    if snapshot.duplicate_keys:
        raise SheetsError("Legacy CRM tab contains duplicate stable IDs")
    if snapshot.unkeyed_nonempty_rows:
        raise SheetsError("Legacy CRM tab contains unkeyed nonempty rows")


def _apply_first_cutover(
    spreadsheet,
    *,
    worksheets,
    database_view,
    crm_sheets,
    crm_v2_sheets,
    apply_layout,
    owner_values,
    retain_archive_titles=(),
):
    token = uuid4().hex[:12]
    active_title = f"_CRM v2 staging active {token}"
    actions_title = f"_CRM v2 staging actions {token}"
    try:
        active_ws = spreadsheet.add_worksheet(
            title=active_title,
            rows=max(1000, len(database_view.rows.active_accounts) + 10),
            cols=len(crm_v2_sheets.ACTIVE_ACCOUNT_HEADERS),
        )
        actions_ws = spreadsheet.add_worksheet(
            title=actions_title,
            rows=max(1000, len(database_view.rows.actions) + 10),
            cols=len(crm_v2_sheets.ACTION_HEADERS),
        )
    except Exception as exc:
        raise SheetsError("CRM v2 staging-tab creation failed") from exc
    active_plan, action_plan = _build_plans(
        active_ws,
        actions_ws,
        database_view,
        crm_v2_sheets=crm_v2_sheets,
    )
    crm_v2_sheets.active_accounts_adapter(active_ws).apply(
        active_plan,
        dry_run=False,
    )
    crm_v2_sheets.actions_adapter(actions_ws).apply(
        action_plan,
        dry_run=False,
    )
    apply_layout(
        spreadsheet,
        active_ws,
        headers=crm_v2_sheets.ACTIVE_ACCOUNT_HEADERS,
        technical_fields=crm_v2_sheets.ACTIVE_ACCOUNT_TECHNICAL_FIELDS,
        owner_values=owner_values,
    )
    apply_layout(
        spreadsheet,
        actions_ws,
        headers=crm_v2_sheets.ACTION_HEADERS,
        technical_fields=crm_v2_sheets.ACTION_TECHNICAL_FIELDS,
        owner_values=owner_values,
    )
    # All value/header/identity readback happens before the destructive title
    # batch.  A failure here leaves every legacy source tab untouched.
    _verify_sheet_payload(
        active_ws,
        headers=crm_v2_sheets.ACTIVE_ACCOUNT_HEADERS,
        key_header=crm_v2_sheets.COL_OPPORTUNITY_ID,
        desired_rows=database_view.rows.active_accounts,
        crm_sheets=crm_sheets,
        exact_headers=True,
        baseline_updates=active_plan.baseline_updates,
    )
    _verify_sheet_payload(
        actions_ws,
        headers=crm_v2_sheets.ACTION_HEADERS,
        key_header=crm_v2_sheets.COL_ACTION_ID,
        desired_rows=database_view.rows.actions,
        crm_sheets=crm_sheets,
        exact_headers=True,
        baseline_updates=action_plan.baseline_updates,
    )

    # Resolve the deletion whitelist from the same pre-cutover inventory.  No
    # fuzzy title match is permitted and protected/non-derived tabs cannot enter
    # this request even if their content resembles a CRM view.
    requests = [
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": active_ws.id,
                    "title": crm_v2_sheets.ACTIVE_ACCOUNTS_TAB,
                    "index": 0,
                },
                "fields": "title,index",
            }
        },
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": actions_ws.id,
                    "title": crm_v2_sheets.ACTIONS_TAB,
                    "index": 1,
                },
                "fields": "title,index",
            }
        },
    ]
    archived_sheet_ids = {}
    archived_titles = {}
    for title in _obsolete_titles(crm_sheets=crm_sheets):
        worksheet = worksheets.get(title)
        if worksheet is not None:
            archive_title = _archive_title(title, token=token)
            archived_sheet_ids[title] = worksheet.id
            archived_titles[title] = archive_title
            requests.append({
                "updateSheetProperties": {
                    "properties": {
                        "sheetId": worksheet.id,
                        "title": archive_title,
                    },
                    "fields": "title",
                }
            })
    try:
        # A single Sheets batchUpdate activates v2 and archives every exact
        # obsolete source.  Nothing is deleted until the enclosing database
        # transaction and baseline commits have succeeded.
        spreadsheet.batch_update({"requests": requests})
    except Exception as exc:
        raise SheetsError("CRM v2 atomic title cutover failed") from exc
    retained = set(retain_archive_titles)
    state = _CutoverState(
        spreadsheet=spreadsheet,
        token=token,
        new_sheet_ids={
            crm_v2_sheets.ACTIVE_ACCOUNTS_TAB: active_ws.id,
            crm_v2_sheets.ACTIONS_TAB: actions_ws.id,
        },
        archived_sheet_ids=archived_sheet_ids,
        archived_titles=archived_titles,
        cleanup_sheet_ids=tuple(
            sheet_id
            for title, sheet_id in archived_sheet_ids.items()
            if title not in retained
        ),
    )
    return active_plan, action_plan, state


def _apply_in_place_staged(
    spreadsheet,
    *,
    active_ws,
    actions_ws,
    database_view,
    crm_sheets,
    crm_v2_sheets,
    apply_layout,
    owner_values,
):
    """Publish both routine surfaces through verified duplicates and one swap.

    Mutating the two canonical worksheets sequentially can expose a mixed
    Active/Actions generation when the second write fails.  Duplicating first
    preserves every formula, unknown operator column, comment, and format while
    keeping both live tabs untouched until one atomic title batch succeeds.
    """
    token = uuid4().hex[:12]
    original_fingerprints = {
        crm_v2_sheets.ACTIVE_ACCOUNTS_TAB: _worksheet_formula_fingerprint(
            active_ws,
            crm_sheets=crm_sheets,
        ),
        crm_v2_sheets.ACTIONS_TAB: _worksheet_formula_fingerprint(
            actions_ws,
            crm_sheets=crm_sheets,
        ),
    }
    try:
        staged_active = spreadsheet.duplicate_sheet(
            active_ws.id,
            new_sheet_name=f"_CRM v2 staging active {token}",
        )
        staged_actions = spreadsheet.duplicate_sheet(
            actions_ws.id,
            new_sheet_name=f"_CRM v2 staging actions {token}",
        )
    except Exception as exc:
        raise SheetsError("CRM v2 routine staging-copy creation failed") from exc

    active_plan, action_plan = _build_plans(
        staged_active,
        staged_actions,
        database_view,
        crm_v2_sheets=crm_v2_sheets,
    )
    if active_plan.imports or action_plan.imports:
        raise SheetsError(
            "CRM v2 source tabs changed while the staged publish was prepared"
        )
    crm_v2_sheets.active_accounts_adapter(staged_active).apply(
        active_plan,
        dry_run=False,
    )
    crm_v2_sheets.actions_adapter(staged_actions).apply(
        action_plan,
        dry_run=False,
    )
    apply_layout(
        spreadsheet,
        staged_active,
        headers=crm_v2_sheets.ACTIVE_ACCOUNT_HEADERS,
        technical_fields=crm_v2_sheets.ACTIVE_ACCOUNT_TECHNICAL_FIELDS,
        owner_values=owner_values,
    )
    apply_layout(
        spreadsheet,
        staged_actions,
        headers=crm_v2_sheets.ACTION_HEADERS,
        technical_fields=crm_v2_sheets.ACTION_TECHNICAL_FIELDS,
        owner_values=owner_values,
    )
    _verify_sheet_payload(
        staged_active,
        headers=crm_v2_sheets.ACTIVE_ACCOUNT_HEADERS,
        key_header=crm_v2_sheets.COL_OPPORTUNITY_ID,
        desired_rows=database_view.rows.active_accounts,
        crm_sheets=crm_sheets,
        exact_headers=False,
        baseline_updates=active_plan.baseline_updates,
    )
    _verify_sheet_payload(
        staged_actions,
        headers=crm_v2_sheets.ACTION_HEADERS,
        key_header=crm_v2_sheets.COL_ACTION_ID,
        desired_rows=database_view.rows.actions,
        crm_sheets=crm_sheets,
        exact_headers=False,
        baseline_updates=action_plan.baseline_updates,
    )
    if original_fingerprints != {
        crm_v2_sheets.ACTIVE_ACCOUNTS_TAB: _worksheet_formula_fingerprint(
            active_ws,
            crm_sheets=crm_sheets,
        ),
        crm_v2_sheets.ACTIONS_TAB: _worksheet_formula_fingerprint(
            actions_ws,
            crm_sheets=crm_sheets,
        ),
    }:
        raise SheetsError("CRM v2 live tabs changed during staged publication")

    archive_titles = {
        crm_v2_sheets.ACTIVE_ACCOUNTS_TAB: _archive_title(
            crm_v2_sheets.ACTIVE_ACCOUNTS_TAB,
            token=token,
        ),
        crm_v2_sheets.ACTIONS_TAB: _archive_title(
            crm_v2_sheets.ACTIONS_TAB,
            token=token,
        ),
    }
    requests = [
        _rename_request(active_ws.id, archive_titles[crm_v2_sheets.ACTIVE_ACCOUNTS_TAB]),
        _rename_request(actions_ws.id, archive_titles[crm_v2_sheets.ACTIONS_TAB]),
        _rename_request(
            staged_active.id,
            crm_v2_sheets.ACTIVE_ACCOUNTS_TAB,
            index=0,
        ),
        _rename_request(
            staged_actions.id,
            crm_v2_sheets.ACTIONS_TAB,
            index=1,
        ),
    ]
    try:
        spreadsheet.batch_update({"requests": requests})
    except Exception as exc:
        raise SheetsError("CRM v2 routine atomic title swap failed") from exc
    state = _CutoverState(
        spreadsheet=spreadsheet,
        token=token,
        new_sheet_ids={
            crm_v2_sheets.ACTIVE_ACCOUNTS_TAB: staged_active.id,
            crm_v2_sheets.ACTIONS_TAB: staged_actions.id,
        },
        archived_sheet_ids={
            crm_v2_sheets.ACTIVE_ACCOUNTS_TAB: active_ws.id,
            crm_v2_sheets.ACTIONS_TAB: actions_ws.id,
        },
        archived_titles=archive_titles,
        cleanup_sheet_ids=(active_ws.id, actions_ws.id),
    )
    return active_plan, action_plan, state


def _worksheet_formula_fingerprint(worksheet, *, crm_sheets) -> str:
    values = crm_sheets._formula_values(worksheet)
    canonical = json.dumps(
        values,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _archive_title(original: str, *, token: str) -> str:
    title = f"_CRM v2 archived {token} {original}"
    if len(title) > 100:
        raise SheetsError("CRM v2 archive title would exceed provider limits")
    return title


def _rename_request(
    sheet_id: int,
    title: str,
    *,
    index: int | None = None,
) -> dict[str, Any]:
    properties: dict[str, Any] = {"sheetId": sheet_id, "title": title}
    fields = ["title"]
    if index is not None:
        properties["index"] = index
        fields.append("index")
    return {
        "updateSheetProperties": {
            "properties": properties,
            "fields": ",".join(fields),
        }
    }


def _compensate_cutover(state: _CutoverState) -> None:
    """Restore both old canonical titles after a post-swap DB failure."""
    failed_token = uuid4().hex[:8]
    requests = []
    for canonical_title, sheet_id in state.new_sheet_ids.items():
        failed_title = f"_CRM v2 failed {failed_token} {canonical_title}"
        requests.append(_rename_request(sheet_id, failed_title))
    for original_title, sheet_id in state.archived_sheet_ids.items():
        requests.append(_rename_request(sheet_id, original_title))
    try:
        state.spreadsheet.batch_update({"requests": requests})
    except Exception as exc:
        raise SheetsError(
            "CRM v2 database apply rolled back but title compensation failed"
        ) from exc


def _cleanup_archives_after_commit(state: _CutoverState) -> dict[str, Any]:
    """Delete archived sources only after the DB transaction has committed."""
    deliberately_retained = (
        len(state.archived_sheet_ids) - len(state.cleanup_sheet_ids)
    )
    if not state.cleanup_sheet_ids:
        return {
            "attempted": False,
            "deleted": 0,
            "retained": deliberately_retained,
        }
    requests = [
        {"deleteSheet": {"sheetId": sheet_id}}
        for sheet_id in state.cleanup_sheet_ids
    ]
    try:
        # One batch is all-or-nothing: a cleanup failure leaves every archive
        # available for manual recovery instead of partially erasing history.
        state.spreadsheet.batch_update({"requests": requests})
    except Exception:
        return {
            "attempted": True,
            "deleted": 0,
            "retained": len(state.archived_sheet_ids),
        }
    return {
        "attempted": True,
        "deleted": len(state.cleanup_sheet_ids),
        "retained": deliberately_retained,
    }


def _verify_sheet_payload(
    worksheet,
    *,
    headers,
    key_header,
    desired_rows,
    crm_sheets,
    exact_headers,
    baseline_updates=(),
) -> None:
    snapshot = crm_sheets.SheetSnapshot.read(
        worksheet,
        required_headers=headers,
        key_header=key_header,
    )
    if snapshot.duplicate_keys:
        raise SheetsError("CRM v2 readback contains duplicate stable IDs")
    if snapshot.unkeyed_nonempty_rows:
        raise SheetsError("CRM v2 readback contains unkeyed nonempty rows")
    if snapshot.header_additions:
        raise SheetsError("CRM v2 readback is missing required headers")
    if exact_headers and snapshot.live_headers != tuple(headers):
        raise SheetsError("CRM v2 staging readback has an unexpected header schema")

    baselines = {
        str(item.stable_id): json.dumps(
            dict(item.values),
            sort_keys=True,
            separators=(",", ":"),
        )
        for item in baseline_updates
    }
    desired = {
        str(row[key_header]): {
            **row,
            **(
                {"Human sync baseline": baselines[str(row[key_header])]}
                if str(row[key_header]) in baselines
                else {}
            ),
        }
        for row in desired_rows
    }
    visible: dict[str, dict[str, str]] = {}
    for stable_id, row_numbers in snapshot.rows_by_key.items():
        row = snapshot.row_dict(row_numbers[0])
        # Account is the first visible managed column on both public surfaces.
        # Missing desired rows keep only their hidden key/baseline so a future
        # reappearance can reuse the same row; they are not visible CRM work.
        if str(row.get(headers[0], "")).strip():
            visible[stable_id] = row
    if set(visible) != set(desired):
        raise SheetsError("CRM v2 readback stable-ID row count does not match the plan")
    for stable_id, desired_row in desired.items():
        actual = visible[stable_id]
        for header in headers:
            if str(actual.get(header, "")) != str(desired_row.get(header, "")):
                raise SheetsError("CRM v2 managed-cell readback does not match the plan")


def _worksheet_inventory(spreadsheet, *, crm_sheets) -> dict[str, Any]:
    worksheets = crm_sheets.retry_sheet_read(
        spreadsheet.worksheets,
        context="failed listing CRM v2 workbook tabs",
    )
    titles = [str(getattr(worksheet, "title", "")) for worksheet in worksheets]
    if len(titles) != len(set(titles)):
        raise SheetsError("CRM workbook contains duplicate tab titles")
    return dict(zip(titles, worksheets))


def _publication_mode(worksheets, *, crm_v2_sheets) -> str:
    present = {
        title for title in (
            crm_v2_sheets.ACTIVE_ACCOUNTS_TAB,
            crm_v2_sheets.ACTIONS_TAB,
        )
        if title in worksheets
    }
    if not present:
        return "first_cutover"
    if len(present) == 2:
        return "in_place"
    raise SheetsError("CRM v2 canonical tabs are only partially present")


def _obsolete_titles(*, crm_sheets) -> tuple[str, ...]:
    return (
        *_OBSOLETE_STATIC_TABS,
        *(crm_sheets.sender_followups_tab(owner) for owner in _SALES_OWNERS),
    )


def _obsolete_tab_count(worksheets, *, crm_sheets) -> int:
    return sum(title in worksheets for title in _obsolete_titles(crm_sheets=crm_sheets))


def _load_reviewed_preview(path: Path, *, now: datetime) -> dict[str, Any]:
    try:
        metadata = path.stat()
    except OSError as exc:
        raise CommandError("Reviewed CRM v2 preview is unavailable") from exc
    if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise CommandError("Reviewed CRM v2 preview must be a private regular file")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise CommandError("Reviewed CRM v2 preview is invalid JSON") from exc
    if not isinstance(payload, dict) or payload.get("schema") != _PREVIEW_SCHEMA:
        raise CommandError("Reviewed CRM v2 preview has an unsupported schema")
    generated_at = _parse_aware_datetime(payload.get("generated_at"))
    age = now - generated_at
    if age < -_PREVIEW_CLOCK_SKEW or age > _PREVIEW_MAX_AGE:
        raise CommandError("Reviewed CRM v2 preview is not recent")
    summary = payload.get("summary")
    inputs = payload.get("inputs")
    active_rows = payload.get("active_accounts")
    if not isinstance(summary, dict) or not isinstance(inputs, dict) or not isinstance(active_rows, list):
        raise CommandError("Reviewed CRM v2 preview is structurally incomplete")
    if summary.get("active_accounts") != len(active_rows):
        raise CommandError("Reviewed CRM v2 preview row count is internally inconsistent")
    payload["_generated_at"] = generated_at
    return payload


def _parse_aware_datetime(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise CommandError("Reviewed CRM v2 preview has an invalid timestamp") from exc
    if timezone.is_naive(parsed):
        raise CommandError("Reviewed CRM v2 preview timestamp must include a timezone")
    return parsed


def _assert_preview_inputs_match(preview, inputs) -> None:
    if _canonical_inputs(preview.get("inputs")) != _canonical_inputs(inputs):
        raise CommandError("Reviewed CRM v2 preview inputs do not match this apply")


def _canonical_inputs(value: Any) -> tuple[Any, ...]:
    if not isinstance(value, Mapping):
        return ()
    normalize = lambda item: " ".join(str(item or "").casefold().split())
    sales = tuple(sorted({normalize(item) for item in value.get("sales_motion_accounts", ()) if normalize(item)}))
    pins = tuple(sorted({normalize(item) for item in value.get("manual_pins", ()) if normalize(item)}))
    overrides_raw = value.get("owner_overrides", {})
    overrides = tuple(sorted(
        (normalize(account), normalize(owner))
        for account, owner in (overrides_raw.items() if isinstance(overrides_raw, Mapping) else ())
        if normalize(account) and normalize(owner)
    ))
    return sales, pins, overrides


def _assert_preview_summary_matches(preview, evidence_rows) -> None:
    rows = list(evidence_rows)
    current = _preview_comparison_counts(rows)
    summary = preview["summary"]
    expected = {
        "account_groups_evaluated": summary.get("account_groups_evaluated"),
        "active_accounts": summary.get("active_accounts"),
        "people_only_accounts": summary.get("people_only_accounts"),
        "admission_reasons": summary.get("admission_reasons"),
    }
    if current != expected:
        raise CommandError(
            "Reviewed CRM v2 preview no longer matches the recomputed "
            "active-account counts or admission reasons"
        )
    preview_fingerprint = _semantic_identity_fingerprint_from_preview(preview)
    current_fingerprint = _semantic_identity_fingerprint_from_evidence(rows)
    if preview_fingerprint != current_fingerprint:
        raise CommandError(
            "Reviewed CRM v2 preview no longer matches the exact active-account "
            "or reminder identity set"
        )


def _preview_comparison_counts(evidence_rows) -> dict[str, Any]:
    rows = list(evidence_rows)
    active = [row for row in rows if row.decision.admitted]
    return {
        "account_groups_evaluated": len(rows),
        "active_accounts": len(active),
        "people_only_accounts": len(rows) - len(active),
        "admission_reasons": dict(sorted(Counter(
            row.decision.primary_reason_code.value for row in active
        ).items())),
    }


def _semantic_identity_fingerprint_from_preview(preview: Mapping[str, Any]) -> str:
    raw_rows = preview.get("active_accounts")
    if not isinstance(raw_rows, list):
        raise CommandError("Reviewed CRM v2 preview is missing active identities")
    identities = []
    keys = []
    for row in raw_rows:
        if not isinstance(row, Mapping):
            raise CommandError("Reviewed CRM v2 preview has an invalid active identity")
        account_key = str(row.get("account_key") or "").strip()
        if not account_key:
            raise CommandError("Reviewed CRM v2 preview has an unkeyed active identity")
        keys.append(account_key)
        identities.append(_semantic_identity_record(
            account_key=account_key,
            lead_ids=row.get("lead_ids"),
            opportunity_id=row.get("opportunity_id"),
            owner=row.get("owner"),
            owner_is_override=row.get("owner_is_override"),
            last_meaningful_touch=row.get("last_meaningful_touch"),
            reminder_target_lead_id=row.get("reminder_target_lead_id"),
            trigger_message_id=row.get("trigger_message_id"),
            trigger_meeting_id=row.get("trigger_meeting_id"),
            do_not_outreach=row.get("do_not_outreach"),
            reminder_do_not_outreach=row.get("reminder_do_not_outreach"),
            decision=row.get("decision"),
        ))
    if len(keys) != len(set(keys)):
        raise CommandError("Reviewed CRM v2 preview has duplicate active identities")
    return _semantic_identity_fingerprint(identities)


def _semantic_identity_fingerprint_from_evidence(evidence_rows) -> str:
    active = [row for row in evidence_rows if row.decision.admitted]
    keys = [row.account_key for row in active]
    if any(not str(key or "").strip() for key in keys) or len(keys) != len(set(keys)):
        raise CommandError("Recomputed CRM v2 evidence has invalid active identities")
    identities = [
        _semantic_identity_record(
            account_key=row.account_key,
            lead_ids=row.lead_ids,
            opportunity_id=row.opportunity_id,
            owner=row.owner,
            owner_is_override=row.owner_is_override,
            last_meaningful_touch=(
                row.last_meaningful_touch.isoformat()
                if row.last_meaningful_touch else ""
            ),
            reminder_target_lead_id=row.reminder_target_lead_id,
            trigger_message_id=row.trigger_message_id,
            trigger_meeting_id=row.trigger_meeting_id,
            do_not_outreach=row.facts.do_not_outreach,
            reminder_do_not_outreach=row.reminder_do_not_outreach,
            decision=asdict(row.decision),
        )
        for row in active
    ]
    return _semantic_identity_fingerprint(identities)


def _semantic_identity_record(
    *,
    account_key,
    lead_ids,
    opportunity_id,
    owner,
    owner_is_override,
    last_meaningful_touch,
    reminder_target_lead_id,
    trigger_message_id,
    trigger_meeting_id,
    do_not_outreach,
    reminder_do_not_outreach,
    decision,
) -> dict[str, Any]:
    if not isinstance(decision, Mapping):
        raise CommandError("Reviewed CRM v2 preview has an invalid decision identity")
    try:
        stable_lead_ids = sorted(int(value) for value in (lead_ids or ()))
    except (TypeError, ValueError) as exc:
        raise CommandError("Reviewed CRM v2 preview has invalid lead identities") from exc
    return {
        "account_key": str(account_key or "").strip(),
        "lead_ids": stable_lead_ids,
        "opportunity_id": str(opportunity_id or "").strip(),
        "owner": " ".join(str(owner or "").casefold().split()),
        "owner_is_override": bool(owner_is_override),
        "last_meaningful_touch": str(last_meaningful_touch or ""),
        "reminder_target_lead_id": _optional_stable_int(reminder_target_lead_id),
        "trigger_message_id": _optional_stable_int(trigger_message_id),
        "trigger_meeting_id": _optional_stable_int(trigger_meeting_id),
        "do_not_outreach": bool(do_not_outreach),
        "reminder_do_not_outreach": bool(reminder_do_not_outreach),
        "decision": _semantic_json_value(decision),
    }


def _optional_stable_int(value):
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise CommandError("Reviewed CRM v2 preview has an invalid reminder identity") from exc


def _semantic_json_value(value):
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "isoformat") and not isinstance(value, str):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {
            str(key): _semantic_json_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_semantic_json_value(item) for item in value]
    return value


def _semantic_identity_fingerprint(identities) -> str:
    canonical = json.dumps(
        sorted(identities, key=lambda row: row["account_key"]),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _context_health(*, now, max_age_hours, preview_generated_at=None) -> dict[str, Any]:
    from crm.models import MeetingNote, MeetingNoteSyncState, Message

    gmail_latest = Message.objects.filter(source=Message.Source.GMAIL).aggregate(
        latest=Max("creation_date")
    )["latest"]
    states = {
        state.source: state
        for state in MeetingNoteSyncState.objects.filter(
            source__in=(MeetingNote.Source.GEMINI, MeetingNote.Source.GRANOLA)
        )
    }
    sources = {
        "gmail": gmail_latest,
        "gemini": (
            states.get(MeetingNote.Source.GEMINI).last_success_at
            if states.get(MeetingNote.Source.GEMINI) else None
        ),
        "granola": (
            states.get(MeetingNote.Source.GRANOLA).last_success_at
            if states.get(MeetingNote.Source.GRANOLA) else None
        ),
    }
    stale_after = timedelta(hours=max_age_hours)
    details = {}
    for name, timestamp in sources.items():
        if timestamp is None:
            details[name] = {"status": "missing", "age_hours": None}
            continue
        age = max(timedelta(0), now - timestamp)
        details[name] = {
            "status": "stale" if age > stale_after else "fresh",
            "age_hours": round(age.total_seconds() / 3600, 1),
        }
    changed_after_preview = bool(
        preview_generated_at
        and any(
            timestamp is not None and timestamp > preview_generated_at
            for timestamp in sources.values()
        )
    )
    if changed_after_preview:
        raise CommandError(
            "Stored CRM context changed after the reviewed preview; regenerate it"
        )
    return {
        "prerequisite": "refresh stored Gmail/Gemini context before preview",
        "provider_calls": 0,
        "stale_sources": sum(item["status"] != "fresh" for item in details.values()),
        "sources": details,
    }


def _assert_reconciliation_safe(report) -> None:
    if report.issues:
        raise SheetsError(
            f"CRM v2 reconciliation has {len(report.issues)} identity issue(s)"
        )


def _evidence_counts(rows) -> dict[str, Any]:
    rows = list(rows)
    active = [row for row in rows if row.decision.admitted]
    return {
        "evaluated": len(rows),
        "active": len(active),
        "people_only": len(rows) - len(active),
        "actionable": sum(row.decision.reminder.should_create_reminder for row in active),
        "outreach_stopped": sum(row.facts.do_not_outreach for row in active),
        "unowned": sum(not row.owner for row in active),
        "admission_reasons": dict(sorted(Counter(
            row.decision.primary_reason_code.value for row in active
        ).items())),
    }


def _people_publish_counts(result: Mapping[str, Any]) -> dict[str, Any]:
    fields = (
        "source_leads",
        "source_deals",
        "companies",
        "rows_before",
        "rows_after",
        "appended",
        "updated",
        "updated_cells",
        "unchanged",
        "skipped",
        "errored",
        "duplicate_keys",
        "duplicate_lead_ids",
        "duplicate_linkedin_urls",
        "header_additions",
    )
    return {
        "status": str(result.get("status") or ""),
        **{
            field: int(result.get(field, 0) or 0)
            for field in fields
        },
    }


def _reconciliation_counts(report) -> dict[str, Any]:
    fields = (
        "evidence_rows", "admitted_rows", "people_only_rows", "accounts_created",
        "opportunities_created", "opportunities_activated",
        "opportunities_deactivated", "contacts_linked", "owners_assigned",
        "domains_populated", "meetings_linked", "meeting_notes_linked",
        "opportunities_unchanged",
    )
    return {
        **{field: int(getattr(report, field)) for field in fields},
        "issues": len(report.issues),
    }


def _pipeline_triage_counts(report, *, reports=()) -> dict[str, Any]:
    passes = tuple(reports) or (report,)
    return {
        "evaluated": int(report.evaluated),
        "eligible": int(report.eligible),
        "promoted": sum(int(item.promoted) for item in passes),
        "preserved": int(report.preserved),
        "skipped": int(report.skipped),
        "issues": sum(len(item.issues) for item in passes),
    }


def _action_counts(report, *, reports=()) -> dict[str, Any]:
    passes = tuple(reports) or (report,)
    snapshot_fields = (
        "evidence_rows", "actionable_rows", "actions_unchanged",
        "human_actions_preserved", "unowned_skipped", "ineligible_rows",
    )
    mutation_fields = (
        "actions_created", "actions_updated", "actions_reused", "actions_cancelled",
    )
    return {
        **{field: int(getattr(report, field)) for field in snapshot_fields},
        **{
            field: sum(int(getattr(item, field)) for item in passes)
            for field in mutation_fields
        },
        "issues": sum(len(item.issues) for item in passes),
    }


def _sheet_plan_counts(
    active_plan,
    action_plan,
    *,
    database_view,
    first_cutover,
) -> dict[str, Any]:
    if active_plan is None or action_plan is None:
        return {
            "first_cutover": first_cutover,
            "active_account_rows": len(database_view.rows.active_accounts),
            "action_rows": len(database_view.rows.actions),
            "appends": len(database_view.rows.active_accounts) + len(database_view.rows.actions),
            "cell_changes": 0,
            "imports": 0,
            "conflicts": 0,
            "unkeyed_rows": 0,
        }
    return {
        "first_cutover": first_cutover,
        "active_account_rows": len(database_view.rows.active_accounts),
        "action_rows": len(database_view.rows.actions),
        "appends": len(active_plan.appends) + len(action_plan.appends),
        "cell_changes": len(active_plan.changes) + len(action_plan.changes),
        "imports": len(active_plan.imports) + len(action_plan.imports),
        "conflicts": len(active_plan.conflicts) + len(action_plan.conflicts),
        "unkeyed_rows": (
            len(active_plan.unkeyed_nonempty_rows)
            + len(action_plan.unkeyed_nonempty_rows)
        ),
    }
