"""Tests for in-daemon degraded-state detection (linkedin/monitoring/degraded.py).

Covers the consecutive-failure tracker and the realtime-listener staleness
check, including the in-process re-alert cooldown shared by both.
"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from linkedin.monitoring import degraded


@pytest.fixture(autouse=True)
def _reset_cooldown():
    """Each test starts with an empty re-alert cooldown cache."""
    degraded._LAST_ALERTED.clear()
    yield
    degraded._LAST_ALERTED.clear()


@pytest.fixture
def patched_notify():
    with patch("linkedin.monitoring.degraded.notify_degraded") as m:
        yield m


class TestTaskFailureTracker:
    def test_alerts_at_threshold(self, patched_notify):
        t = degraded.TaskFailureTracker("Arian")
        for _ in range(5):  # TASK_FAILURE_STREAK_THRESHOLD default 5
            t.record_failure()
        patched_notify.assert_called_once()
        assert patched_notify.call_args.kwargs["sender"] == "Arian"

    def test_below_threshold_is_silent(self, patched_notify):
        t = degraded.TaskFailureTracker("Arian")
        for _ in range(4):
            t.record_failure()
        patched_notify.assert_not_called()

    def test_success_resets_streak(self, patched_notify):
        t = degraded.TaskFailureTracker("Arian")
        for _ in range(4):
            t.record_failure()
        t.record_success()
        for _ in range(4):
            t.record_failure()
        patched_notify.assert_not_called()

    def test_cooldown_suppresses_repeat(self, patched_notify):
        t = degraded.TaskFailureTracker("Arian")
        for _ in range(10):  # crosses the threshold twice
            t.record_failure()
        patched_notify.assert_called_once()


class TestCheckListenerHeartbeat:
    def test_noop_when_listener_disabled(self, monkeypatch, patched_notify):
        monkeypatch.setattr("linkedin.conf.ENABLE_REALTIME_LISTENER", False)
        degraded.check_listener_heartbeat("Arian", "arian@example.com")
        patched_notify.assert_not_called()

    def test_alerts_on_stale_heartbeat(self, monkeypatch, patched_notify):
        monkeypatch.setattr("linkedin.conf.ENABLE_REALTIME_LISTENER", True)
        stale = timezone.now() - timedelta(hours=2)
        monkeypatch.setattr(
            "linkedin.monitoring.degraded.read_heartbeat", lambda _u: stale,
        )
        degraded.check_listener_heartbeat("Arian", "arian@example.com")
        patched_notify.assert_called_once()
        assert patched_notify.call_args.kwargs["sender"] == "Arian"

    def test_no_alert_on_fresh_heartbeat(self, monkeypatch, patched_notify):
        monkeypatch.setattr("linkedin.conf.ENABLE_REALTIME_LISTENER", True)
        fresh = timezone.now() - timedelta(minutes=1)
        monkeypatch.setattr(
            "linkedin.monitoring.degraded.read_heartbeat", lambda _u: fresh,
        )
        degraded.check_listener_heartbeat("Arian", "arian@example.com")
        patched_notify.assert_not_called()

    def test_no_alert_when_heartbeat_missing(self, monkeypatch, patched_notify):
        # None ⇒ never written yet (startup grace) — not treated as stale.
        monkeypatch.setattr("linkedin.conf.ENABLE_REALTIME_LISTENER", True)
        monkeypatch.setattr(
            "linkedin.monitoring.degraded.read_heartbeat", lambda _u: None,
        )
        degraded.check_listener_heartbeat("Arian", "arian@example.com")
        patched_notify.assert_not_called()
