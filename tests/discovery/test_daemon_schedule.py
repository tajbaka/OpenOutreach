from contextlib import ExitStack
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from linkedin.daemon import _claimable_task_types_now, seconds_until_active
from linkedin.models import Task


def _schedule(now, *, discovery_available):
    return (
        patch("linkedin.daemon.ENABLE_ACTIVE_HOURS", True),
        patch("linkedin.daemon.ACTIVE_TIMEZONE", "UTC"),
        patch("linkedin.daemon.ACTIVE_START_HOUR", 9),
        patch("linkedin.daemon.ACTIVE_END_HOUR", 17),
        patch("linkedin.daemon.REST_DAYS", (5, 6)),
        patch("linkedin.daemon.ENABLE_PACING_CATCH_UP", False),
        patch("linkedin.daemon.timezone.localtime", return_value=now),
        patch(
            "linkedin.daemon._discovery_available_now",
            return_value=discovery_available,
        ),
    )


def _enter(patches):
    stack = ExitStack()
    for patcher in patches:
        stack.enter_context(patcher)
    return stack


def test_weekday_excludes_discovery_before_connect_work_finishes():
    now = datetime(2026, 7, 29, 12, 0, tzinfo=ZoneInfo("UTC"))
    patches = _schedule(now, discovery_available=False)
    with _enter(patches):
        claimable = _claimable_task_types_now(object())

    assert Task.TaskType.CONNECT in claimable
    assert Task.TaskType.DISCOVERY not in claimable


def test_weekday_allows_discovery_as_soon_as_connect_work_finishes():
    now = datetime(2026, 7, 29, 12, 0, tzinfo=ZoneInfo("UTC"))
    patches = _schedule(now, discovery_available=True)
    with _enter(patches):
        assert _claimable_task_types_now(object()) is None


def test_rest_day_claims_discovery_at_any_hour():
    now = datetime(2026, 8, 1, 2, 0, tzinfo=ZoneInfo("UTC"))
    patches = _schedule(now, discovery_available=True)
    with _enter(patches):
        assert _claimable_task_types_now(object()) == {Task.TaskType.DISCOVERY}


def test_daemon_stays_awake_when_dynamic_discovery_is_available():
    now = datetime(2026, 8, 1, 2, 0, tzinfo=ZoneInfo("UTC"))
    patches = _schedule(now, discovery_available=True)
    with _enter(patches):
        assert seconds_until_active(object()) == 0
