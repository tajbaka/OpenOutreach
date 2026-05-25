"""Tests for the daemon-startup listener catch-up gap logic."""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.utils import timezone

from linkedin.realtime import catchup


def test_missing_heartbeat_is_infinite_gap():
    with patch("linkedin.realtime.catchup.read_heartbeat", return_value=None):
        assert catchup.compute_gap_minutes("arian@x.com") == float("inf")


def test_recent_heartbeat_is_small_gap():
    recent = timezone.now() - timedelta(minutes=3)
    with patch("linkedin.realtime.catchup.read_heartbeat", return_value=recent):
        gap = catchup.compute_gap_minutes("arian@x.com")
    assert 2 < gap < 4


def test_gap_below_threshold_does_not_prompt_or_backfill():
    recent = timezone.now() - timedelta(minutes=5)
    with patch("linkedin.realtime.catchup.read_heartbeat", return_value=recent), \
         patch("linkedin.realtime.catchup.call_command") as mock_cmd, \
         patch("builtins.input") as mock_input:
        catchup.run_startup_catchup(username="arian@x.com", account_label="primary")
    mock_cmd.assert_not_called()
    mock_input.assert_not_called()


def test_large_gap_headless_logs_warning_no_backfill(caplog):
    old = timezone.now() - timedelta(hours=9)
    with patch("linkedin.realtime.catchup.read_heartbeat", return_value=old), \
         patch("linkedin.realtime.catchup.call_command") as mock_cmd:
        catchup.run_startup_catchup(
            username="arian@x.com", account_label="primary", interactive=False,
        )
    mock_cmd.assert_not_called()
    assert any("listener was off" in r.message.lower() for r in caplog.records)


def test_large_gap_interactive_yes_runs_backfill():
    old = timezone.now() - timedelta(hours=9)
    with patch("linkedin.realtime.catchup.read_heartbeat", return_value=old), \
         patch("linkedin.realtime.catchup.call_command") as mock_cmd, \
         patch("builtins.input", return_value="y"):
        catchup.run_startup_catchup(
            username="arian@x.com", account_label="primary", interactive=True,
        )
    mock_cmd.assert_called_once_with(
        "backfill_messages", account="primary", skip_prereq_gate=True,
    )


def test_large_gap_interactive_no_skips_backfill():
    old = timezone.now() - timedelta(hours=9)
    with patch("linkedin.realtime.catchup.read_heartbeat", return_value=old), \
         patch("linkedin.realtime.catchup.call_command") as mock_cmd, \
         patch("builtins.input", return_value="n"):
        catchup.run_startup_catchup(
            username="arian@x.com", account_label="primary", interactive=True,
        )
    mock_cmd.assert_not_called()


def test_git_pull_restart_bypasses_interactive_prompt(monkeypatch):
    old = timezone.now() - timedelta(hours=9)
    monkeypatch.setenv("OPENOUTREACH_RESTART_REASON", "git_pull")

    with patch("linkedin.realtime.catchup.read_heartbeat", return_value=old), \
         patch("linkedin.realtime.catchup.call_command") as mock_cmd, \
         patch("builtins.input") as mock_input:
        catchup.run_startup_catchup(
            username="arian@x.com", account_label="primary", interactive=True,
        )

    mock_cmd.assert_not_called()
    mock_input.assert_not_called()


def test_interactive_none_defers_to_tty_detection():
    """interactive=None resolves via sys.stdin.isatty() at call time."""
    old = timezone.now() - timedelta(hours=9)
    with patch("linkedin.realtime.catchup.read_heartbeat", return_value=old), \
         patch("linkedin.realtime.catchup.call_command") as mock_cmd, \
         patch("sys.stdin.isatty", return_value=True), \
         patch("builtins.input", return_value="y"):
        catchup.run_startup_catchup(username="arian@x.com", account_label="primary")
    mock_cmd.assert_called_once_with(
        "backfill_messages", account="primary", skip_prereq_gate=True,
    )
