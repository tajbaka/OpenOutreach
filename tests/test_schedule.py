from __future__ import annotations

from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from linkedin.daemon import seconds_until_active
from linkedin.models import ActionLog, Task


def _mock_now(year, month, day, hour, minute=0, tz="UTC"):
    return datetime(year, month, day, hour, minute, tzinfo=ZoneInfo(tz))


@pytest.fixture(autouse=True)
def _default_schedule(settings):
    """Ensure tests use known schedule defaults."""


class TestSecondsUntilActive:
    @patch("linkedin.daemon.ACTIVE_START_HOUR", 9)
    @patch("linkedin.daemon.ACTIVE_END_HOUR", 17)
    @patch("linkedin.daemon.ACTIVE_TIMEZONE", "UTC")
    @patch("linkedin.daemon.REST_DAYS", (5, 6))
    def test_inside_active_window(self):
        with patch("linkedin.daemon.timezone.localtime", return_value=_mock_now(2026, 3, 18, 12)):
            assert seconds_until_active() == 0.0

    @patch("linkedin.daemon.ACTIVE_START_HOUR", 9)
    @patch("linkedin.daemon.ACTIVE_END_HOUR", 17)
    @patch("linkedin.daemon.ACTIVE_TIMEZONE", "UTC")
    @patch("linkedin.daemon.REST_DAYS", (5, 6))
    def test_before_start(self):
        with patch("linkedin.daemon.timezone.localtime", return_value=_mock_now(2026, 3, 18, 7)):
            result = seconds_until_active()
            assert result == pytest.approx(2 * 3600, abs=1)

    @patch("linkedin.daemon.ACTIVE_START_HOUR", 9)
    @patch("linkedin.daemon.ACTIVE_END_HOUR", 17)
    @patch("linkedin.daemon.ACTIVE_TIMEZONE", "UTC")
    @patch("linkedin.daemon.REST_DAYS", (5, 6))
    def test_after_end(self):
        with patch("linkedin.daemon.timezone.localtime", return_value=_mock_now(2026, 3, 18, 18)):
            result = seconds_until_active()
            assert result == pytest.approx(15 * 3600, abs=1)  # 15h to Thu 9am

    @patch("linkedin.daemon.ACTIVE_START_HOUR", 9)
    @patch("linkedin.daemon.ACTIVE_END_HOUR", 17)
    @patch("linkedin.daemon.ACTIVE_TIMEZONE", "UTC")
    @patch("linkedin.daemon.REST_DAYS", (5, 6))
    def test_friday_evening_skips_weekend(self):
        # Fri Mar 20 2026 is a Friday (weekday=4)
        with patch("linkedin.daemon.timezone.localtime", return_value=_mock_now(2026, 3, 20, 18)):
            result = seconds_until_active()
            # Next active: Mon Mar 23 9am = 63h away
            assert result == pytest.approx(63 * 3600, abs=1)

    @patch("linkedin.daemon.ACTIVE_START_HOUR", 9)
    @patch("linkedin.daemon.ACTIVE_END_HOUR", 17)
    @patch("linkedin.daemon.ACTIVE_TIMEZONE", "UTC")
    @patch("linkedin.daemon.REST_DAYS", (5, 6))
    def test_saturday_skips_to_monday(self):
        # Sat Mar 21 2026 noon
        with patch("linkedin.daemon.timezone.localtime", return_value=_mock_now(2026, 3, 21, 12)):
            result = seconds_until_active()
            # Next active: Mon Mar 23 9am = 45h away
            assert result == pytest.approx(45 * 3600, abs=1)

    @patch("linkedin.daemon.ACTIVE_START_HOUR", 9)
    @patch("linkedin.daemon.ACTIVE_END_HOUR", 17)
    @patch("linkedin.daemon.ACTIVE_TIMEZONE", "Europe/Berlin")
    @patch("linkedin.daemon.REST_DAYS", (5, 6))
    def test_timezone_respected(self):
        # Wed 8am Berlin = still before 9am start
        with patch("linkedin.daemon.timezone.localtime", return_value=_mock_now(2026, 3, 18, 8, tz="Europe/Berlin")):
            result = seconds_until_active()
            assert result == pytest.approx(3600, abs=1)

    @patch("linkedin.daemon.ACTIVE_START_HOUR", 9)
    @patch("linkedin.daemon.ACTIVE_END_HOUR", 17)
    @patch("linkedin.daemon.ACTIVE_TIMEZONE", "UTC")
    @patch("linkedin.daemon.REST_DAYS", ())
    def test_no_rest_days(self):
        # Sat noon, but no rest days configured
        with patch("linkedin.daemon.timezone.localtime", return_value=_mock_now(2026, 3, 21, 12)):
            assert seconds_until_active() == 0.0

    @patch("linkedin.daemon.ACTIVE_START_HOUR", 9)
    @patch("linkedin.daemon.ACTIVE_END_HOUR", 17)
    @patch("linkedin.daemon.ACTIVE_TIMEZONE", "UTC")
    @patch("linkedin.daemon.REST_DAYS", (5, 6))
    def test_at_exact_start(self):
        with patch("linkedin.daemon.timezone.localtime", return_value=_mock_now(2026, 3, 18, 9)):
            assert seconds_until_active() == 0.0

    @patch("linkedin.daemon.ACTIVE_START_HOUR", 9)
    @patch("linkedin.daemon.ACTIVE_END_HOUR", 17)
    @patch("linkedin.daemon.ACTIVE_TIMEZONE", "UTC")
    @patch("linkedin.daemon.REST_DAYS", (5, 6))
    def test_at_exact_end(self):
        with patch("linkedin.daemon.timezone.localtime", return_value=_mock_now(2026, 3, 18, 17)):
            result = seconds_until_active()
            # Should be outside (end is exclusive), next day 9am = 16h
            assert result == pytest.approx(16 * 3600, abs=1)

    @patch("linkedin.daemon.ENABLE_ACTIVE_HOURS", False)
    def test_disabled_always_active(self):
        # Outside hours on a rest day — should still return 0 when disabled
        with patch("linkedin.daemon.timezone.localtime", return_value=_mock_now(2026, 3, 21, 23)):
            assert seconds_until_active() == 0.0

    @patch("linkedin.daemon._is_behind_normal_window_pace", return_value=True)
    @patch("linkedin.daemon.ENABLE_PACING_CATCH_UP", True)
    @patch("linkedin.daemon.ACTIVE_START_HOUR", 9)
    @patch("linkedin.daemon.ACTIVE_END_HOUR", 17)
    @patch("linkedin.daemon.ACTIVE_TIMEZONE", "UTC")
    @patch("linkedin.daemon.REST_DAYS", (5, 6))
    def test_catch_up_keeps_daemon_active_after_end(self, _behind):
        with patch("linkedin.daemon.timezone.localtime", return_value=_mock_now(2026, 3, 18, 19)):
            assert seconds_until_active(object()) == 0.0

    @patch("linkedin.daemon._is_behind_normal_window_pace")
    @patch("linkedin.daemon.ENABLE_PACING_CATCH_UP", True)
    @patch("linkedin.daemon.ACTIVE_START_HOUR", 9)
    @patch("linkedin.daemon.ACTIVE_END_HOUR", 17)
    @patch("linkedin.daemon.ACTIVE_TIMEZONE", "UTC")
    @patch("linkedin.daemon.REST_DAYS", (5, 6))
    def test_followup_catch_up_keeps_daemon_active_after_end(self, behind):
        def side_effect(_profile, action_type):
            return action_type == ActionLog.ActionType.FOLLOW_UP

        behind.side_effect = side_effect
        with patch("linkedin.daemon.timezone.localtime", return_value=_mock_now(2026, 3, 18, 19)):
            assert seconds_until_active(object()) == 0.0

    @patch("linkedin.daemon._is_behind_normal_window_pace")
    @patch("linkedin.daemon.ENABLE_PACING_CATCH_UP", True)
    @patch("linkedin.daemon.ACTIVE_START_HOUR", 9)
    @patch("linkedin.daemon.ACTIVE_END_HOUR", 17)
    @patch("linkedin.daemon.ACTIVE_TIMEZONE", "UTC")
    @patch("linkedin.daemon.REST_DAYS", (5, 6))
    def test_after_hours_followup_only_catch_up_restricts_claim_types(self, behind):
        from linkedin.daemon import _claimable_task_types_now

        def side_effect(_profile, action_type):
            return action_type == ActionLog.ActionType.FOLLOW_UP

        behind.side_effect = side_effect
        with patch("linkedin.daemon.timezone.localtime", return_value=_mock_now(2026, 3, 18, 19)):
            assert _claimable_task_types_now(object()) == {Task.TaskType.FOLLOW_UP}

    @patch("linkedin.daemon._is_behind_normal_window_pace", return_value=False)
    @patch("linkedin.daemon.ENABLE_PACING_CATCH_UP", True)
    @patch("linkedin.daemon.ACTIVE_START_HOUR", 9)
    @patch("linkedin.daemon.ACTIVE_END_HOUR", 17)
    @patch("linkedin.daemon.ACTIVE_TIMEZONE", "UTC")
    @patch("linkedin.daemon.REST_DAYS", (5, 6))
    def test_after_hours_catch_up_sleeps_when_caught_up(self, _behind):
        with patch("linkedin.daemon.timezone.localtime", return_value=_mock_now(2026, 3, 18, 23)):
            result = seconds_until_active(object())
            assert result == pytest.approx(10 * 3600, abs=1)
