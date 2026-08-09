from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from linkedin import conf
from linkedin.discovery.config import (
    discovery_day_bounds,
    discovery_gate_open,
    discovery_limits,
    next_discovery_day_start,
    weekday_connection_work_complete,
)
from linkedin.exceptions import DiscoveryConfigurationError
from linkedin.models import ActionLog


def _configure(monkeypatch):
    monkeypatch.setattr(conf, "ENABLE_PROFILE_DISCOVERY", True)
    monkeypatch.setattr(conf, "ENABLE_CONNECT", True)
    monkeypatch.setattr(conf, "ENABLE_AUTO_DISCOVERY", False)
    monkeypatch.setattr(conf, "LLM_API_KEY", "test-key")
    monkeypatch.setattr(conf, "AI_MODEL", "test-model")
    monkeypatch.setattr(conf, "ACTIVE_TIMEZONE", "America/Toronto")
    monkeypatch.setattr(conf, "REST_DAYS", (5, 6))
    monkeypatch.setattr(conf, "CONNECT_DAILY_LIMIT", None)
    monkeypatch.setattr(conf, "DISCOVERY_DAILY_LIMIT", 25)
    monkeypatch.setattr(conf, "DISCOVERY_CONNECT_LIMIT_GRACE", 5)
    monkeypatch.setattr(conf, "DISCOVERY_MAX_CARDS_PER_RUN", 200)
    monkeypatch.setattr(conf, "DISCOVERY_MAX_PAGES_PER_RUN", 10)
    monkeypatch.setattr(conf, "DISCOVERY_MAX_PROFILE_VISITS_PER_RUN", 40)
    monkeypatch.setattr(conf, "DISCOVERY_MAX_CONSECUTIVE_NO_MATCHES", 75)
    monkeypatch.setattr(conf, "DISCOVERY_MAX_RUN_MINUTES", 120)
    monkeypatch.setattr(conf, "DISCOVERY_PROFILE_DELAY_MIN_SECONDS", 20)
    monkeypatch.setattr(conf, "DISCOVERY_PROFILE_DELAY_MAX_SECONDS", 45)


@pytest.mark.django_db
def test_weekday_waits_until_within_grace_of_connect_limit(
    fake_session,
    monkeypatch,
):
    from tests.factories import DealFactory
    from linkedin.enums import ProfileState

    _configure(monkeypatch)
    fake_session.linkedin_profile.connect_daily_limit = 20
    fake_session.linkedin_profile.save(update_fields=["connect_daily_limit"])
    DealFactory(campaign=fake_session.campaign, state=ProfileState.READY_TO_CONNECT)
    now = datetime(2026, 7, 29, 12, 0, tzinfo=ZoneInfo("America/Toronto"))

    assert not weekday_connection_work_complete(fake_session.linkedin_profile, now=now)
    logs = ActionLog.objects.bulk_create(
        [
            ActionLog(
                linkedin_profile=fake_session.linkedin_profile,
                campaign=fake_session.campaign,
                action_type=ActionLog.ActionType.CONNECT,
            )
            for _ in range(15)
        ],
    )
    ActionLog.objects.filter(pk__in=[log.pk for log in logs]).update(created_at=now)

    assert weekday_connection_work_complete(fake_session.linkedin_profile, now=now)
    assert discovery_gate_open(fake_session.linkedin_profile, now=now)


@pytest.mark.django_db
def test_weekday_opens_when_connect_lane_has_no_work(fake_session, monkeypatch):
    _configure(monkeypatch)
    now = datetime(2026, 7, 29, 10, 0, tzinfo=ZoneInfo("America/Toronto"))

    assert discovery_gate_open(fake_session.linkedin_profile, now=now)


@pytest.mark.django_db
def test_rest_day_is_free_without_connection_progress(fake_session, monkeypatch):
    _configure(monkeypatch)
    saturday = datetime(2026, 8, 1, 1, 0, tzinfo=ZoneInfo("America/Toronto"))

    assert discovery_gate_open(fake_session.linkedin_profile, now=saturday)


def test_next_discovery_day_is_local_midnight(monkeypatch):
    _configure(monkeypatch)
    current = datetime(2026, 7, 31, 22, 0, tzinfo=ZoneInfo("America/Toronto"))

    assert next_discovery_day_start(current) == datetime(
        2026,
        8,
        1,
        0,
        0,
        tzinfo=ZoneInfo("America/Toronto"),
    )


def test_day_bounds_use_active_timezone(monkeypatch):
    _configure(monkeypatch)
    utc = ZoneInfo("UTC")

    start, end = discovery_day_bounds(datetime(2026, 7, 30, 2, 0, tzinfo=utc))

    assert start.date().isoformat() == "2026-07-29"
    assert (end - start).total_seconds() == 86400


def test_invalid_limit_configuration_fails(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(conf, "DISCOVERY_DAILY_LIMIT", 0)

    with pytest.raises(DiscoveryConfigurationError, match="must be positive"):
        discovery_limits()


def test_discovery_requires_llm_configuration(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(conf, "LLM_API_KEY", "")

    with pytest.raises(DiscoveryConfigurationError, match="LLM_API_KEY and AI_MODEL"):
        discovery_limits()
