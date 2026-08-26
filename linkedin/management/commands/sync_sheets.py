"""Incrementally publish the durable Lead ledger to the People tab.

Every Lead is eligible, including contacts with no Deal and contacts whose
automation Deal is inactive, failed, or otherwise historical. Deals only
supply optional status/stage rollups. Existing People rows are never removed,
rebuilt, or reordered.

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
from contextlib import nullcontext

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Exists, OuterRef, Prefetch
from django.utils import timezone


class Command(BaseCommand):
    help = "Incrementally publish the complete Lead ledger to People."

    def add_arguments(self, parser):
        parser.add_argument("--campaign", type=int, default=None,
                            help="Limit to a single Campaign (PK).")
        parser.add_argument("--dry-run", action="store_true",
                            help="Show planned changes without writing to Sheets.")

    def _console_text(self, message: str) -> str:
        encoding = getattr(self.stdout, "encoding", None) or sys.stdout.encoding or "utf-8"
        return message.encode(encoding, errors="replace").decode(encoding)

    def handle(self, *args, **options):
        """Serialize every standalone People publication with refresh_crm."""
        from linkedin.crm_lock import (
            CrmRefreshAlreadyRunning,
            crm_refresh_lock,
        )

        lock_held = bool(options.pop("_crm_refresh_lock_held", False))
        lock_context = nullcontext() if lock_held else crm_refresh_lock()
        try:
            with lock_context:
                return self._handle_locked(*args, **options)
        except CrmRefreshAlreadyRunning as exc:
            raise CommandError(str(exc)) from exc

    def _handle_locked(self, *args, **options):
        from crm.models import Deal, Lead, Meeting

        from linkedin.conf import GOOGLE_SHEETS_CREDENTIALS_PATH, GOOGLE_SHEETS_ID
        from linkedin.exceptions import SheetsError
        from linkedin.notifications import sheets

        dry_run: bool = options["dry_run"]
        campaign_pk: int | None = options["campaign"]
        self.result = {
            "status": "planned" if dry_run else "published",
            "source_leads": 0,
            "source_deals": 0,
            "companies": 0,
            "rows_before": 0,
            "rows_after": 0,
            "appended": 0,
            "updated": 0,
            "updated_cells": 0,
            "unchanged": 0,
            "skipped": 0,
            "errored": 0,
            "ambiguous_existing": 0,
            "header_additions": 0,
            "duplicate_keys": 0,
            "duplicate_lead_ids": 0,
            "duplicate_linkedin_urls": 0,
        }

        if not GOOGLE_SHEETS_ID or not GOOGLE_SHEETS_CREDENTIALS_PATH:
            raise CommandError(
                "GOOGLE_SHEETS_ID and GOOGLE_SHEETS_CREDENTIALS_PATH must "
                "be set in .env. Exact dry-runs also read the live sheet."
            )

        # People is a contact ledger, not a view of one automation state. Load
        # every Lead and prefetch every relevant per-campaign Deal. A campaign
        # filter narrows the source for the standalone diagnostic command but
        # still never removes rows already present in People.
        deals_qs = (
            Deal.objects.filter(lead_id__isnull=False)
            .annotate(
                has_meeting=Exists(
                    Meeting.objects.filter(lead_id=OuterRef("lead_id"))
                )
            )
            .order_by("id")
        )
        if campaign_pk is not None:
            deals_qs = deals_qs.filter(campaign_id=campaign_pk)
        leads_qs = (
            Lead.objects.defer(
                "embedding",
                "description",
                "phones",
                "phone_providers_tried",
                "email_providers_tried",
            )
            .order_by("id")
        )
        if campaign_pk is not None:
            leads_qs = leads_qs.filter(deal__campaign_id=campaign_pk).distinct()
        leads = list(
            leads_qs.prefetch_related(
                Prefetch("deal_set", queryset=deals_qs, to_attr="people_deals")
            )
        )

        company_deals: dict[str, list] = defaultdict(list)
        companies: set[str] = set()
        source_deals = 0
        for lead in leads:
            company = (lead.company_name or "").strip()
            company_key = company.casefold() if company else f"__lead__:{lead.pk}"
            if company:
                companies.add(company.casefold())
            lead_deals = list(lead.people_deals)
            source_deals += len(lead_deals)
            company_deals[company_key].extend(lead_deals)

        self.result.update({
            "source_leads": len(leads),
            "source_deals": source_deals,
            "companies": len(companies),
        })

        try:
            idx = sheets.SheetIndex.load(apply_schema=not dry_run)
        except SheetsError as e:
            raise CommandError(f"failed loading People tab: {e}") from e
        rows_before = idx.material_row_count
        self.result["rows_before"] = rows_before
        last_synced = timezone.localdate().isoformat()

        skipped = errored = ambiguous_existing = unchanged = 0
        for lead in leads:
            full = f"{lead.first_name} {lead.last_name}".strip()
            company = (lead.company_name or "").strip()
            company_key = company.casefold() if company else f"__lead__:{lead.pk}"
            lead_deals = list(lead.people_deals)

            try:
                existing = idx.get_row(
                    lead.linkedin_url,
                    lead_id=lead.pk,
                ) or {}
            except SheetsError as e:
                self.stderr.write(f"  ! identity {full or 'unnamed lead'}: {e}")
                if idx.is_identity_represented(
                    lead.linkedin_url,
                    lead_id=lead.pk,
                ):
                    ambiguous_existing += 1
                else:
                    errored += 1
                continue

            if lead.disqualified:
                target_status = sheets.STATUS_DONT_SEND
            elif lead_deals:
                target_status = sheets.aggregate_person_outreach_status(
                    sheets.deal_to_outreach_status(deal) for deal in lead_deals
                )
            else:
                target_status = existing.get(sheets.COL_OUTREACH_STATUS, "") or ""

            if company_deals[company_key]:
                target_stage = sheets.aggregate_company_stage(
                    [
                        sheets.deal_to_stage(deal)
                        for deal in company_deals[company_key]
                    ]
                )
            else:
                target_stage = existing.get(sheets.COL_STAGE, "") or ""

            # Preserve the email-list union. Matching is case-insensitive but
            # the operator's existing spelling/order wins.
            existing_emails = [
                value.strip()
                for value in (existing.get(sheets.COL_EMAILS, "") or "").split("\n")
                if value.strip()
            ]
            merged_emails = list(existing_emails)
            seen_emails = {value.casefold() for value in existing_emails}
            if lead.email and lead.email.casefold() not in seen_emails:
                merged_emails.append(lead.email)

            payload = sheets.build_row_payload(
                lead=lead,
                title=existing.get(sheets.COL_TITLE, "") or "",
                emails=merged_emails,
                outreach_status=target_status,
                stage=target_stage,
                priority=existing.get(sheets.COL_PRIORITY, "") or "",
                primary_location=existing.get(sheets.COL_PRIMARY_LOCATION, "") or "",
                notes=existing.get(sheets.COL_NOTES, "") or "",
                ai_notes=existing.get(sheets.COL_AI_NOTES, "") or "",
                last_synced=last_synced,
            )

            try:
                was_new, changed = idx.upsert_row(payload)
            except SheetsError as e:
                self.stderr.write(f"  ! upsert {full or 'unnamed lead'}: {e}")
                if idx.is_identity_represented(
                    lead.linkedin_url,
                    lead_id=lead.pk,
                ):
                    ambiguous_existing += 1
                else:
                    errored += 1
                continue

            if was_new:
                self.stdout.write(
                    self._console_text(
                        f"  + {(full or 'Unnamed lead'):35s} "
                        f"status={target_status} stage={target_stage}"
                    )
                )
            elif changed:
                self.stdout.write(
                    self._console_text(
                        f"  ~ {(full or 'Unnamed lead'):35s} {', '.join(changed)}"
                    )
                )
            else:
                unchanged += 1

        self.stdout.write(
            f"Publishing {len(leads)} contact(s) with {source_deals} Deal signal(s)"
            f"{' (dry-run)' if dry_run else ''}..."
        )

        if dry_run:
            plan = idx.plan()
            counts = idx.flush(dry_run=True)
            duplicate_lead_ids = sum(
                duplicate.column == sheets.COL_LEAD_ID
                for duplicate in plan.duplicate_keys
            )
            duplicate_linkedin_urls = sum(
                duplicate.column == sheets.COL_LINKEDIN_URL
                for duplicate in plan.duplicate_keys
            )
            self.result.update({
                "appended": counts["appended"],
                "updated": counts["updated"],
                "updated_cells": plan.updated_cells,
                "unchanged": unchanged,
                "skipped": skipped,
                "errored": errored,
                "ambiguous_existing": ambiguous_existing,
                "rows_after": rows_before + counts["appended"],
                "header_additions": len(plan.header_additions),
                "duplicate_keys": len(plan.duplicate_keys),
                "duplicate_lead_ids": duplicate_lead_ids,
                "duplicate_linkedin_urls": duplicate_linkedin_urls,
            })
            self.stdout.write(
                "Exact People plan — "
                f"headers:{len(plan.header_additions)} "
                f"appended:{counts['appended']} "
                f"updated:{counts['updated']} "
                f"cells:{plan.updated_cells}"
            )
            if plan.header_additions:
                self.stdout.write(
                    "  headers to append: " + ", ".join(plan.header_additions)
                )
            for duplicate in plan.duplicate_keys:
                # Never print stable IDs or LinkedIn URLs in routine output.
                self.stderr.write(
                    f"  ! duplicate {duplicate.column}: rows "
                    + ", ".join(str(row) for row in duplicate.row_numbers)
                )
            self.stdout.write(
                f"No writes — unchanged:{unchanged} skipped:{skipped} "
                f"errored:{errored}"
            )
            return

        plan = idx.plan()
        duplicate_lead_ids = sum(
            duplicate.column == sheets.COL_LEAD_ID
            for duplicate in plan.duplicate_keys
        )
        duplicate_linkedin_urls = sum(
            duplicate.column == sheets.COL_LINKEDIN_URL
            for duplicate in plan.duplicate_keys
        )
        try:
            counts = idx.flush()
        except SheetsError as e:
            raise CommandError(f"People flush failed: {e}") from e

        self.result.update({
            "appended": counts["appended"],
            "updated": counts["updated"],
            "updated_cells": plan.updated_cells,
            "unchanged": unchanged,
            "skipped": skipped,
            "errored": errored,
            "ambiguous_existing": ambiguous_existing,
            "rows_after": rows_before + counts["appended"],
            "header_additions": len(plan.header_additions),
            "duplicate_keys": len(plan.duplicate_keys),
            "duplicate_lead_ids": duplicate_lead_ids,
            "duplicate_linkedin_urls": duplicate_linkedin_urls,
        })

        self.stdout.write(self.style.SUCCESS(
            f"Done — appended:{counts['appended']} updated:{counts['updated']} "
            f"unchanged:{unchanged} skipped:{skipped} errored:{errored}"
        ))


def run_people_sync(*, dry_run: bool, stdout, stderr, lock_held: bool = False) -> dict:
    """Run People publishing; callers may declare the shared lock already held."""
    command = Command(stdout=stdout, stderr=stderr)
    command.handle(
        dry_run=dry_run,
        campaign=None,
        _crm_refresh_lock_held=lock_held,
    )
    return dict(command.result)
