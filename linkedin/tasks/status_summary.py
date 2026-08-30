"""Hourly all-sender Slack status summary."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.utils import timezone

from linkedin.conf import (
    ACTIVE_END_HOUR,
    ACTIVE_START_HOUR,
    ACTIVE_TIMEZONE,
    ENABLE_ACTIVE_HOURS,
    ENABLE_PACING_CATCH_UP,
    EXPECTED_OUTBOUND_SENDERS,
    REST_DAYS,
)
from linkedin.enums import ProfileState
from linkedin.models import (
    ActionLog,
    Campaign,
    DaemonHeartbeat,
    LinkedInProfile,
    Task,
    active_day_start,
)
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


def _active_outbound_profiles():
    profiles = LinkedInProfile.objects.filter(active=True).select_related("user")
    for profile in profiles:
        if Campaign.objects.filter(user=profile.user, status=Campaign.Status.ACTIVE).exists():
            yield profile


def _catch_up_enabled_for_any_sender(*, now) -> bool:
    """Whether any sender is intentionally allowed to send after hours."""
    if not ENABLE_PACING_CATCH_UP:
        return False
    local_now = timezone.localtime(now, timezone=ZoneInfo(ACTIVE_TIMEZONE))
    if local_now.weekday() in REST_DAYS or local_now.hour < ACTIVE_END_HOUR:
        return False

    from linkedin.tasks.connect import _is_behind_normal_window_pace

    for profile in _active_outbound_profiles():
        if (
            _is_behind_normal_window_pace(profile, ActionLog.ActionType.CONNECT)
            or _is_behind_normal_window_pace(profile, ActionLog.ActionType.FOLLOW_UP)
        ):
            return True
    return False


def should_post_status_summary_now(*, now=None) -> tuple[bool, str]:
    """Return whether the hourly sender status is expected to be actionable."""
    if not ENABLE_ACTIVE_HOURS:
        return True, ""
    now = now or timezone.now()
    local_now = timezone.localtime(now, timezone=ZoneInfo(ACTIVE_TIMEZONE))
    is_normal_active = (
        local_now.weekday() not in REST_DAYS
        and ACTIVE_START_HOUR <= local_now.hour < ACTIVE_END_HOUR
    )
    if is_normal_active:
        return True, ""
    if _catch_up_enabled_for_any_sender(now=now):
        return True, ""
    return False, "outside active hours and no pacing catch-up lane is active"


def _gmail_counts_by_sender(today_start) -> dict[str, int]:
    from crm.models import Message

    counts: dict[str, int] = {}
    messages = Message.objects.filter(
        source=Message.Source.GMAIL,
        direction=Message.Direction.OUTBOUND,
        sent_at__gte=today_start,
    ).select_related("operator")
    for message in messages:
        if message.operator_id:
            sender = message.operator.handle
        else:
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


def _profile_for_sender(sender: str):
    profile = (
        LinkedInProfile.objects.filter(active=True)
        .filter(linkedin_username__iexact=sender)
        .first()
    )
    if profile is not None:
        return profile
    for candidate in LinkedInProfile.objects.filter(active=True):
        if resolve_operator(candidate.linkedin_username) == sender:
            return candidate
    return None


def _sender_should_report(sender: str, profile, today_start) -> tuple[bool, str]:
    """Whether this sender should appear in the hourly Slack status summary."""
    heartbeat = DaemonHeartbeat.objects.filter(sender=sender).first()
    if heartbeat is None or heartbeat.last_alive is None or heartbeat.last_alive < today_start:
        return False, "no heartbeat today"
    if profile is None:
        return False, "no active profile"
    if not profile.can_execute(ActionLog.ActionType.CONNECT):
        return False, "connect limit reached"
    return True, ""


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
        profile = _profile_for_sender(sender)
        should_report, reason = _sender_should_report(sender, profile, today_start)
        if not should_report:
            logger.info("status_summary: suppressing %s (%s)", sender, reason)
            continue

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
        should_post, reason = should_post_status_summary_now(now=now)
        if not should_post:
            logger.info("status_summary suppressed: %s", reason)
            return

        rows = build_status_summary_rows(since=since)
        if rows:
            notify_status_summary(rows=rows, since=since, generated_at=now)
            logger.info("status_summary posted for %d sender(s)", len(rows))
        else:
            logger.info("status_summary suppressed: no active sender rows")
    finally:
        enqueue_status_summary(delay_seconds=STATUS_SUMMARY_INTERVAL_SECONDS, since=now)
