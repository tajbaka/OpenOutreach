"""Batch-reconcile accepted invitations through the Connections page."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from django.utils import timezone
from termcolor import colored

from linkedin.actions.connections import scrape_connections
from linkedin.actions.conversations import get_conversation
from linkedin.conf import CONNECTION_SWEEP_INTERVAL_HOURS, ENABLE_SWEEP_CONNECTIONS
from linkedin.db.deals import set_profile_state
from linkedin.db.urls import url_to_public_id
from linkedin.enums import ProfileState
from linkedin.models import ActionLog, Task
from linkedin.notifications.slack import (
    latest_reply_from_lead,
    notify_connection_accepted,
)

logger = logging.getLogger(__name__)


def process_accepted_deal(session, deal, *, entry=None) -> None:
    """Apply the shared post-accept path for one freshly accepted Deal."""
    from crm.models import Deal
    from linkedin.tasks.connect import enqueue_follow_up, recommended_action_delay

    public_id = url_to_public_id(deal.lead.linkedin_url) if deal.lead.linkedin_url else None
    if not public_id:
        raise ValueError(f"Deal {deal.pk} has no LinkedIn public identifier")

    session.campaign = deal.campaign
    set_profile_state(session, public_id, ProfileState.CONNECTED.value)
    deal = Deal.objects.select_related("lead", "campaign").get(pk=deal.pk)

    full_name = (
        f"{deal.lead.first_name or ''} {deal.lead.last_name or ''}".strip()
        or public_id
    )
    try:
        messages = get_conversation(session, public_id)
    except Exception as e:
        logger.warning("Could not fetch conversation for %s: %s", full_name, e)
        messages = None
    reply = latest_reply_from_lead(messages, full_name)
    reply_text = reply.get("text") if reply else None

    if reply:
        ts_str = (reply.get("timestamp") or "").strip()
        if ts_str:
            try:
                naive = datetime.strptime(ts_str, "%Y-%m-%d %H:%M")
                deal.last_reply_at = timezone.make_aware(
                    naive, timezone.get_current_timezone(),
                )
                deal.save(update_fields=["last_reply_at"])
            except ValueError:
                pass

    from linkedin.operators import resolve_operator

    operator = resolve_operator(session.linkedin_profile.linkedin_username)
    try:
        notify_connection_accepted(
            full_name=full_name,
            title="",
            company=deal.lead.company_name or "",
            profile_url=deal.lead.linkedin_url
            or f"https://www.linkedin.com/in/{public_id}/",
            campaign_name=deal.campaign.name,
            reply_text=reply_text,
            operator=operator,
        )
    except Exception as e:
        logger.warning("Slack notify failed for %s: %s", full_name, e)

    delay_seconds = recommended_action_delay(
        session.linkedin_profile, ActionLog.ActionType.FOLLOW_UP,
    )
    connected_on = getattr(entry, "connected_on", None)
    if connected_on:
        age_days = (timezone.now().date() - connected_on).days
        if age_days > 0:
            logger.debug(
                "%s accepted %d day(s) ago — follow-up in %.0fs",
                public_id, age_days, delay_seconds,
            )

    from linkedin.icp_outbound import resolve_icp

    enqueue_follow_up(
        deal.campaign.pk,
        public_id,
        operator=operator,
        icp=resolve_icp(deal.lead),
        delay_seconds=delay_seconds,
    )
    from gmail.handoff import maybe_schedule_gmail_sequence

    maybe_schedule_gmail_sequence(deal=deal, operator=operator)


def reconcile_pending_connections(session) -> tuple[int, int, int]:
    """Reconcile accepted invitations across this sender's campaigns."""
    from crm.models import Deal

    pending_deals = list(
        Deal.objects.filter(
            state=ProfileState.PENDING,
            campaign__in=session.campaigns,
        )
        .select_related("lead", "campaign")
        .order_by("id")
    )
    if not pending_deals:
        return 0, 0, 0

    # Prefer the positive invite ledger timestamp but preserve legacy PENDING
    # rows through update_date. LinkedIn returns connections newest-first.
    oldest_pending = min(
        deal.invitation_sent_at or deal.update_date
        for deal in pending_deals
    )
    entries = scrape_connections(session, stop_before=oldest_pending.date())
    accepted_by_pid = {entry.public_id: entry for entry in entries}

    matched = 0
    for deal in pending_deals:
        public_id = url_to_public_id(deal.lead.linkedin_url) if deal.lead.linkedin_url else None
        entry = accepted_by_pid.get(public_id) if public_id else None
        if entry is None:
            continue
        process_accepted_deal(session, deal, entry=entry)
        matched += 1

    return len(pending_deals), matched, len(entries)


def handle_sweep_connections(task, session, qualifiers):
    if not ENABLE_SWEEP_CONNECTIONS:
        logger.debug("sweep_connections disabled — skipping task %s", task.pk)
        return

    logger.info(
        "%s sweep_connections",
        colored("▶", "magenta", attrs=["bold"]),
    )
    pending_count, matched, entry_count = reconcile_pending_connections(session)
    logger.info(
        "sweep_connections: %d pending → %d newly connected (of %d on-page)",
        pending_count, matched, entry_count,
    )

    from linkedin.operators import resolve_operator

    enqueue_sweep_connections(
        operator=resolve_operator(session.linkedin_profile.linkedin_username),
        delay_seconds=CONNECTION_SWEEP_INTERVAL_HOURS * 3600,
    )


def enqueue_sweep_connections(*, operator: str, delay_seconds: float | None = None):
    """Ensure one pending sweep_connections task exists per operator."""
    if not ENABLE_SWEEP_CONNECTIONS:
        return
    if not operator:
        raise ValueError("enqueue_sweep_connections requires a non-empty operator")
    if delay_seconds is None:
        delay_seconds = CONNECTION_SWEEP_INTERVAL_HOURS * 3600

    if Task.objects.filter(
        task_type=Task.TaskType.SWEEP_CONNECTIONS,
        status=Task.Status.PENDING,
        payload__operator=operator,
    ).exists():
        return

    Task.objects.create(
        task_type=Task.TaskType.SWEEP_CONNECTIONS,
        scheduled_at=timezone.now() + timedelta(seconds=delay_seconds),
        payload={"operator": operator},
    )
