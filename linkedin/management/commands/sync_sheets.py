"""Mirror Deal state into the Google Sheets People tab.

Steady-state sync (replaces sync_airtable). Iterates in-funnel deals,
groups by company to compute the aggregate Stage, then upserts one row
per lead into the People tab. Don't-downgrade preserves manual sheet
edits — see sheets.should_patch_outreach_status / should_patch_stage.

Idempotent. Safe to run from cron. Decoupled from the daemon — failures
here never block outreach.

Usage:
    python manage.py sync_sheets                     # all campaigns
    python manage.py sync_sheets --campaign 1        # one campaign
    python manage.py sync_sheets --dry-run           # show what would happen
"""
from __future__ import annotations

import sys
from collections import defaultdict
from datetime import datetime, timezone

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Mirror Deal state to the Google Sheets People tab."

    def add_arguments(self, parser):
        parser.add_argument("--campaign", type=int, default=None,
                            help="Limit to a single Campaign (PK).")
        parser.add_argument("--dry-run", action="store_true",
                            help="Show planned changes without writing to Sheets.")

    def handle(self, *args, **options):
        from crm.models import Deal

        from linkedin.conf import GOOGLE_SHEETS_CREDENTIALS_PATH, GOOGLE_SHEETS_ID
        from linkedin.enums import ProfileState
        from linkedin.exceptions import SheetsError
        from linkedin.notifications import sheets

        dry_run: bool = options["dry_run"]
        campaign_pk: int | None = options["campaign"]

        if not dry_run and (
            not GOOGLE_SHEETS_ID or not GOOGLE_SHEETS_CREDENTIALS_PATH
        ):
            self.stderr.write(
                "GOOGLE_SHEETS_ID and GOOGLE_SHEETS_CREDENTIALS_PATH must "
                "be set in .env. Sync disabled."
            )
            sys.exit(1)

        in_funnel = (
            ProfileState.PENDING,
            ProfileState.CONNECTED,
            ProfileState.COMPLETED,
            ProfileState.FAILED,
        )
        deals_qs = (
            Deal.objects.filter(
                state__in=in_funnel,
                lead_id__isnull=False,
                lead__disqualified=False,
            )
            .select_related("lead", "campaign")
            .order_by("id")
        )
        if campaign_pk is not None:
            deals_qs = deals_qs.filter(campaign_id=campaign_pk)
        deals = list(deals_qs)
        if not deals:
            self.stdout.write("No in-funnel deals — nothing to sync.")
            return

        # Group by company. Skip leads with no company_name — they can't be
        # represented in a per-company-aggregate Stage and the People row
        # would be missing the natural anchor for grouping.
        groups: dict[str, list] = defaultdict(list)
        skipped = 0
        for deal in deals:
            cname = (deal.lead.company_name or "").strip()
            if not cname:
                self.stdout.write(
                    f"  - skip {deal.lead.first_name} {deal.lead.last_name}: "
                    f"no company_name"
                )
                skipped += 1
                continue
            groups[cname].append(deal)

        self.stdout.write(
            f"Syncing {len(deals)} deal(s) across {len(groups)} "
            f"compan{'ies' if len(groups) != 1 else 'y'}"
            f"{' (dry-run)' if dry_run else ''}..."
        )

        if dry_run:
            self._plan(groups, sheets)
            return

        idx = sheets.SheetIndex.load()
        last_synced = datetime.now(timezone.utc).date().isoformat()

        appended = updated = unchanged = errored = 0
        synth_ok = synth_err = 0

        for company_name, group in groups.items():
            target_stage = sheets.aggregate_company_stage(
                [sheets.deal_to_stage(d) for d in group]
            )
            for deal_in_group in group:
                lead = deal_in_group.lead
                full = f"{lead.first_name} {lead.last_name}".strip()
                target_status = sheets.deal_to_outreach_status(deal_in_group)

                if not lead.linkedin_url:
                    self.stdout.write(self.style.WARNING(
                        f"  - skip {full}: no linkedin_url"
                    ))
                    skipped += 1
                    continue

                # Look up existing row first so the synthesis pass can read
                # the live status from the sheet (don't-downgrade lookup).
                existing = idx.get_row(lead.linkedin_url) or {}
                current_status_in_sheet = existing.get(
                    sheets.COL_OUTREACH_STATUS, "",
                )

                # ---- Phase D synthesis: D1 email extract + D2 Wants Meeting.
                # Runs before payload assembly so D2 status changes feed into
                # the same row write below. Notes column is human-only — we
                # never write synthesis-derived text there.
                synth_status_override = ""
                try:
                    from linkedin.notifications.synthesis import synthesize_for_deal
                    synth_result = synthesize_for_deal(
                        deal_in_group,
                        current_outreach_status=current_status_in_sheet,
                    )
                    if synth_result and synth_result.wants_meeting_now:
                        synth_status_override = sheets.STATUS_WANTS_MEETING
                    synth_ok += 1
                except Exception as e:
                    self.stdout.write(self.style.WARNING(
                        f"    ! synthesis failed for {full}: {e}"
                    ))
                    synth_err += 1

                effective_status = synth_status_override or target_status

                # Build the row payload. Title / Notes / AI Notes / Priority
                # come from the existing row when present (sheet is the
                # source of truth post-backfill). Synth notes get prepended
                # to existing Notes if the LLM detected meeting intent.
                emails = [lead.email] if lead.email else []
                # Preserve the email-list union — existing sheet emails win
                # except for new ones.
                existing_emails = [
                    e.strip() for e in (existing.get(sheets.COL_EMAILS, "") or "").split("\n")
                    if e.strip()
                ]
                merged_emails = list(existing_emails)
                for e in emails:
                    if e not in merged_emails:
                        merged_emails.append(e)

                # Notes is human-only — preserve whatever's already in the sheet.
                notes = existing.get(sheets.COL_NOTES, "") or ""

                payload = sheets.build_row_payload(
                    lead=lead,
                    title=existing.get(sheets.COL_TITLE, "") or "",
                    emails=merged_emails,
                    outreach_status=effective_status,
                    stage=target_stage,
                    priority=existing.get(sheets.COL_PRIORITY, "") or "",
                    primary_location=existing.get(sheets.COL_PRIMARY_LOCATION, "") or "",
                    notes=notes,
                    ai_notes=existing.get(sheets.COL_AI_NOTES, "") or "",
                    last_synced=last_synced,
                )

                try:
                    was_new, changed = idx.upsert_row(payload)
                except SheetsError as e:
                    self.stderr.write(f"  ! upsert {full}: {e}")
                    errored += 1
                    continue

                if was_new:
                    appended += 1
                    self.stdout.write(
                        f"  + {full:35s} status={effective_status} "
                        f"stage={target_stage}"
                    )
                elif changed:
                    updated += 1
                    self.stdout.write(
                        f"  ~ {full:35s} {', '.join(changed)}"
                    )
                else:
                    unchanged += 1

        try:
            counts = idx.flush()
        except SheetsError as e:
            self.stderr.write(f"  ! flush failed: {e}")
            sys.exit(1)

        self.stdout.write(self.style.SUCCESS(
            f"Done — appended:{counts['appended']} updated:{counts['updated']} "
            f"unchanged:{unchanged} synth_ok:{synth_ok} synth_err:{synth_err} "
            f"skipped:{skipped} errored:{errored}"
        ))

    def _plan(self, groups, sheets):
        """Dry-run output."""
        for company_name, group in groups.items():
            stage = sheets.aggregate_company_stage(
                [sheets.deal_to_stage(d) for d in group]
            )
            self.stdout.write(
                f"company: {company_name:30s} stage={stage} ({len(group)} lead(s))"
            )
            for d in group:
                lead = d.lead
                full = f"{lead.first_name} {lead.last_name}".strip()
                status = sheets.deal_to_outreach_status(d)
                self.stdout.write(
                    f"  - {full:30s} status={status}"
                )
