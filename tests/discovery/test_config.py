from datetime import datetime, timedelta
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
from linkedin.models import Task


def _configure(monkeypatch):
    monkeypatch.setattr(conf, "ENABLE_PROFILE_DISCOVERY", True)
    monkeypatch.setattr(conf, "ENABLE_CONNECT", True)
    monkeypatch.setattr(conf, "ENABLE_AUTO_DISCOVERY", False)
    monkeypatch.setattr(conf, "ENABLE_ACTIVE_HOURS", True)
    monkeypatch.setattr(conf, "ENABLE_PACING_CATCH_UP", False)
    monkeypatch.setattr(conf, "LLM_API_KEY", "test-key")
    monkeypatch.setattr(conf, "AI_MODEL", "test-model")
    monkeypatch.setattr(conf, "ACTIVE_TIMEZONE", "America/Toronto")
    monkeypatch.setattr(conf, "ACTIVE_START_HOUR", 9)
    monkeypatch.setattr(conf, "ACTIVE_END_HOUR", 17)
    monkeypatch.setattr(conf, "REST_DAYS", (5, 6))
    monkeypatch.setattr(conf, "CONNECT_DAILY_LIMIT", None)
    monkeypatch.setattr(conf, "DISCOVERY_DAILY_LIMIT", 25)
    monkeypatch.setattr(conf, "DISCOVERY_MAX_CARDS_PER_RUN", 200)
    monkeypatch.setattr(conf, "DISCOVERY_MAX_PAGES_PER_RUN", 10)
    monkeypatch.setattr(conf, "DISCOVERY_MAX_PROFILE_VISITS_PER_RUN", 40)
    monkeypatch.setattr(conf, "DISCOVERY_MAX_CONSECUTIVE_NO_MATCHES", 75)
    monkeypatch.setattr(conf, "DISCOVERY_MAX_RUN_MINUTES", 120)
    monkeypatch.setattr(conf, "DISCOVERY_PROFILE_DELAY_MIN_SECONDS", 20)
    monkeypatch.setattr(conf, "DISCOVERY_PROFILE_DELAY_MAX_SECONDS", 45)


@pytest.mark.django_db
def test_weekday_waits_until_connect_task_is_parked_for_next_day(
    fake_session,
    monkeypatch,
):
    from tests.factories import DealFactory
    from linkedin.enums import ProfileState

    _configure(monkeypatch)
    DealFactory(campaign=fake_session.campaign, state=ProfileState.READY_TO_CONNECT)
    Task.objects.filter(task_type=Task.TaskType.CONNECT).delete()
    now = datetime(2026, 7, 29, 12, 0, tzinfo=ZoneInfo("America/Toronto"))
    task = Task.objects.create(
        task_type=Task.TaskType.CONNECT,
        scheduled_at=now + timedelta(hours=1),
        payload={"campaign_id": fake_session.campaign.pk},
    )

    assert not weekday_connection_work_complete(fake_session.linkedin_profile, now=now)
    task.scheduled_at = now + timedelta(days=1)
    task.save(update_fields=["scheduled_at"])

    assert weekday_connection_work_complete(fake_session.linkedin_profile, now=now)
    assert discovery_gate_open(fake_session.linkedin_profile, now=now)


@pytest.mark.django_db
def test_weekday_stays_closed_for_missing_connect_task_with_work(
    fake_session,
    monkeypatch,
):
    from tests.factories import DealFactory
    from linkedin.enums import ProfileState

    _configure(monkeypatch)
    DealFactory(
        campaign=fake_session.campaign,
        state=ProfileState.READY_TO_CONNECT,
    )
    now = datetime(2026, 7, 29, 12, 0, tzinfo=ZoneInfo("America/Toronto"))

    assert not weekday_connection_work_complete(fake_session.linkedin_profile, now=now)


@pytest.mark.django_db
def test_weekday_opens_when_daemon_has_closed_connect_lane_for_day(
    fake_session,
    monkeypatch,
):
    from tests.factories import DealFactory
    from linkedin.enums import ProfileState

    _configure(monkeypatch)
    DealFactory(campaign=fake_session.campaign, state=ProfileState.READY_TO_CONNECT)
    Task.objects.filter(task_type=Task.TaskType.CONNECT).delete()
    now = datetime(2026, 7, 29, 19, 0, tzinfo=ZoneInfo("America/Toronto"))
    Task.objects.create(
        task_type=Task.TaskType.CONNECT,
        scheduled_at=now + timedelta(hours=1),
        payload={"campaign_id": fake_session.campaign.pk},
    )

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
