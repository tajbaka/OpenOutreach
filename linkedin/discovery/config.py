"""Configuration, daily boundaries, and weekday discovery gating."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.utils import timezone

from linkedin import conf
from linkedin.exceptions import DiscoveryConfigurationError


@dataclass(frozen=True)
class DiscoveryLimits:
    max_cards: int
    max_sections: int
    max_scroll_rounds: int
    max_consecutive_empty_scrolls: int
    max_profile_recommendations_per_visit: int
    max_profile_visits: int
    max_consecutive_no_matches: int
    max_run_minutes: int
    delay_min_seconds: int
    delay_max_seconds: int


def validate_discovery_settings() -> None:
    try:
        ZoneInfo(conf.ACTIVE_TIMEZONE)
    except ZoneInfoNotFoundError as exc:
        raise DiscoveryConfigurationError(
            f"Unknown ACTIVE_TIMEZONE: {conf.ACTIVE_TIMEZONE!r}",
        ) from exc

    if (
        conf.ENABLE_PROFILE_DISCOVERY
        and (not conf.LLM_API_KEY or not conf.AI_MODEL)
    ):
        raise DiscoveryConfigurationError(
            "LLM_API_KEY and AI_MODEL are required when profile discovery is enabled",
        )

    positive = {
        "DISCOVERY_DAILY_LIMIT": conf.DISCOVERY_DAILY_LIMIT,
        "DISCOVERY_MAX_CARDS_PER_RUN": conf.DISCOVERY_MAX_CARDS_PER_RUN,
        "DISCOVERY_MAX_SECTIONS_PER_RUN": conf.DISCOVERY_MAX_SECTIONS_PER_RUN,
        "DISCOVERY_MAX_SCROLL_ROUNDS_PER_RUN": (
            conf.DISCOVERY_MAX_SCROLL_ROUNDS_PER_RUN
        ),
        "DISCOVERY_MAX_CONSECUTIVE_EMPTY_SCROLLS": (
            conf.DISCOVERY_MAX_CONSECUTIVE_EMPTY_SCROLLS
        ),
        "DISCOVERY_MAX_PROFILE_RECOMMENDATIONS_PER_VISIT": (
            conf.DISCOVERY_MAX_PROFILE_RECOMMENDATIONS_PER_VISIT
        ),
        "DISCOVERY_MAX_PROFILE_VISITS_PER_RUN": (
            conf.DISCOVERY_MAX_PROFILE_VISITS_PER_RUN
        ),
        "DISCOVERY_MAX_CONSECUTIVE_NO_MATCHES": (
            conf.DISCOVERY_MAX_CONSECUTIVE_NO_MATCHES
        ),
        "DISCOVERY_MAX_RUN_MINUTES": conf.DISCOVERY_MAX_RUN_MINUTES,
        "DISCOVERY_PROFILE_DELAY_MIN_SECONDS": (
            conf.DISCOVERY_PROFILE_DELAY_MIN_SECONDS
        ),
        "DISCOVERY_PROFILE_DELAY_MAX_SECONDS": (
            conf.DISCOVERY_PROFILE_DELAY_MAX_SECONDS
        ),
    }
    for name, value in positive.items():
        if value <= 0:
            raise DiscoveryConfigurationError(f"{name} must be positive")

    if not 0 <= conf.DISCOVERY_VISIT_SCORE_THRESHOLD <= 100:
        raise DiscoveryConfigurationError(
            "DISCOVERY_VISIT_SCORE_THRESHOLD must be between 0 and 100",
        )

    if (
        conf.DISCOVERY_PROFILE_DELAY_MIN_SECONDS
        > conf.DISCOVERY_PROFILE_DELAY_MAX_SECONDS
    ):
        raise DiscoveryConfigurationError(
            "DISCOVERY_PROFILE_DELAY_MIN_SECONDS cannot exceed "
            "DISCOVERY_PROFILE_DELAY_MAX_SECONDS",
        )


def discovery_limits() -> DiscoveryLimits:
    validate_discovery_settings()
    return DiscoveryLimits(
        max_cards=conf.DISCOVERY_MAX_CARDS_PER_RUN,
        max_sections=conf.DISCOVERY_MAX_SECTIONS_PER_RUN,
        max_scroll_rounds=conf.DISCOVERY_MAX_SCROLL_ROUNDS_PER_RUN,
        max_consecutive_empty_scrolls=(
            conf.DISCOVERY_MAX_CONSECUTIVE_EMPTY_SCROLLS
        ),
        max_profile_recommendations_per_visit=(
            conf.DISCOVERY_MAX_PROFILE_RECOMMENDATIONS_PER_VISIT
        ),
        max_profile_visits=conf.DISCOVERY_MAX_PROFILE_VISITS_PER_RUN,
        max_consecutive_no_matches=conf.DISCOVERY_MAX_CONSECUTIVE_NO_MATCHES,
        max_run_minutes=conf.DISCOVERY_MAX_RUN_MINUTES,
        delay_min_seconds=conf.DISCOVERY_PROFILE_DELAY_MIN_SECONDS,
        delay_max_seconds=conf.DISCOVERY_PROFILE_DELAY_MAX_SECONDS,
    )


def discovery_local_now(now: datetime | None = None) -> datetime:
    validate_discovery_settings()
    tz = ZoneInfo(conf.ACTIVE_TIMEZONE)
    value = now or timezone.now()
    if timezone.is_naive(value):
        value = timezone.make_aware(value, timezone=tz)
    return timezone.localtime(value, timezone=tz)


def discovery_day_bounds(now: datetime | None = None) -> tuple[datetime, datetime]:
    local = discovery_local_now(now)
    tz = ZoneInfo(conf.ACTIVE_TIMEZONE)
    start = datetime.combine(local.date(), time.min, tzinfo=tz)
    return start, start + timedelta(days=1)


def discovery_day_end(now: datetime | None = None) -> datetime:
    _start, end = discovery_day_bounds(now)
    return end


def next_discovery_day_start(now: datetime | None = None) -> datetime | None:
    if not conf.ENABLE_PROFILE_DISCOVERY:
        return None
    return discovery_day_end(now)


def _connectable_work_exists(profile, campaign_ids: list[int]) -> bool:
    if conf.ENABLE_AUTO_DISCOVERY:
        return True
    from crm.models import Deal
    from linkedin.enums import ProfileState

    return Deal.objects.filter(
        campaign_id__in=campaign_ids,
        lead__disqualified=False,
        state__in=[ProfileState.QUALIFIED, ProfileState.READY_TO_CONNECT],
    ).exists()


def weekday_connection_work_complete(
    profile,
    *,
    now: datetime | None = None,
) -> bool:
    """Whether the sender's connect lane is parked for the rest of the day."""
    if not conf.ENABLE_CONNECT:
        return True

    from linkedin.models import Campaign, Task

    local = discovery_local_now(now)
    campaign_ids = list(
        Campaign.objects.filter(
            user=profile.user,
            status=Campaign.Status.ACTIVE,
        ).values_list("id", flat=True),
    )
    if not campaign_ids:
        return True

    # Empty connect tasks self-reschedule so the lane can recover when new
    # candidates arrive. They are not evidence of unfinished work. Check the
    # actual candidate pool before pacing catch-up or pending-task state so an
    # exhausted pool hands off to discovery immediately.
    if not _connectable_work_exists(profile, campaign_ids):
        return True

    if (
        conf.ENABLE_ACTIVE_HOURS
        and local.hour >= conf.ACTIVE_END_HOUR
        and (
            not conf.ENABLE_PACING_CATCH_UP
            or not _connect_catch_up_active(profile)
        )
    ):
        return True

    connect_tasks = Task.objects.filter(
        task_type=Task.TaskType.CONNECT,
        payload__campaign_id__in=campaign_ids,
    )
    if connect_tasks.filter(status=Task.Status.RUNNING).exists():
        return False

    _start, day_end = discovery_day_bounds(local)
    pending = connect_tasks.filter(status=Task.Status.PENDING)
    if pending.filter(
        scheduled_at__lt=day_end,
    ).exists():
        return False
    if pending.exists():
        return True

    # A missing connect task is recoverable queue drift, not proof that the
    # lane finished. Keep discovery closed so the daemon can heal that queue.
    return False


def _connect_catch_up_active(profile) -> bool:
    from linkedin.models import ActionLog
    from linkedin.tasks.connect import _is_behind_normal_window_pace

    return _is_behind_normal_window_pace(profile, ActionLog.ActionType.CONNECT)


def discovery_gate_open(profile, *, now: datetime | None = None) -> bool:
    """Rest days are free; weekdays wait for this sender's connect lane."""
    if not conf.ENABLE_PROFILE_DISCOVERY:
        return False
    local = discovery_local_now(now)
    if local.weekday() in conf.REST_DAYS:
        return True
    return weekday_connection_work_complete(profile, now=local)
