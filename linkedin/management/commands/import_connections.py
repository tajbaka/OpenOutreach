"""Backfill Attio with already-accepted LinkedIn connections from a CSV.

Logs into LinkedIn as a separate account (env-var-driven, decoupled from the
daemon's LinkedInProfile DB rows), scrapes the Connections page back N days,
and for each CSV row whose URL matches a connection card, creates a Lead +
Deal at state=CONNECTED in our DB. The hourly sync_attio cron then mirrors
these to Attio. Does NOT enqueue follow-ups or touch the daemon task queue.

Required env vars (alternative LinkedIn account, distinct from the daemon's):
    BACKFILL_LINKEDIN_USERNAME
    BACKFILL_LINKEDIN_PASSWORD

Cookies cached at `data/backfill_cookies.json` so subsequent runs reuse the
session — delete that file to force a fresh login.

Three-way DB dedupe rule:
- URL not in DB                                    → create Lead + Deal
- URL in DB, only as a Deal in the backfill camp.  → upsert (idempotent)
- URL in DB with a Deal in any other campaign      → skip (let daemon own it)

CSV format:
    LinkedIn URL,First Name,Message
    https://www.linkedin.com/in/waylonkrush/,Waylon,"Hey Waylon, ..."
"""
from __future__ import annotations

import csv as csv_module
import enum
import logging
import os
import random
import time
from dataclasses import dataclass
from datetime import datetime as _dt, timedelta
from typing import Iterable, IO

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from crm.models import Deal, Lead, Message
from linkedin.db.urls import url_to_public_id
from linkedin.enums import ProfileState
from linkedin.models import Campaign

logger = logging.getLogger(__name__)


class CsvFormatError(Exception):
    pass


@dataclass(frozen=True)
class CsvRow:
    public_id: str
    linkedin_url: str
    first_name: str
    outbound_message: str


def parse_csv(fp: IO) -> Iterable[CsvRow]:
    reader = csv_module.DictReader(fp)
    if reader.fieldnames is None or "LinkedIn URL" not in reader.fieldnames:
        raise CsvFormatError(
            "CSV must include a 'LinkedIn URL' column. "
            f"Got: {reader.fieldnames}"
        )
    if "First Name" not in reader.fieldnames:
        raise CsvFormatError("CSV must include a 'First Name' column.")

    for row in reader:
        url = (row.get("LinkedIn URL") or "").strip()
        if not url:
            continue
        public_id = url_to_public_id(url) or ""
        if not public_id:
            logger.warning("Could not extract public_id from %r — skipping", url)
            continue
        yield CsvRow(
            public_id=public_id,
            linkedin_url=url,
            first_name=(row.get("First Name") or "").strip(),
            outbound_message=(row.get("Message") or "").strip(),
        )


# ---------------------------------------------------------------------------
# Dedupe — replace-if-Chuka-is-ahead semantics
# ---------------------------------------------------------------------------


class DedupeDecision(enum.Enum):
    CREATE = "create"   # No Deal in target campaign — create fresh at CONNECTED.
    REPLACE = "replace" # Existing Deal at QUALIFIED/READY_TO_CONNECT/PENDING — promote to CONNECTED.
    SKIP = "skip"       # Existing Deal at CONNECTED+ — daemon's view is at-or-ahead, leave alone.


# Order in which Deal.state advances along the auto-managed funnel. We treat
# anything below CONNECTED as "behind" (so the import promotes), and anything
# at-or-past CONNECTED as "at or ahead" (so the import skips).
_BEHIND_STATES = {
    ProfileState.QUALIFIED,
    ProfileState.READY_TO_CONNECT,
    ProfileState.PENDING,
}


def decide_dedupe(*, linkedin_url: str, target_campaign: Campaign) -> tuple[DedupeDecision, Deal | None]:
    """Look at existing Deals for this URL in the target campaign.

    Returns the decision plus the conflicting Deal (if any) so apply_match
    can act on it without re-querying.
    """
    lead = Lead.objects.filter(linkedin_url=linkedin_url).first()
    if lead is None:
        return DedupeDecision.CREATE, None

    existing = (
        Deal.objects
        .filter(lead=lead, campaign=target_campaign)
        .first()
    )
    if existing is None:
        return DedupeDecision.CREATE, None

    if existing.state in _BEHIND_STATES:
        return DedupeDecision.REPLACE, existing
    return DedupeDecision.SKIP, existing


# ---------------------------------------------------------------------------
# Per-match write
# ---------------------------------------------------------------------------


def apply_match(*, row: CsvRow, target_campaign: Campaign) -> DedupeDecision:
    """Apply CREATE or REPLACE for a CSV row. Returns the decision taken.

    Outbound message persistence is deferred to `get_conversation` (called by
    the command's main loop) — Voyager returns the real entityUrn, sender,
    and timestamp, which is strictly better than the CSV-derived placeholder.
    """
    decision, existing = decide_dedupe(
        linkedin_url=row.linkedin_url, target_campaign=target_campaign,
    )

    if decision == DedupeDecision.SKIP:
        return decision

    with transaction.atomic():
        lead, _ = Lead.objects.get_or_create(
            linkedin_url=row.linkedin_url,
            defaults={
                "first_name": row.first_name,
                "public_identifier": row.public_id,
            },
        )
        # Backfill identifying fields if Lead pre-existed without them.
        updates = {}
        if not lead.first_name and row.first_name:
            updates["first_name"] = row.first_name
        if not lead.public_identifier:
            updates["public_identifier"] = row.public_id
        if updates:
            for k, v in updates.items():
                setattr(lead, k, v)
            lead.save(update_fields=list(updates.keys()))

        if decision == DedupeDecision.CREATE:
            Deal.objects.create(
                lead=lead,
                campaign=target_campaign,
                state=ProfileState.CONNECTED,
                sent_note=row.outbound_message or "",
            )
        elif decision == DedupeDecision.REPLACE:
            # Promote existing PENDING/READY/QUALIFIED Deal to CONNECTED and
            # overwrite the invite text — Chuka's invite is the one that
            # produced the actual connection.
            assert existing is not None
            existing.state = ProfileState.CONNECTED
            if row.outbound_message:
                existing.sent_note = row.outbound_message
            existing.connect_attempts = 0
            existing.backoff_hours = 0
            existing.save(update_fields=[
                "state", "sent_note", "connect_attempts", "backoff_hours",
            ])

    return decision


# ---------------------------------------------------------------------------
# Lazy imports — these touch Playwright / browser session state, so we keep
# them out of module top so unit tests that exercise the helpers above don't
# pull them in needlessly.
# ---------------------------------------------------------------------------


def _stamp_reply(*, linkedin_url: str, target_campaign: Campaign, reply: dict):
    """Set Deal.last_reply_at from the latest inbound message timestamp."""
    deal = Deal.objects.filter(
        lead__linkedin_url=linkedin_url, campaign=target_campaign,
    ).first()
    if deal is None:
        return
    ts_str = (reply.get("timestamp") or "").strip()
    if not ts_str:
        return
    try:
        naive = _dt.strptime(ts_str, "%Y-%m-%d %H:%M")
        deal.last_reply_at = timezone.make_aware(
            naive, timezone.get_current_timezone(),
        )
        deal.save(update_fields=["last_reply_at"])
    except ValueError:
        pass


def _resolve_target_campaign(campaign_id: int | None) -> Campaign:
    """Pick the Campaign this import will write into.

    Auto-default behavior: if --campaign isn't given and exactly one Campaign
    exists in the DB, use it. Otherwise require explicit --campaign <id>.
    """
    if campaign_id is not None:
        try:
            return Campaign.objects.get(pk=campaign_id)
        except Campaign.DoesNotExist:
            raise CommandError(f"No Campaign with pk={campaign_id}")

    campaigns = list(Campaign.objects.all())
    if len(campaigns) == 1:
        return campaigns[0]
    if len(campaigns) == 0:
        raise CommandError("No Campaigns exist. Create one first via Django Admin.")
    names = ", ".join(f"{c.pk}={c.name!r}" for c in campaigns)
    raise CommandError(
        f"Multiple Campaigns exist ({names}). "
        f"Pass --campaign <id> to choose which one to write into.",
    )


# Module-level imports that the test suite patches via name reference.
from linkedin.actions.connections import scrape_connections  # noqa: E402
from linkedin.actions.conversations import get_conversation  # noqa: E402
from linkedin.actions.standalone_session import StandaloneLinkedInSession  # noqa: E402
from linkedin.conf import ROOT_DIR  # noqa: E402
from linkedin.notifications.slack import latest_reply_from_lead  # noqa: E402


# Env-var-driven separate LinkedIn account (distinct from daemon's profile).
ENV_USERNAME = "BACKFILL_LINKEDIN_USERNAME"
ENV_PASSWORD = "BACKFILL_LINKEDIN_PASSWORD"
COOKIE_PATH = ROOT_DIR / "data" / "backfill_cookies.json"

# Pacing between get_conversation calls. With ~300 matches, 25-50s jitter
# averages ~37s/lead → ~3 hours total runtime, well below LinkedIn's
# behavioral-detection threshold for messaging-thread access.
SLEEP_MIN_SECONDS = 25
SLEEP_MAX_SECONDS = 50


def make_backfill_session() -> StandaloneLinkedInSession:
    """Build a StandaloneLinkedInSession configured for the backfill account.

    Reads BACKFILL_LINKEDIN_USERNAME / BACKFILL_LINKEDIN_PASSWORD from env;
    cookies cached separately from Sales Nav at data/backfill_cookies.json.
    """
    return StandaloneLinkedInSession(
        env_username=ENV_USERNAME,
        env_password=ENV_PASSWORD,
        cookie_path=COOKIE_PATH,
        label="Backfill",
    )


class Command(BaseCommand):
    help = "Backfill Attio with already-accepted LinkedIn connections from a CSV."

    def add_arguments(self, parser):
        parser.add_argument("--csv", required=True, help="Path to the CSV file.")
        parser.add_argument(
            "--campaign", type=int, default=None,
            help="Campaign pk to write into. Defaults to the only Campaign if "
                 "exactly one exists; required when there are multiple.",
        )
        parser.add_argument(
            "--since-days", type=int, default=90,
            help="How far back on the Connections page to paginate (default: 90 days).",
        )
        parser.add_argument(
            "--limit", type=int, default=0,
            help="Cap how many actionable rows to process this run (0 = no cap).",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Print the plan without logging in or writing to the DB.",
        )

    def handle(self, *args, **opts):
        csv_path = opts["csv"]
        campaign_id = opts["campaign"]
        since_days = opts["since_days"]
        limit = opts["limit"]
        dry_run = opts["dry_run"]

        # Resolve the target campaign first — purely a read in dry-run mode
        # (no rows created), so the dry-run is now truly side-effect-free.
        target_campaign = _resolve_target_campaign(campaign_id)

        # Env-var creds only required for the live run (dry-run never logs in).
        if not dry_run:
            if not os.getenv(ENV_USERNAME, "").strip() or not os.getenv(ENV_PASSWORD, ""):
                raise CommandError(
                    f"Set {ENV_USERNAME} and {ENV_PASSWORD} in .env "
                    f"(see linkedin/management/commands/import_connections.py docstring)."
                )

        with open(csv_path, newline="") as fp:
            try:
                rows = list(parse_csv(fp))
            except CsvFormatError as e:
                raise CommandError(str(e))

        self.stdout.write(
            f"Loaded {len(rows)} rows; target campaign: {target_campaign.name} (pk={target_campaign.pk})"
        )

        # Phase 1: dedupe pre-pass — count what each row would do.
        plan: dict[DedupeDecision, list[CsvRow]] = {
            DedupeDecision.CREATE: [],
            DedupeDecision.REPLACE: [],
            DedupeDecision.SKIP: [],
        }
        for row in rows:
            decision, _ = decide_dedupe(
                linkedin_url=row.linkedin_url, target_campaign=target_campaign,
            )
            plan[decision].append(row)

        for r in plan[DedupeDecision.SKIP]:
            self.stdout.write(f"  skip (already at CONNECTED+ in target campaign): {r.linkedin_url}")
        for r in plan[DedupeDecision.REPLACE]:
            self.stdout.write(f"  replace (will promote → CONNECTED): {r.linkedin_url}")

        actionable = plan[DedupeDecision.CREATE] + plan[DedupeDecision.REPLACE]
        if limit > 0 and len(actionable) > limit:
            self.stdout.write(f"Capping actionable to first {limit} (--limit).")
            actionable = actionable[:limit]
        self.stdout.write(
            f"Actionable: {len(actionable)} "
            f"(create={len(plan[DedupeDecision.CREATE])}, "
            f"replace={len(plan[DedupeDecision.REPLACE])}), "
            f"skipped: {len(plan[DedupeDecision.SKIP])}"
        )

        if dry_run:
            self.stdout.write("[dry-run] would log in, scrape, and write the above.")
            return

        session = make_backfill_session()
        session.start()
        session.campaign = target_campaign

        stop_before = (timezone.now() - timedelta(days=since_days)).date()
        self.stdout.write(f"Scraping connections back to {stop_before} ...")
        entries = scrape_connections(session, stop_before=stop_before)
        accepted_by_pid = {e.public_id: e for e in entries}
        self.stdout.write(f"Scraped {len(entries)} connection cards.")

        # Build the matched-only worklist so pacing applies between actual
        # Voyager calls, not all 300 actionable rows.
        matched_rows = [r for r in actionable if r.public_id in accepted_by_pid]
        unmatched = len(actionable) - len(matched_rows)
        self.stdout.write(
            f"Matched against scrape: {len(matched_rows)} "
            f"(unmatched/not-on-Connections-page: {unmatched})"
        )

        created, replaced, skipped_already_connected, with_reply = 0, 0, 0, 0
        # Track whether the previous iteration did any Voyager work — only
        # then do we need to pace before the next iteration.
        last_iter_did_voyager = False
        for i, row in enumerate(matched_rows, 1):
            # Pacing: only sleep before iterations that follow a Voyager-touching
            # iteration. Re-runs of mostly-already-connected leads now stay fast.
            if last_iter_did_voyager:
                time.sleep(random.uniform(SLEEP_MIN_SECONDS, SLEEP_MAX_SECONDS))
            last_iter_did_voyager = False

            decision = apply_match(row=row, target_campaign=target_campaign)

            if decision == DedupeDecision.SKIP:
                skipped_already_connected += 1
                self.stdout.write(
                    f"  [{i}/{len(matched_rows)}] {row.public_id}: skip "
                    f"(already CONNECTED+ in target campaign)"
                )
                # No Voyager calls fired — don't pace next iteration.
                continue

            if decision == DedupeDecision.CREATE:
                created += 1
            elif decision == DedupeDecision.REPLACE:
                replaced += 1

            # Inbound side: get_conversation also runs persist_thread via the hook.
            # This is where Chuka's outbound invite + any inbound replies land
            # in crm.Message with proper sender/timestamp from Voyager.
            try:
                messages = get_conversation(session, row.public_id)
            except Exception as e:
                logger.warning("get_conversation failed for %s: %s", row.public_id, e)
                messages = None

            if messages:
                full_name = (row.first_name or row.public_id).strip()
                reply = latest_reply_from_lead(messages, full_name)
                if reply:
                    _stamp_reply(
                        linkedin_url=row.linkedin_url,
                        target_campaign=target_campaign,
                        reply=reply,
                    )
                    with_reply += 1

            self.stdout.write(
                f"  [{i}/{len(matched_rows)}] {row.public_id}: "
                f"{decision.value}, msgs={len(messages) if messages else 0}"
            )

            # We hit Voyager — pace before the next iteration.
            last_iter_did_voyager = True

        self.stdout.write(
            f"import_connections: created={created}, replaced={replaced}, "
            f"skipped_already_connected={skipped_already_connected}, "
            f"with_reply={with_reply} of {len(matched_rows)} matched; "
            f"sync_attio will mirror these on its next run."
        )
