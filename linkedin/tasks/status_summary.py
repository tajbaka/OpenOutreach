"""Hourly all-sender Slack status summary."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from django.utils import timezone

from linkedin.conf import EXPECTED_OUTBOUND_SENDERS
from linkedin.enums import ProfileState
from linkedin.models import ActionLog, Campaign, LinkedInProfile, Task, active_day_start
from linkedin.notifications.slack import notify_status_summary
from linkedin.operators import resolve_operator

logger = logging.getLogger(__name__)

STATUS_SUMMARY_INTERVAL_SECONDS = 3600


def _parse_since(value: str | None):
    if not value:
        return timezone.now() - timedelta(seconds=STATUS_SUMMARY_INTERVAL_SECONDS)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return timezone.now() - timedelta(seconds=STATUS_SUMMARY_INTERVAL_SECONDS)
    if timezone.is_naive(parsed):
        return timezone.make_aware(parsed, timezone=timezone.get_current_timezone())
    return parsed


def _expected_senders() -> list[str]:
    configured = [resolve_operator(sender) for sender in EXPECTED_OUTBOUND_SENDERS]
    senders = {sender for sender in configured if sender}
    profiles = LinkedInProfile.objects.filter(active=True).select_related("user")
    for profile in profiles:
        if Campaign.objects.filter(user=profile.user, status=Campaign.Status.ACTIVE).exists():
            sender = resolve_operator(profile.linkedin_username)
            if sender:
                senders.add(sender)
    return sorted(senders)


def _campaign_ids_by_sender() -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    profiles = LinkedInProfile.objects.filter(active=True).select_related("user")
    for profile in profiles:
        sender = resolve_operator(profile.linkedin_username)
        if not sender:
            continue
        campaign_ids = list(
            Campaign.objects.filter(user=profile.user, status=Campaign.Status.ACTIVE)
            .values_list("pk", flat=True)
        )
        if campaign_ids:
            out.setdefault(sender, []).extend(campaign_ids)
    return out


def _gmail_counts_by_sender(today_start) -> dict[str, int]:
    from crm.models import Message

    counts: dict[str, int] = {}
    messages = Message.objects.filter(
        source=Message.Source.GMAIL,
        direction=Message.Direction.OUTBOUND,
        sent_at__gte=today_start,
    )
    for message in messages:
        external_id = message.external_id or ""
        if external_id.startswith("gmail-send:"):
            sender = external_id.split(":", 2)[1]
        else:
            sender = message.sender
        sender = resolve_operator(sender)
        if sender:
            counts[sender] = counts.get(sender, 0) + 1
    return counts


def _manual_reply_counts_by_sender(today_start) -> dict[str, int]:
    from crm.models import Message

    counts: dict[str, int] = {}
    messages = Message.objects.filter(
        source=Message.Source.LINKEDIN,
        direction=Message.Direction.OUTBOUND,
        sent_at__gte=today_start,
        external_id__startswith="manual-reply:",
    )
    for message in messages:
        sender = resolve_operator(message.sender)
        if sender:
            counts[sender] = counts.get(sender, 0) + 1
    return counts


def build_status_summary_rows(*, since) -> list[dict]:
    """Return all per-sender metrics for the Slack status snapshot."""
    from crm.models import Deal

    today_start = active_day_start()
    campaign_ids_by_sender = _campaign_ids_by_sender()
    gmail_counts = _gmail_counts_by_sender(today_start)
    manual_counts = _manual_reply_counts_by_sender(today_start)
    senders = sorted(set(_expected_senders()) | set(gmail_counts) | set(manual_counts))

    rows: list[dict] = []
    for sender in senders:
        campaign_ids = campaign_ids_by_sender.get(sender, [])
        profile = (
            LinkedInProfile.objects.filter(active=True)
            .filter(linkedin_username__iexact=sender)
            .first()
        )
        if profile is None:
            for candidate in LinkedInProfile.objects.filter(active=True):
                if resolve_operator(candidate.linkedin_username) == sender:
                    profile = candidate
                    break

        action_logs = ActionLog.objects.filter(created_at__gte=today_start)
        if profile is not None:
            action_logs = action_logs.filter(linkedin_profile=profile)
        else:
            action_logs = action_logs.none()

        rows.append({
            "sender": sender,
            "connects_today": action_logs.filter(
                action_type=ActionLog.ActionType.CONNECT,
            ).count(),
            "linkedin_followups_today": action_logs.filter(
                action_type=ActionLog.ActionType.FOLLOW_UP,
            ).count(),
            "email_followups_today": gmail_counts.get(sender, 0),
            "manual_replies_today": manual_counts.get(sender, 0),
            "newly_connected": (
                Deal.objects.filter(
                    campaign_id__in=campaign_ids,
                    connected_at__gte=since,
                    lead__disqualified=False,
                ).count()
                if campaign_ids else 0
            ),
            "connect_runs_today": (
                Task.objects.filter(
                    task_type=Task.TaskType.CONNECT,
                    payload__campaign_id__in=campaign_ids,
                    started_at__gte=today_start,
                ).count()
                if campaign_ids else 0
            ),
            "qualified_remaining": (
                Deal.objects.filter(
                    campaign_id__in=campaign_ids,
                    state=ProfileState.QUALIFIED,
                    lead__disqualified=False,
                ).count()
                if campaign_ids else 0
            ),
        })
    return rows


def enqueue_status_summary(*, delay_seconds: float | None = None, since=None) -> None:
    """Ensure one pending account-agnostic status summary task exists."""
    if delay_seconds is None:
        delay_seconds = STATUS_SUMMARY_INTERVAL_SECONDS
    if since is None:
        since = timezone.now()
    if Task.objects.filter(
        task_type=Task.TaskType.STATUS_SUMMARY,
        status=Task.Status.PENDING,
    ).exists():
        return
    Task.objects.create(
        task_type=Task.TaskType.STATUS_SUMMARY,
        scheduled_at=timezone.now() + timedelta(seconds=delay_seconds),
        payload={"since": since.isoformat()},
    )


def handle_status_summary(task, session, qualifiers) -> None:
    """Post the all-sender status summary and schedule the next hourly run."""
    now = timezone.now()
    since = _parse_since((task.payload or {}).get("since"))
    try:
        rows = build_status_summary_rows(since=since)
        notify_status_summary(rows=rows, since=since, generated_at=now)
        logger.info("status_summary posted for %d sender(s)", len(rows))
    finally:
        enqueue_status_summary(delay_seconds=STATUS_SUMMARY_INTERVAL_SECONDS, since=now)
