"""Mirror Deal state into the Attio Sales list.

Groups in-funnel deals by company. For each company:
- one Company record (reused across all leads at that company)
- one Sales list entry (Stage = aggregate of all leads' stages)
- one Person record per individual lead

The "deal" is at the company level (FedRampGPT sells to the org). The
entry's Stage advances when ANY lead at the company progresses — so if
lead A is still Prospecting and lead B replies, the company entry moves
to Qualification. Won wins outright; Lost only sticks if all leads at
the company are Lost.

Idempotent. Safe to run from cron. Decoupled from the daemon — failures
here never block outreach.

Don't-downgrade: humans can manually move entries to Meeting+ and the
script preserves that. See `should_patch_stage`.

Usage:
    python manage.py sync_attio                       # all campaigns
    python manage.py sync_attio --campaign 1          # one campaign
    python manage.py sync_attio --dry-run             # show what would happen
"""
from __future__ import annotations

import sys
from collections import defaultdict

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Mirror Deal state to the Attio Sales list."

    def add_arguments(self, parser):
        parser.add_argument("--campaign", type=int, default=None,
                            help="Limit to a single Campaign (PK).")
        parser.add_argument("--dry-run", action="store_true",
                            help="Show planned changes without calling Attio.")

    def handle(self, *args, **options):
        from linkedin.conf import ATTIO_API_KEY, ATTIO_SALES_LIST_ID
        from linkedin.enums import ProfileState
        from linkedin.exceptions import AttioError
        from linkedin.notifications.attio import (
            PROGRESSION_RANK,
            aggregate_company_stage,
            create_company,
            create_person,
            create_sales_entry,
            deal_to_outreach_status,
            deal_to_stage,
            get_person_outreach_status,
            get_sales_entry_state,
            patch_sales_entry_mpoc,
            patch_sales_entry_stage,
            set_person_outreach_status,
            should_patch_outreach_status,
            should_patch_stage,
        )

        from crm.models import Deal

        dry_run: bool = options["dry_run"]
        campaign_pk: int | None = options["campaign"]

        if not dry_run and (not ATTIO_API_KEY or not ATTIO_SALES_LIST_ID):
            self.stderr.write(
                "ATTIO_API_KEY or ATTIO_SALES_LIST_ID is not set in .env. "
                "Sync disabled."
            )
            sys.exit(1)

        in_funnel = (
            ProfileState.PENDING,
            ProfileState.CONNECTED,
            ProfileState.COMPLETED,
            ProfileState.FAILED,
        )
        deals_qs = (
            Deal.objects.filter(state__in=in_funnel, lead_id__isnull=False)
            .select_related("lead", "campaign")
            .order_by("id")
        )
        if campaign_pk is not None:
            deals_qs = deals_qs.filter(campaign_id=campaign_pk)
        deals = list(deals_qs)
        if not deals:
            self.stdout.write("No in-funnel deals — nothing to sync.")
            return

        # Group by company. Skip leads with no company_name (can't be in
        # a company-parented Sales list).
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
            f"Syncing {len(deals)} deal(s) across {len(groups)} compan{'ies' if len(groups)!=1 else 'y'}"
            f"{' (dry-run)' if dry_run else ''}..."
        )

        created_company = created_person = created_entry = 0
        patched_stage = patched_mpoc = patched_status = errored = 0

        def pick_leader(group_deals):
            """Furthest-along active lead; tie-break by oldest deal id."""
            return max(
                group_deals,
                key=lambda d: (
                    PROGRESSION_RANK.get(deal_to_stage(d), -1),
                    -d.id,
                ),
            )

        for company_name, group in groups.items():
            leads = [d.lead for d in group]
            target_stage = aggregate_company_stage([deal_to_stage(d) for d in group])
            # Leader drives Main point of contact (both new + existing entries).
            leader_deal = pick_leader(group)

            try:
                # ---- 1. Company: reuse from any peer Lead, or create
                company_id = next(
                    (l.attio_company_id for l in leads if l.attio_company_id), ""
                )
                if not company_id:
                    if dry_run:
                        company_id = "(would-create)"
                        self.stdout.write(f"  + [would create company] {company_name}")
                    else:
                        company_id = create_company(company_name)
                        self.stdout.write(f"  + company {company_name} -> {company_id}")
                        created_company += 1

                # ---- 2. Person: one per lead. Create if missing; write
                #         per-person Outreach status (don't-downgrade applied).
                for deal_in_group in group:
                    lead = deal_in_group.lead
                    full = f"{lead.first_name} {lead.last_name}".strip()
                    target_status = deal_to_outreach_status(deal_in_group)

                    if not lead.attio_person_id:
                        if dry_run:
                            self.stdout.write(
                                f"    + [would create person] {full} @ {target_status}"
                            )
                        else:
                            pid = create_person(
                                first_name=lead.first_name,
                                last_name=lead.last_name,
                                linkedin_url=lead.linkedin_url or "",
                                company_id=company_id,
                            )
                            lead.attio_person_id = pid
                            self.stdout.write(f"    + person  {full} -> {pid}")
                            created_person += 1
                            # Set initial outreach_status on creation.
                            set_person_outreach_status(pid, target_status)
                            patched_status += 1
                    elif not dry_run:
                        # Existing person — patch outreach_status only if changed
                        # and not a downgrade.
                        current_status = get_person_outreach_status(lead.attio_person_id)
                        if should_patch_outreach_status(current_status, target_status):
                            set_person_outreach_status(lead.attio_person_id, target_status)
                            self.stdout.write(
                                f"    ~ status  {full}: "
                                f"{current_status or '?'} -> {target_status}"
                            )
                            patched_status += 1

                    if lead.attio_company_id != company_id and not dry_run:
                        lead.attio_company_id = company_id
                    if not dry_run:
                        lead.save(update_fields=["attio_person_id", "attio_company_id"])

                    # ---- Phase D synthesis: D1 email extract + D2 Wants Meeting.
                    # Best-effort; any failure stays inside synthesize_for_deal.
                    if not dry_run:
                        try:
                            from linkedin.notifications.synthesis import synthesize_for_deal
                            synthesize_for_deal(deal_in_group)
                        except Exception as e:
                            self.stdout.write(self.style.WARNING(
                                f"    ! synthesis failed for {full}: {e}"
                            ))

                # ---- 3. Sales list entry: one per company. Reuse from any
                #         peer Lead, or create. Stage + MPOC kept in sync with
                #         the group's leader on every run.
                entry_id = next(
                    (l.attio_entry_id for l in leads if l.attio_entry_id), ""
                )
                leader_full = (
                    f"{leader_deal.lead.first_name} "
                    f"{leader_deal.lead.last_name}"
                ).strip()
                if not entry_id:
                    if dry_run:
                        self.stdout.write(
                            f"  + [would create entry] {company_name} @ {target_stage} "
                            f"(MPOC: {leader_full})"
                        )
                    else:
                        entry_id = create_sales_entry(
                            company_id=company_id,
                            person_id=leader_deal.lead.attio_person_id,
                            stage=target_stage,
                        )
                        self.stdout.write(
                            f"  + entry   {company_name} -> {entry_id} "
                            f"[{target_stage}, MPOC: {leader_full}]"
                        )
                        created_entry += 1
                else:
                    # Existing entry — patch Stage and/or MPOC if changed.
                    if dry_run:
                        self.stdout.write(
                            f"  ~ [would maybe-patch] {company_name} -> "
                            f"stage={target_stage} mpoc={leader_full}"
                        )
                    else:
                        state = get_sales_entry_state(entry_id)
                        current_stage = state["stage"]
                        current_mpoc = state["mpoc_id"]

                        if should_patch_stage(current_stage, target_stage):
                            patch_sales_entry_stage(entry_id, target_stage)
                            self.stdout.write(
                                f"  ~ stage   {company_name}: "
                                f"{current_stage or '?'} -> {target_stage}"
                            )
                            patched_stage += 1
                        elif current_stage != target_stage:
                            self.stdout.write(
                                f"  = stage   {company_name}: keeping {current_stage} "
                                f"(don't downgrade to {target_stage})"
                            )

                        leader_pid = leader_deal.lead.attio_person_id
                        if leader_pid and current_mpoc and current_mpoc != leader_pid:
                            patch_sales_entry_mpoc(entry_id, leader_pid)
                            self.stdout.write(
                                f"  ~ mpoc    {company_name}: -> {leader_full}"
                            )
                            patched_mpoc += 1

                # Save entry_id on every lead in the group so future runs
                # find it via peer-lookup.
                if entry_id and not dry_run:
                    for lead in leads:
                        if lead.attio_entry_id != entry_id:
                            lead.attio_entry_id = entry_id
                            lead.save(update_fields=["attio_entry_id"])

            except AttioError as e:
                self.stderr.write(f"  ! {company_name}: {e}")
                errored += 1
                continue

        self.stdout.write(
            self.style.SUCCESS(
                f"Done — companies:{created_company} people:{created_person} "
                f"entries:{created_entry} stage_patched:{patched_stage} "
                f"mpoc_patched:{patched_mpoc} status_patched:{patched_status} "
                f"skipped:{skipped} errored:{errored}"
            )
        )
