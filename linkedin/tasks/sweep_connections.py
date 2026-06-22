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

from datetime import datetime

from linkedin.actions.connections import scrape_connections
from linkedin.actions.conversations import get_conversation
from linkedin.conf import CONNECTION_SWEEP_INTERVAL_HOURS, ENABLE_SWEEP_CONNECTIONS
from linkedin.db.deals import set_profile_state
from linkedin.db.urls import url_to_public_id
from linkedin.enums import ProfileState
from linkedin.models import ActionLog, Task, active_day_start
from linkedin.notifications.slack import (
    latest_reply_from_lead,
    notify_connection_accepted,
    notify_sweep_summary,
)

logger = logging.getLogger(__name__)


def handle_sweep_connections(task, session, qualifiers):
    if not ENABLE_SWEEP_CONNECTIONS:
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

        # Pull conversation history so we can persist last_reply_at on the Deal
        # (drives Attio's Prospecting → Qualification stage transition) and
        # surface any reply text in the Slack notification. One extra LinkedIn
        # API call per match — cheap because matches are rare.
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

        try:
            # Operator = the human running this LinkedIn account (Chuka /
            # Arian). Derived from the session's LinkedInProfile.user so the
            # Slack notification tells the team whose lead just accepted.
            user = getattr(session.linkedin_profile, "user", None)
            operator = (user.first_name or user.username) if user else ""
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
        # If LinkedIn reports an older connected_on date, we missed the event —
        # don't further delay the follow-up beyond the ML-recommended cadence.
        if entry.connected_on:
            age_days = (now.date() - entry.connected_on).days
            if age_days > 0:
                logger.debug(
                    "%s accepted %d day(s) ago — follow-up in %.0fs",
                    public_id, age_days, delay_seconds,
                )

        from linkedin.operators import resolve_operator
        operator = resolve_operator(session.linkedin_profile.linkedin_username)
        enqueue_follow_up(
            deal.campaign.pk,
            public_id,
            operator=operator,
            delay_seconds=delay_seconds,
        )
        from gmail.handoff import maybe_schedule_gmail_sequence

        maybe_schedule_gmail_sequence(deal=deal, operator=operator)
        matched += 1

    logger.info(
        "sweep_connections: %d pending → %d newly connected (of %d on-page)",
        pending_deals.count(), matched, len(entries),
    )

    # Lean per-sender analytics snapshot to the ops channel — one post per
    # sweep, so its cadence is CONNECTION_SWEEP_INTERVAL_HOURS for free.
    _post_sweep_summary(session, newly_connected=matched)

    # Self-reschedule.
    enqueue_sweep_connections(delay_seconds=CONNECTION_SWEEP_INTERVAL_HOURS * 3600)


def enqueue_sweep_connections(delay_seconds: float | None = None):
    """Ensure one pending sweep_connections task exists; do not duplicate."""
    if not ENABLE_SWEEP_CONNECTIONS:
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


def _post_sweep_summary(session, newly_connected: int) -> None:
    """Gather minimal per-sender send counts and post them to the ops
    Slack channel. Best-effort — any failure here is logged and never
    disturbs the sweep.

    All counts are scoped to the account that ran this sweep:
      - sends today from ActionLog (this LinkedInProfile).
    """
    from crm.models import Deal
    from linkedin.operators import resolve_operator

    try:
        today_start = active_day_start()
        connects_today = ActionLog.objects.filter(
            linkedin_profile=session.linkedin_profile,
            action_type=ActionLog.ActionType.CONNECT,
            created_at__gte=today_start,
        ).count()
        followups_today = ActionLog.objects.filter(
            linkedin_profile=session.linkedin_profile,
            action_type=ActionLog.ActionType.FOLLOW_UP,
            created_at__gte=today_start,
        ).count()
        connect_runs_today = Task.objects.filter(
            task_type=Task.TaskType.CONNECT,
            payload__campaign_id__in=[c.pk for c in session.campaigns],
            started_at__gte=today_start,
        ).count()
        qualified = Deal.objects.filter(
            campaign__in=session.campaigns,
            state=ProfileState.QUALIFIED,
            lead__disqualified=False,
        ).count()

        notify_sweep_summary(
            sender=resolve_operator(session.linkedin_profile.linkedin_username),
            connects_today=connects_today,
            followups_today=followups_today,
            connect_runs_today=connect_runs_today,
            qualified=qualified,
            newly_connected=newly_connected,
        )
    except Exception as e:
        logger.warning("sweep summary post failed: %s", e)
