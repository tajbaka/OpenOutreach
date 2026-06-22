"""Backfill follow_up Tasks for the Connected/No-Reply cohort.

One-time-ish command for catching up after `ENABLE_FOLLOW_UP` is flipped
on, or any time the operator wants to re-pin everyone in the
Connected/No-Reply bucket. Iterates Deals at `state=Connected` whose
Lead has zero inbound `crm.Message` rows and isn't disqualified, then
enqueues one `follow_up` Task per Lead with a small stagger delay so
the daemon's rate limit doesn't get hammered.

The daemon's existing `handle_follow_up` (see `linkedin/tasks/follow_up.py`)
picks them up per its active-hours / daily-cap policy, sends the rigid
ICP DM, marks Completed on success.

Run from a Claude session or cron:
    .venv/bin/python manage.py enqueue_no_reply_followups
    .venv/bin/python manage.py enqueue_no_reply_followups --limit 20
    .venv/bin/python manage.py enqueue_no_reply_followups --campaign 1 --dry-run

The default skips any Lead that already has a pending or running
`follow_up` Task — re-running is idempotent. `--force` overrides that
guard and queues a fresh Task even if one already exists.
"""
from __future__ import annotations

import logging

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Exists, OuterRef

from crm.models import Deal, Message
from linkedin.db.messages import lead_outbound_operators
from linkedin.db.urls import url_to_public_id
from linkedin.enums import ProfileState
from linkedin.models import LinkedInProfile, Task
from linkedin.operators import resolve_operator
from linkedin.tasks.connect import enqueue_follow_up

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Enqueue follow_up Tasks for every Connected/No-Reply Lead so the daemon can send the rigid ICP DM."

    def add_arguments(self, parser):
        parser.add_argument(
            "--campaign", type=int, default=None,
            help="Restrict to a single Campaign by primary key. Default: all campaigns.",
        )
        parser.add_argument(
            "--operator", type=str, default=None,
            help="Canonical operator handle (e.g. 'Chuka', 'Arian') whose LinkedIn "
                 "account is currently logged in. Only leads whose outbound DMs "
                 "originated from this operator's account are eligible — sending "
                 "from the wrong account would post into a thread that doesn't "
                 "exist on this side. Defaults to the resolved handle of the "
                 "first active LinkedInProfile in the DB.",
        )
        parser.add_argument(
            "--limit", type=int, default=0,
            help="Cap how many Tasks to enqueue (0 = all eligible).",
        )
        parser.add_argument(
            "--stagger-seconds", type=int, default=30,
            help="Delay between consecutive Tasks (default 30s). The daemon "
                 "already paces sends inside `handle_follow_up`, but a small "
                 "stagger spreads the queue more evenly across active hours.",
        )
        parser.add_argument(
            "--force", action="store_true",
            help="Enqueue a fresh Task even if one already exists for this Lead. "
                 "Default: skip leads with a pending or running follow_up Task.",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Print what would be enqueued, don't insert any Tasks.",
        )

    def handle(self, *args, **opts):
        campaign_id = opts["campaign"]
        operator = opts["operator"]
        limit = opts["limit"]
        stagger = opts["stagger_seconds"]
        force = opts["force"]
        dry_run = opts["dry_run"]

        # Resolve the operator the daemon will run as. Required so we don't
        # enqueue Tasks the daemon will skip at claim-time (the
        # owner-scoping guard in handle_follow_up would drop them).
        if not operator:
            active = LinkedInProfile.objects.filter(active=True).first()
            if not active:
                raise CommandError(
                    "No active LinkedInProfile in DB and no --operator passed. "
                    "Set --operator=<Chuka|Arian|...> explicitly or activate a "
                    "profile via Django Admin."
                )
            operator = resolve_operator(active.linkedin_username)
            self.stdout.write(
                f"Resolved operator from active LinkedInProfile "
                f"({active.linkedin_username}) → {operator!r}"
            )
        else:
            operator = resolve_operator(operator)

        # Eligible Deal queryset: CONNECTED, not disqualified, no inbound
        # message on the Lead. We DON'T inspect the LinkedIn DM directly —
        # `crm.Message` is the persisted truth from sweep_connections /
        # backfill / agent persists, so this filter is fast and stable.
        no_inbound = ~Exists(
            Message.objects.filter(
                lead_id=OuterRef("lead_id"),
                direction=Message.Direction.INBOUND,
            )
        )
        qs = (
            Deal.objects
            .filter(state=ProfileState.CONNECTED.value, lead__disqualified=False)
            .annotate(_has_inbound_proxy=Exists(Message.objects.filter(
                lead_id=OuterRef("lead_id"),
                direction=Message.Direction.INBOUND,
            )))
            .filter(_has_inbound_proxy=False)
            .select_related("lead", "campaign")
        )
        if campaign_id is not None:
            qs = qs.filter(campaign_id=campaign_id)

        deals = list(qs)
        if not deals:
            self.stdout.write("No Connected/No-Reply deals found. Nothing to enqueue.")
            return

        # Owner-scoping filter — drop leads where the outbound DM originated
        # from a different operator's account. The daemon would skip these
        # at claim-time anyway (see `handle_follow_up`), but pre-filtering
        # keeps the queue clean. Leads with zero outbound (freshly swept
        # CONNECTED, never messaged) have an empty owner set — those are
        # fair game for whichever daemon picks them up.
        before_owner = len(deals)
        deals = [
            d for d in deals
            if (
                not (owners := lead_outbound_operators(d.lead))
                or operator in owners
            )
        ]
        cross_account = before_owner - len(deals)
        if cross_account:
            self.stdout.write(
                f"Skipped {cross_account} lead(s) whose LinkedIn thread "
                f"belongs to a different operator (not {operator!r})."
            )

        if not force:
            # Drop leads with an existing pending/running follow_up Task —
            # the daemon will pick those up on its own; re-enqueuing would
            # just create duplicates.
            existing_public_ids = set()
            for t in Task.objects.filter(
                task_type=Task.TaskType.FOLLOW_UP,
                status__in=[Task.Status.PENDING, Task.Status.RUNNING],
            ):
                pid = (t.payload or {}).get("public_id")
                if pid:
                    existing_public_ids.add(pid)
            before = len(deals)
            deals = [
                d for d in deals
                if url_to_public_id(d.lead.linkedin_url) not in existing_public_ids
            ]
            skipped = before - len(deals)
            if skipped:
                self.stdout.write(f"Skipped {skipped} lead(s) with an existing pending follow_up Task. (--force to override.)")

        if limit and len(deals) > limit:
            deals = deals[:limit]

        self.stdout.write(
            f"Eligible: {len(deals)} Connected/No-Reply lead(s). "
            f"Stagger: {stagger}s between enqueues. "
            f"Dry-run: {dry_run}."
        )

        if dry_run:
            for i, d in enumerate(deals[:20]):
                self.stdout.write(
                    f"  [{d.lead.id}] {d.lead.first_name} {d.lead.last_name}  "
                    f"({d.lead.company_name})  +{i*stagger}s"
                )
            if len(deals) > 20:
                self.stdout.write(f"  ... and {len(deals)-20} more")
            return

        enqueued = 0
        for i, deal in enumerate(deals):
            public_id = url_to_public_id(deal.lead.linkedin_url)
            if not public_id:
                logger.warning("no public_id for Lead %s (url=%s) — skipping",
                               deal.lead.id, deal.lead.linkedin_url)
                continue
            enqueue_follow_up(
                deal.campaign_id, public_id,
                operator=operator,
                delay_seconds=i * stagger,
            )
            from gmail.handoff import maybe_schedule_gmail_sequence

            maybe_schedule_gmail_sequence(deal=deal, operator=operator)
            enqueued += 1

        self.stdout.write(self.style.SUCCESS(f"Enqueued {enqueued} follow_up Task(s)."))
