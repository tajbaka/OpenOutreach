from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from linkedin import conf
from linkedin.discovery.config import (
    discovery_day_bounds,
    discovery_limits,
    discovery_window_open,
    next_discovery_window_start,
)
from linkedin.exceptions import DiscoveryConfigurationError


def _configure(monkeypatch):
    monkeypatch.setattr(conf, "ENABLE_PROFILE_DISCOVERY", True)
    monkeypatch.setattr(conf, "ENABLE_ACTIVE_HOURS", True)
    monkeypatch.setattr(conf, "LLM_API_KEY", "test-key")
    monkeypatch.setattr(conf, "AI_MODEL", "test-model")
    monkeypatch.setattr(conf, "ACTIVE_TIMEZONE", "America/Toronto")
    monkeypatch.setattr(conf, "ACTIVE_END_HOUR", 17)
    monkeypatch.setattr(conf, "DISCOVERY_TIMEZONE", "America/Toronto")
    monkeypatch.setattr(conf, "DISCOVERY_WEEKDAY_START_HOUR", 18)
    monkeypatch.setattr(conf, "DISCOVERY_WEEKDAY_END_HOUR", 21)
    monkeypatch.setattr(conf, "DISCOVERY_RUN_ON_REST_DAYS", True)
    monkeypatch.setattr(conf, "DISCOVERY_REST_DAY_START_HOUR", 11)
    monkeypatch.setattr(conf, "DISCOVERY_REST_DAY_END_HOUR", 16)
    monkeypatch.setattr(conf, "REST_DAYS", (5, 6))
    monkeypatch.setattr(conf, "DISCOVERY_MAX_CARDS_PER_RUN", 200)
    monkeypatch.setattr(conf, "DISCOVERY_MAX_PAGES_PER_RUN", 10)
    monkeypatch.setattr(conf, "DISCOVERY_MAX_PROFILE_VISITS_PER_RUN", 40)
    monkeypatch.setattr(conf, "DISCOVERY_MAX_CONSECUTIVE_NO_MATCHES", 75)
    monkeypatch.setattr(conf, "DISCOVERY_MAX_RUN_MINUTES", 120)
    monkeypatch.setattr(conf, "DISCOVERY_PROFILE_DELAY_MIN_SECONDS", 20)
    monkeypatch.setattr(conf, "DISCOVERY_PROFILE_DELAY_MAX_SECONDS", 45)


def test_weekday_discovery_opens_only_after_outbound(monkeypatch):
    _configure(monkeypatch)
    tz = ZoneInfo("America/Toronto")

    assert not discovery_window_open(datetime(2026, 7, 29, 17, 59, tzinfo=tz))
    assert discovery_window_open(datetime(2026, 7, 29, 18, 0, tzinfo=tz))
    assert not discovery_window_open(datetime(2026, 7, 29, 21, 0, tzinfo=tz))


def test_rest_day_uses_its_own_window(monkeypatch):
    _configure(monkeypatch)
    tz = ZoneInfo("America/Toronto")

    assert discovery_window_open(datetime(2026, 8, 1, 12, 0, tzinfo=tz))
    assert not discovery_window_open(datetime(2026, 8, 1, 17, 0, tzinfo=tz))


def test_next_window_moves_to_rest_day_window(monkeypatch):
    _configure(monkeypatch)
    tz = ZoneInfo("America/Toronto")
    friday_after_window = datetime(2026, 7, 31, 22, 0, tzinfo=tz)

    result = next_discovery_window_start(friday_after_window)

    assert result == datetime(2026, 8, 1, 11, 0, tzinfo=tz)


def test_day_bounds_use_discovery_timezone(monkeypatch):
    _configure(monkeypatch)
    utc = ZoneInfo("UTC")

    start, end = discovery_day_bounds(datetime(2026, 7, 30, 2, 0, tzinfo=utc))

    assert start.date().isoformat() == "2026-07-29"
    assert (end - start).total_seconds() == 86400


def test_invalid_limit_configuration_fails(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(conf, "DISCOVERY_MAX_CARDS_PER_RUN", 0)

    with pytest.raises(DiscoveryConfigurationError, match="must be positive"):
        discovery_limits()


def test_discovery_requires_active_hours(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(conf, "ENABLE_ACTIVE_HOURS", False)

    with pytest.raises(DiscoveryConfigurationError, match="ENABLE_ACTIVE_HOURS"):
        discovery_limits()


def test_discovery_requires_llm_configuration(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(conf, "LLM_API_KEY", "")

    with pytest.raises(DiscoveryConfigurationError, match="LLM_API_KEY and AI_MODEL"):
        discovery_limits()


def test_discovery_timezone_must_match_outbound_timezone(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(conf, "DISCOVERY_TIMEZONE", "UTC")

    with pytest.raises(DiscoveryConfigurationError, match="must match ACTIVE_TIMEZONE"):
        discovery_limits()
