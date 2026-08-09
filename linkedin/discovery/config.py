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
    max_pages: int
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
        "DISCOVERY_MAX_PAGES_PER_RUN": conf.DISCOVERY_MAX_PAGES_PER_RUN,
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

    if conf.DISCOVERY_CONNECT_LIMIT_GRACE < 0:
        raise DiscoveryConfigurationError(
            "DISCOVERY_CONNECT_LIMIT_GRACE cannot be negative",
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
        max_pages=conf.DISCOVERY_MAX_PAGES_PER_RUN,
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


def _connectable_work_exists(profile) -> bool:
    if conf.ENABLE_AUTO_DISCOVERY:
        return True
    from crm.models import Deal
    from linkedin.enums import ProfileState
    from linkedin.models import Campaign

    return Deal.objects.filter(
        campaign__user=profile.user,
        campaign__status=Campaign.Status.ACTIVE,
        lead__disqualified=False,
        state__in=[ProfileState.QUALIFIED, ProfileState.READY_TO_CONNECT],
    ).exists()


def weekday_connection_work_complete(
    profile,
    *,
    now: datetime | None = None,
) -> bool:
    """Whether weekday connection work is done enough to yield to discovery."""
    if not conf.ENABLE_CONNECT:
        return True

    from linkedin.models import ActionLog

    daily_limit = conf.CONNECT_DAILY_LIMIT or profile.connect_daily_limit
    threshold = max(daily_limit - conf.DISCOVERY_CONNECT_LIMIT_GRACE, 1)
    start, end = discovery_day_bounds(now)
    sent = ActionLog.objects.filter(
        linkedin_profile=profile,
        action_type=ActionLog.ActionType.CONNECT,
        created_at__gte=start,
        created_at__lt=end,
    ).count()
    if sent >= threshold:
        return True
    if not profile.can_execute(ActionLog.ActionType.CONNECT):
        return True
    return not _connectable_work_exists(profile)


def discovery_gate_open(profile, *, now: datetime | None = None) -> bool:
    """Rest days are free; weekdays wait for this sender's connect lane."""
    if not conf.ENABLE_PROFILE_DISCOVERY:
        return False
    local = discovery_local_now(now)
    if local.weekday() in conf.REST_DAYS:
        return True
    return weekday_connection_work_complete(profile, now=local)
