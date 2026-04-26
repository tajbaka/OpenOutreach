# linkedin/tasks/sweep_connections.py
"""Sweep connections task — batch-detects accepted invitations via the Connections page.

Replaces the per-profile check_pending flow: one page visit per sweep interval
reconciles every PENDING Deal across all of this account's campaigns.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone
from termcolor import colored

from linkedin.actions.connections import scrape_connections
from linkedin.conf import CONNECTION_SWEEP_INTERVAL_HOURS, ENABLE_FOLLOW_UP
from linkedin.db.deals import set_profile_state
from linkedin.db.urls import url_to_public_id
from linkedin.enums import ProfileState
from linkedin.models import ActionLog, Task

logger = logging.getLogger(__name__)


def handle_sweep_connections(task, session, qualifiers):
    if not ENABLE_FOLLOW_UP:
        # Defense in depth: should never fire, since daemon cancels these on
        # startup and enqueue_sweep_connections is gated.
        logger.debug("sweep_connections disabled \u2014 skipping task %s", task.pk)
        return

    from crm.models import Deal
    from linkedin.tasks.connect import enqueue_follow_up, recommended_action_delay

    logger.info(
        "%s sweep_connections",
        colored("\u25b6", "magenta", attrs=["bold"]),
    )

    # All PENDING Deals for this account's campaigns — one query, one cross-ref.
    pending_deals = (
        Deal.objects.filter(
            state=ProfileState.PENDING,
            campaign__in=session.campaigns,
        )
        .select_related("lead", "campaign")
    )

    # Earliest invite date across all outstanding PENDINGs. The connections
    # page is sorted newest-first, so cards older than this cutoff cannot be
    # acceptances of our requests — we can stop scrolling once we pass it.
    oldest_pending = pending_deals.order_by("update_date").values_list(
        "update_date", flat=True,
    ).first()
    stop_before = oldest_pending.date() if oldest_pending else None

    entries = scrape_connections(session, stop_before=stop_before)
    accepted_by_pid = {e.public_id: e for e in entries}

    matched = 0
    now = timezone.now()
    for deal in pending_deals:
        public_id = url_to_public_id(deal.lead.linkedin_url) if deal.lead.linkedin_url else None
        if not public_id:
            continue
        entry = accepted_by_pid.get(public_id)
        if entry is None:
            continue

        session.campaign = deal.campaign
        set_profile_state(session, public_id, ProfileState.CONNECTED.value)

        delay_seconds = recommended_action_delay(
            session.linkedin_profile, ActionLog.ActionType.FOLLOW_UP,
        )
        # If LinkedIn reports an older connected_on date, we missed the event —
        # don't further delay the follow-up beyond the ML-recommended cadence.
        if entry.connected_on:
            age_days = (now.date() - entry.connected_on).days
            if age_days > 0:
                logger.debug(
                    "%s accepted %d day(s) ago — follow-up in %.0fs",
                    public_id, age_days, delay_seconds,
                )

        enqueue_follow_up(deal.campaign.pk, public_id, delay_seconds=delay_seconds)
        matched += 1

    logger.info(
        "sweep_connections: %d pending → %d newly connected (of %d on-page)",
        pending_deals.count(), matched, len(entries),
    )

    # Self-reschedule.
    enqueue_sweep_connections(delay_seconds=CONNECTION_SWEEP_INTERVAL_HOURS * 3600)


def enqueue_sweep_connections(delay_seconds: float | None = None):
    """Ensure one pending sweep_connections task exists; do not duplicate."""
    if not ENABLE_FOLLOW_UP:
        return
    if delay_seconds is None:
        delay_seconds = CONNECTION_SWEEP_INTERVAL_HOURS * 3600

    if Task.objects.filter(
        task_type=Task.TaskType.SWEEP_CONNECTIONS,
        status=Task.Status.PENDING,
    ).exists():
        return

    Task.objects.create(
        task_type=Task.TaskType.SWEEP_CONNECTIONS,
        scheduled_at=timezone.now() + timedelta(seconds=delay_seconds),
        payload={},
    )
