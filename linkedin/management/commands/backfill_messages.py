"""Sync `crm.Message` for every LinkedIn DM thread we know about.

Standalone management command (not part of the running daemon). Auto-runs
one sync pass per LinkedIn account that has env-configured credentials.
Each pass logs in via `StandaloneLinkedInSession` (cookies cached per
account so subsequent runs skip re-auth), asks LinkedIn who it's logged
in as, then fetches every CONNECTED+ Lead whose outbound thread sender
matches that account.

Idempotent on `(source, external_id)`, so re-running just adds new messages
without touching existing rows. Run periodically (e.g. cron) since the
running daemon stops watching threads after the initial accept; manual
replies don't land in the DB until this command re-pulls them.

Env vars expected in `.env` (set both pairs to sync both accounts; either
pair alone is fine if you only have one):

    # Primary account (e.g. the one the running daemon also uses)
    LINKEDIN_USERNAME=...
    LINKEDIN_PASSWORD=...

    # Backfill account (the secondary one, also used by import_connections)
    BACKFILL_LINKEDIN_USERNAME=...
    BACKFILL_LINKEDIN_PASSWORD=...

Usage:
    .venv/bin/python manage.py backfill_messages
    .venv/bin/python manage.py backfill_messages --campaign 1 --limit 50 --dry-run
"""
from __future__ import annotations

import logging
import os
import random
import time

from django.core.management.base import BaseCommand, CommandError

from crm.models import Deal, Message
from linkedin.actions.conversations import get_conversation
from linkedin.actions.standalone_session import StandaloneLinkedInSession
from linkedin.api.client import PlaywrightLinkedinAPI
from linkedin.enums import ProfileState

logger = logging.getLogger(__name__)


# Pacing between Voyager calls — randomized within this band to look human
# and stay clear of LinkedIn rate limits.
SLEEP_MIN_SECONDS = 8
SLEEP_MAX_SECONDS = 22


# Each tuple: (label, username env var, password env var). The session
# derives its cookie cache path from the resolved username, so swapping
# usernames within a slot doesn't collide with the prior account's cache.
ACCOUNTS = [
    ("primary",  "LINKEDIN_USERNAME",          "LINKEDIN_PASSWORD"),
    ("backfill", "BACKFILL_LINKEDIN_USERNAME", "BACKFILL_LINKEDIN_PASSWORD"),
]


def _make_session(label: str, env_user: str, env_pass: str) -> StandaloneLinkedInSession:
    return StandaloneLinkedInSession(
        env_username=env_user,
        env_password=env_pass,
        label=label,
    )


def _self_display_name(session) -> str:
    """Ask LinkedIn who this session is logged in as.

    Returns the user's display name (e.g. "Chukwuka Agu"), used to filter
    Leads to threads that actually live on this account. Raises if the call
    fails — without a verified identity we can't safely scope the queryset.
    """
    api = PlaywrightLinkedinAPI(session=session)
    profile, _ = api.get_profile(public_identifier="me")
    if not profile:
        raise CommandError("Could not fetch own profile via Voyager API.")
    name = (profile.get("full_name")
            or f"{profile.get('first_name', '')} {profile.get('last_name', '')}".strip())
    if not name:
        raise CommandError(f"Self-profile returned no name: {profile!r}")
    return name


def _eligible_deals(*, sender: str, campaign_id: int | None):
    states_with_thread = [
        ProfileState.CONNECTED,
        ProfileState.COMPLETED,
        ProfileState.FAILED,
    ]
    qs = (
        Deal.objects.filter(state__in=states_with_thread, lead_id__isnull=False)
        .select_related("lead", "campaign")
        .filter(
            lead__messages__direction="outbound",
            lead__messages__source=Message.Source.LINKEDIN,
            lead__messages__sender__iexact=sender,
        )
        .order_by("update_date")
        .distinct()
    )
    if campaign_id is not None:
        qs = qs.filter(campaign_id=campaign_id)
    return qs


def _run_prereq_gate_for_accounts(configured: list[tuple[str, str, str]]) -> bool:
    """Stack one prereq report per configured account, prompt once.

    `configured` is the list of (label, env_user_name, env_pass_name) tuples
    this command run will iterate over. We map each env_user → operator
    canonical handle, build a StalenessReport per operator, print them all,
    and prompt continue/abort if any is stale. Returns True to proceed.
    """
    from linkedin.operators import resolve_operator
    from linkedin.workflow_prereqs import check_prereqs, format_report, prompt_if_stale, StalenessReport

    reports: list[StalenessReport] = []
    for _label, env_user_name, _env_pass_name in configured:
        account = os.getenv(env_user_name, "").strip()
        operator = resolve_operator(account) if account else ""
        reports.append(check_prereqs("backfill-messages", operator=operator))

    any_warn = any(r.has_warnings for r in reports)
    for r in reports:
        print(format_report(r))
    if not any_warn:
        return True

    # Synthesize a single "any-warning" report so prompt_if_stale handles
    # the prompt + TTY auto-continue logic in one place. Operator + workflow
    # name in the synthesized header are cosmetic ("multi-operator") since
    # the real per-operator reports were already printed above.
    composite = StalenessReport(
        workflow="backfill-messages",
        operator="multi-operator",
        rows=[row for r in reports for row in r.rows if row.is_warning],
    )
    # Reports already printed above; suppress the helper's print so the
    # output isn't duplicated.
    return prompt_if_stale(composite, print_report=False)


class Command(BaseCommand):
    help = "Resync crm.Message rows from LinkedIn DM threads. Auto-runs one pass per env-configured account."

    def add_arguments(self, parser):
        parser.add_argument(
            "--campaign", type=int, default=None,
            help="Restrict to a single Campaign by primary key. Default: all campaigns.",
        )
        parser.add_argument(
            "--limit", type=int, default=0,
            help="Cap how many Leads to process per pass (0 = all eligible).",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Log in (cheap once cookies are cached), derive sender, list eligible "
                 "Leads, but skip thread fetching.",
        )

    def handle(self, *args, **opts):
        from linkedin.notifications.slack import notify_on_error
        with notify_on_error(
            "backfill_messages",
            context={
                "campaign": opts.get("campaign"),
                "limit": opts.get("limit"),
                "dry_run": opts.get("dry_run", False),
            },
        ):
            self._handle_impl(*args, **opts)

    def _handle_impl(self, *args, **opts):
        campaign_id = opts["campaign"]
        limit = opts["limit"]
        dry_run = opts["dry_run"]

        configured = [
            (label, env_user, env_pass)
            for (label, env_user, env_pass) in ACCOUNTS
            if os.getenv(env_user) and os.getenv(env_pass)
        ]
        if not configured:
            raise CommandError(
                "No LinkedIn accounts configured in .env. Set at least one of:\n"
                "  LINKEDIN_USERNAME + LINKEDIN_PASSWORD (primary account), or\n"
                "  BACKFILL_LINKEDIN_USERNAME + BACKFILL_LINKEDIN_PASSWORD (backfill account)."
            )

        # Prereq staleness check — backfill_messages depends on
        # import-connections being fresh per operator. This command iterates
        # over every configured account, so build one report per operator
        # and surface them together before prompting once.
        if not _run_prereq_gate_for_accounts(configured):
            self.stdout.write(self.style.WARNING("Aborted by operator."))
            return

        for label, env_user, env_pass in configured:
            user_display = os.getenv(env_user)
            self.stdout.write(f"\n=== {label} ({user_display}) ===")
            try:
                session = _make_session(label, env_user, env_pass)
                session.start()
            except Exception as e:
                self.stdout.write(self.style.WARNING(
                    f"Login failed for {label}: {e}. Skipping this account."
                ))
                continue

            sender = _self_display_name(session)
            self.stdout.write(f"Logged in as {sender!r}.")
            self._run_pass(
                session=session, sender=sender,
                campaign_id=campaign_id, limit=limit, dry_run=dry_run,
            )

    def _run_pass(self, *, session, sender: str, campaign_id: int | None,
                  limit: int, dry_run: bool):
        deals = list(_eligible_deals(sender=sender, campaign_id=campaign_id))
        total = len(deals)
        if limit > 0:
            deals = deals[:limit]
        self.stdout.write(f"Eligible Leads: {total}. Processing this pass: {len(deals)}.")

        if dry_run:
            for d in deals[:25]:
                pid = d.lead.public_identifier or d.lead.linkedin_url
                self.stdout.write(f"  would fetch {pid} (campaign={d.campaign_id})")
            if len(deals) > 25:
                self.stdout.write(f"  … and {len(deals) - 25} more")
            self.stdout.write("[dry-run] no thread fetches.")
            return

        if not deals:
            self.stdout.write(f"No eligible Leads for sender={sender!r}.")
            return

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
                time.sleep(random.uniform(SLEEP_MIN_SECONDS, SLEEP_MAX_SECONDS))
                continue

            if parsed:
                fetched += 1
                count_now = Message.objects.filter(
                    lead=lead, source=Message.Source.LINKEDIN,
                ).count()
                if count_now > 0:
                    persisted += 1

                # Stamp Deal.last_reply_at from the newest inbound message we
                # just persisted. Without this, Stage stays at Prospecting
                # even after a reply is in the DB (sync_sheets gates the
                # Prospecting → Qualification transition on this field).
                latest_inbound = Message.objects.filter(
                    lead=lead, source=Message.Source.LINKEDIN,
                    direction=Message.Direction.INBOUND,
                ).order_by("-sent_at").first()
                stamped = ""
                if latest_inbound and (
                    deal.last_reply_at is None
                    or latest_inbound.sent_at > deal.last_reply_at
                ):
                    deal.last_reply_at = latest_inbound.sent_at
                    deal.save(update_fields=["last_reply_at"])
                    stamped = f" → last_reply_at={latest_inbound.sent_at.isoformat()}"

                self.stdout.write(
                    f"  [{i}/{len(deals)}] {pid}: fetched {len(parsed)} msgs, "
                    f"persisted={count_now}{stamped}"
                )
            else:
                self.stdout.write(f"  [{i}/{len(deals)}] {pid}: no conversation found")

            if i < len(deals):
                time.sleep(random.uniform(SLEEP_MIN_SECONDS, SLEEP_MAX_SECONDS))

        self.stdout.write(
            f"Done. Processed {len(deals)} Leads → fetched {fetched} threads "
            f"→ persisted {persisted}. Errors: {errors}. Skipped (no public_id): {skipped}."
        )

        # Record the pass so the followup workflow's staleness check knows
        # who walked threads + when. Per-operator key so Chuka's pass and
        # Arian's pass each have their own latest-run timestamp.
        if not dry_run:
            from linkedin.models import WorkflowRun
            from linkedin.operators import resolve_operator
            WorkflowRun.objects.create(
                name="backfill-messages",
                operator=resolve_operator(sender),
                summary=(
                    f"sender={sender!r} processed={len(deals)} "
                    f"fetched={fetched} persisted={persisted} "
                    f"errors={errors} skipped_no_pid={skipped}"
                ),
                counts={
                    "deals_eligible": len(deals),
                    "threads_fetched": fetched,
                    "messages_persisted": persisted,
                    "errors": errors,
                    "skipped_no_pid": skipped,
                },
            )
