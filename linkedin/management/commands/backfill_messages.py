"""Backfill `crm.Message` for existing Leads whose threads were never persisted.

The `get_conversation` hook (Phase B) only fires on future calls — historical
threads from already-CONNECTED leads aren't in `crm.Message` until the daemon
happens to re-visit them. This one-off command sweeps every Deal at
state >= CONNECTED and, for any Lead with zero stored messages, calls
`get_conversation` once. The hook does the actual upsert.

Idempotent: skips Leads that already have at least one Message row.

Usage:
    .venv/bin/python manage.py backfill_messages                   # all campaigns, default daemon account
    .venv/bin/python manage.py backfill_messages --campaign 1
    .venv/bin/python manage.py backfill_messages --handle me@example.com
    .venv/bin/python manage.py backfill_messages --limit 50 --dry-run
"""
from __future__ import annotations

import logging
import random
import time

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count, Q

from crm.models import Deal, Message
from linkedin.actions.conversations import get_conversation
from linkedin.browser.registry import get_or_create_session
from linkedin.conf import get_first_active_profile_handle
from linkedin.enums import ProfileState
from linkedin.models import Campaign, LinkedInProfile

logger = logging.getLogger(__name__)


# Pacing between Voyager calls — randomized within this band to look human
# and stay clear of LinkedIn rate limits.
SLEEP_MIN_SECONDS = 8
SLEEP_MAX_SECONDS = 22


class Command(BaseCommand):
    help = "Backfill crm.Message rows for Leads whose conversation history was never persisted."

    def add_arguments(self, parser):
        parser.add_argument(
            "--campaign", type=int, default=None,
            help="Restrict to a single Campaign by primary key. Default: all campaigns.",
        )
        parser.add_argument(
            "--handle", default=None,
            help="LinkedInProfile.linkedin_username to log in as. "
                 "Default: first active LinkedInProfile.",
        )
        parser.add_argument(
            "--limit", type=int, default=0,
            help="Cap how many Leads to process this run (0 = all eligible).",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Print the plan without logging in or fetching anything.",
        )

    def handle(self, *args, **opts):
        campaign_id = opts["campaign"]
        handle = opts["handle"] or get_first_active_profile_handle()
        limit = opts["limit"]
        dry_run = opts["dry_run"]

        if not handle:
            raise CommandError(
                "No active LinkedInProfile found and --handle wasn't given.",
            )
        # `handle` is the Django User username (matches what AccountSession
        # uses internally), not LinkedInProfile.linkedin_username.
        if not LinkedInProfile.objects.filter(user__username=handle).exists():
            raise CommandError(
                f"No LinkedInProfile attached to a Django user with username={handle!r}",
            )

        # Eligible: Deal at CONNECTED+ (we have a thread to fetch). Skip Leads
        # that already have any LinkedIn messages persisted (idempotency).
        states_with_thread = [
            ProfileState.CONNECTED,
            ProfileState.COMPLETED,
            ProfileState.FAILED,
        ]
        deals_qs = (
            Deal.objects.filter(state__in=states_with_thread, lead_id__isnull=False)
            .select_related("lead", "campaign")
            .annotate(
                msg_count=Count(
                    "lead__messages",
                    filter=Q(lead__messages__source=Message.Source.LINKEDIN),
                ),
            )
            .filter(msg_count=0)
            .order_by("update_date")  # oldest first — most likely to need backfill
        )
        if campaign_id is not None:
            deals_qs = deals_qs.filter(campaign_id=campaign_id)

        # Materialize before we possibly touch many Leads (cheap — just IDs + names).
        deals = list(deals_qs)
        total = len(deals)
        if limit > 0:
            deals = deals[:limit]

        self.stdout.write(
            f"Eligible Leads (CONNECTED+ with no LinkedIn Messages yet): {total}. "
            f"Processing this run: {len(deals)}."
        )

        if dry_run:
            for d in deals[:25]:
                pid = d.lead.public_identifier or d.lead.linkedin_url
                self.stdout.write(f"  would fetch thread for {pid} (campaign={d.campaign_id})")
            if len(deals) > 25:
                self.stdout.write(f"  … and {len(deals) - 25} more")
            self.stdout.write("[dry-run] no LinkedIn login, no fetches.")
            return

        if not deals:
            self.stdout.write("Nothing to backfill — all eligible Leads already have messages.")
            return

        session = get_or_create_session(handle=handle)
        # Some downstream helpers read session.campaign — set it per-iter
        # rather than locking to one campaign.
        fetched, persisted, errors, skipped = 0, 0, 0, 0
        for i, deal in enumerate(deals, 1):
            lead = deal.lead
            pid = lead.public_identifier
            if not pid:
                skipped += 1
                continue

            session.campaign = deal.campaign

            try:
                parsed = get_conversation(session, pid)
            except Exception as e:
                errors += 1
                logger.warning("get_conversation failed for %s: %s", pid, e)
                self.stdout.write(self.style.WARNING(
                    f"  [{i}/{len(deals)}] {pid}: ERROR — {e}"
                ))
                # Still pace before the next attempt.
                time.sleep(random.uniform(SLEEP_MIN_SECONDS, SLEEP_MAX_SECONDS))
                continue

            if parsed:
                fetched += 1
                # Re-count messages to confirm the hook persisted (it should have).
                count_now = Message.objects.filter(
                    lead=lead, source=Message.Source.LINKEDIN,
                ).count()
                if count_now > 0:
                    persisted += 1
                self.stdout.write(
                    f"  [{i}/{len(deals)}] {pid}: fetched {len(parsed)} msgs, "
                    f"persisted={count_now}"
                )
            else:
                self.stdout.write(f"  [{i}/{len(deals)}] {pid}: no conversation found")

            # Pacing — only sleep if there's a next iteration.
            if i < len(deals):
                time.sleep(random.uniform(SLEEP_MIN_SECONDS, SLEEP_MAX_SECONDS))

        self.stdout.write(
            f"\nDone. Processed {len(deals)} Leads → fetched {fetched} threads "
            f"→ persisted {persisted}. Errors: {errors}. Skipped (no public_id): {skipped}."
        )
