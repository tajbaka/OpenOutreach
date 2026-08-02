from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import patch

from linkedin import conf
from linkedin.daemon import _claimable_task_types_now, seconds_until_active
from linkedin.models import Task


def _configure(monkeypatch):
    monkeypatch.setattr(conf, "ENABLE_PROFILE_DISCOVERY", True)
    monkeypatch.setattr(conf, "ENABLE_ACTIVE_HOURS", True)
    monkeypatch.setattr(conf, "LLM_API_KEY", "test-key")
    monkeypatch.setattr(conf, "AI_MODEL", "test-model")
    monkeypatch.setattr(conf, "ACTIVE_TIMEZONE", "UTC")
    monkeypatch.setattr(conf, "ACTIVE_END_HOUR", 17)
    monkeypatch.setattr(conf, "DISCOVERY_TIMEZONE", "UTC")
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


def test_weekday_discovery_window_claims_only_discovery(monkeypatch):
    _configure(monkeypatch)
    now = datetime(2026, 7, 29, 19, 0, tzinfo=ZoneInfo("UTC"))
    with (
        patch("linkedin.daemon.ENABLE_ACTIVE_HOURS", True),
        patch("linkedin.daemon.ACTIVE_TIMEZONE", "UTC"),
        patch("linkedin.daemon.ACTIVE_START_HOUR", 9),
        patch("linkedin.daemon.ACTIVE_END_HOUR", 17),
        patch("linkedin.daemon.REST_DAYS", (5, 6)),
        patch("linkedin.daemon.ENABLE_PACING_CATCH_UP", False),
        patch("linkedin.daemon.timezone.localtime", return_value=now),
    ):
        assert _claimable_task_types_now(object()) == {Task.TaskType.DISCOVERY}


def test_rest_day_discovery_window_claims_discovery(monkeypatch):
    _configure(monkeypatch)
    now = datetime(2026, 8, 1, 12, 0, tzinfo=ZoneInfo("UTC"))
    with (
        patch("linkedin.daemon.ENABLE_ACTIVE_HOURS", True),
        patch("linkedin.daemon.ACTIVE_TIMEZONE", "UTC"),
        patch("linkedin.daemon.ACTIVE_START_HOUR", 9),
        patch("linkedin.daemon.ACTIVE_END_HOUR", 17),
        patch("linkedin.daemon.REST_DAYS", (5, 6)),
        patch("linkedin.daemon.ENABLE_PACING_CATCH_UP", False),
        patch("linkedin.daemon.timezone.localtime", return_value=now),
    ):
        assert _claimable_task_types_now(object()) == {Task.TaskType.DISCOVERY}


def test_outbound_catch_up_takes_precedence(monkeypatch):
    _configure(monkeypatch)
    now = datetime(2026, 7, 29, 19, 0, tzinfo=ZoneInfo("UTC"))
    with (
        patch("linkedin.daemon.ENABLE_ACTIVE_HOURS", True),
        patch("linkedin.daemon.ACTIVE_TIMEZONE", "UTC"),
        patch("linkedin.daemon.ACTIVE_START_HOUR", 9),
        patch("linkedin.daemon.ACTIVE_END_HOUR", 17),
        patch("linkedin.daemon.REST_DAYS", (5, 6)),
        patch(
            "linkedin.daemon._catch_up_task_types",
            return_value={Task.TaskType.CONNECT},
        ),
        patch("linkedin.daemon.timezone.localtime", return_value=now),
    ):
        assert _claimable_task_types_now(object()) == {Task.TaskType.CONNECT}


def test_daemon_wakes_for_discovery_start(monkeypatch):
    _configure(monkeypatch)
    now = datetime(2026, 7, 29, 17, 0, tzinfo=ZoneInfo("UTC"))
    with (
        patch("linkedin.daemon.ENABLE_ACTIVE_HOURS", True),
        patch("linkedin.daemon.ACTIVE_TIMEZONE", "UTC"),
        patch("linkedin.daemon.ACTIVE_START_HOUR", 9),
        patch("linkedin.daemon.ACTIVE_END_HOUR", 17),
        patch("linkedin.daemon.REST_DAYS", (5, 6)),
        patch("linkedin.daemon.ENABLE_PACING_CATCH_UP", False),
        patch("linkedin.daemon.timezone.localtime", return_value=now),
    ):
        assert seconds_until_active(object()) == 3600
