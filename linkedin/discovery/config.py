"""Configuration and time-window helpers for profile discovery."""
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
        ZoneInfo(conf.DISCOVERY_TIMEZONE)
    except ZoneInfoNotFoundError as exc:
        raise DiscoveryConfigurationError(
            f"Unknown DISCOVERY_TIMEZONE: {conf.DISCOVERY_TIMEZONE!r}",
        ) from exc

    hour_pairs = (
        (
            "DISCOVERY_WEEKDAY_START_HOUR",
            conf.DISCOVERY_WEEKDAY_START_HOUR,
            "DISCOVERY_WEEKDAY_END_HOUR",
            conf.DISCOVERY_WEEKDAY_END_HOUR,
        ),
        (
            "DISCOVERY_REST_DAY_START_HOUR",
            conf.DISCOVERY_REST_DAY_START_HOUR,
            "DISCOVERY_REST_DAY_END_HOUR",
            conf.DISCOVERY_REST_DAY_END_HOUR,
        ),
    )
    for start_name, start, end_name, end in hour_pairs:
        if not 0 <= start <= 23:
            raise DiscoveryConfigurationError(f"{start_name} must be within 0..23")
        if not 1 <= end <= 24:
            raise DiscoveryConfigurationError(f"{end_name} must be within 1..24")
        if start >= end:
            raise DiscoveryConfigurationError(
                f"{start_name} must be earlier than {end_name}",
            )

    if (
        conf.ENABLE_PROFILE_DISCOVERY
        and not conf.ENABLE_ACTIVE_HOURS
    ):
        raise DiscoveryConfigurationError(
            "ENABLE_ACTIVE_HOURS must be true when profile discovery is enabled",
        )

    if (
        conf.ENABLE_PROFILE_DISCOVERY
        and (not conf.LLM_API_KEY or not conf.AI_MODEL)
    ):
        raise DiscoveryConfigurationError(
            "LLM_API_KEY and AI_MODEL are required when profile discovery is enabled",
        )

    if (
        conf.ENABLE_PROFILE_DISCOVERY
        and conf.DISCOVERY_TIMEZONE != conf.ACTIVE_TIMEZONE
    ):
        raise DiscoveryConfigurationError(
            "DISCOVERY_TIMEZONE must match ACTIVE_TIMEZONE so discovery cannot "
            "overlap outbound hours",
        )

    if (
        conf.ENABLE_ACTIVE_HOURS
        and conf.DISCOVERY_WEEKDAY_START_HOUR < conf.ACTIVE_END_HOUR
    ):
        raise DiscoveryConfigurationError(
            "DISCOVERY_WEEKDAY_START_HOUR must be at or after ACTIVE_END_HOUR",
        )

    positive = {
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
    tz = ZoneInfo(conf.DISCOVERY_TIMEZONE)
    value = now or timezone.now()
    if timezone.is_naive(value):
        value = timezone.make_aware(value, timezone=tz)
    return timezone.localtime(value, timezone=tz)


def discovery_day_bounds(now: datetime | None = None) -> tuple[datetime, datetime]:
    local = discovery_local_now(now)
    tz = ZoneInfo(conf.DISCOVERY_TIMEZONE)
    start = datetime.combine(local.date(), time.min, tzinfo=tz)
    return start, start + timedelta(days=1)


def discovery_window_open(now: datetime | None = None) -> bool:
    if not conf.ENABLE_PROFILE_DISCOVERY:
        return False
    local = discovery_local_now(now)
    if local.weekday() in conf.REST_DAYS:
        if not conf.DISCOVERY_RUN_ON_REST_DAYS:
            return False
        start = conf.DISCOVERY_REST_DAY_START_HOUR
        end = conf.DISCOVERY_REST_DAY_END_HOUR
    else:
        start = conf.DISCOVERY_WEEKDAY_START_HOUR
        end = conf.DISCOVERY_WEEKDAY_END_HOUR
    return start <= local.hour < end


def discovery_window_end(now: datetime | None = None) -> datetime | None:
    if not discovery_window_open(now):
        return None
    local = discovery_local_now(now)
    end_hour = (
        conf.DISCOVERY_REST_DAY_END_HOUR
        if local.weekday() in conf.REST_DAYS
        else conf.DISCOVERY_WEEKDAY_END_HOUR
    )
    if end_hour == 24:
        return local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
            days=1,
        )
    return local.replace(hour=end_hour, minute=0, second=0, microsecond=0)


def next_discovery_window_start(
    now: datetime | None = None,
    *,
    after_current_day: bool = False,
) -> datetime | None:
    """Return the next configured discovery start, or now when already open."""
    if not conf.ENABLE_PROFILE_DISCOVERY:
        return None
    local = discovery_local_now(now)
    if discovery_window_open(local) and not after_current_day:
        return local

    tz = ZoneInfo(conf.DISCOVERY_TIMEZONE)
    for offset in range(0, 9):
        candidate_date = local.date() + timedelta(days=offset)
        if after_current_day and candidate_date == local.date():
            continue
        is_rest_day = candidate_date.weekday() in conf.REST_DAYS
        if is_rest_day and not conf.DISCOVERY_RUN_ON_REST_DAYS:
            continue
        start_hour = (
            conf.DISCOVERY_REST_DAY_START_HOUR
            if is_rest_day
            else conf.DISCOVERY_WEEKDAY_START_HOUR
        )
        candidate = datetime.combine(
            candidate_date,
            time(hour=start_hour),
            tzinfo=tz,
        )
        if candidate > local:
            return candidate
    return None
