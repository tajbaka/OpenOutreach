"""Retired host flags cannot override campaign channels or worker startup."""
from argparse import Namespace
from datetime import datetime
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

import daemon_supervisor as supervisor


@pytest.fixture(autouse=True)
def retired_flags(monkeypatch):
    monkeypatch.setenv("ENABLE_LINKEDIN_DAEMON", "false")
    monkeypatch.setenv("ENABLE_GMAIL_SEQUENCE", "false")
    monkeypatch.setenv("ENABLE_LINKEDIN_FEED_COLLECTOR", "false")
    monkeypatch.setenv("LINKEDIN_USERNAME", "chukyjack@gmail.com")


def test_daemon_launch_remains_safety_registered_with_retired_flag(monkeypatch):
    safety = SimpleNamespace(launch=Mock())
    monkeypatch.setattr(supervisor, "_safety", safety)
    assert supervisor._start_daemon(restart_reason="sender_restart") is safety.launch.return_value
    args, kwargs = safety.launch.call_args
    assert args[0] == [supervisor.sys.executable, "manage.py"]
    assert kwargs["cwd"] == supervisor.ROOT_DIR
    assert kwargs["env"]["OPENOUTREACH_SUPERVISED"] == "1"
    assert kwargs["env"]["OPENOUTREACH_RESTART_REASON"] == "sender_restart"


def test_feed_uses_its_own_setting_with_retired_daemon_flag(monkeypatch):
    now = datetime(2026, 11, 16, 10, tzinfo=ZoneInfo("America/Toronto"))
    schedule = SimpleNamespace(due=Mock(return_value=True))
    assert supervisor._feed_collection_should_start(now, schedule=schedule) is False
    schedule.due.assert_not_called()
    monkeypatch.setenv("ENABLE_LINKEDIN_FEED_COLLECTOR", "true")
    assert supervisor._feed_collection_should_start(now, schedule=schedule) is True
    schedule.due.assert_called_once_with(now)


@pytest.mark.parametrize("failed_worker", [None, "daemon", "run_gmail_worker"])
def test_supervision_starts_and_restarts_both_workers_with_retired_flags(monkeypatch, failed_worker):
    clock = SimpleNamespace(now=0)
    stopped = Event()
    safety = SimpleNamespace(stopped=stopped, raise_if_failed=lambda: None)
    launches, notices, polls = [], [], []
    monkeypatch.setattr(supervisor, "_safety", None)
    monkeypatch.setattr(supervisor.signal, "signal", lambda *args: None)
    monkeypatch.setattr(supervisor.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(supervisor, "_maybe_pull_update", lambda args: False)
    monkeypatch.setattr(supervisor, "_stop_daemon", lambda proc: None)
    monkeypatch.setattr(supervisor, "_stop_process", lambda proc, **kwargs: None)
    monkeypatch.setattr(supervisor, "_notify", lambda title, detail: notices.append(title))
    monkeypatch.setattr(supervisor, "_feed_schedule", Mock(side_effect=AssertionError("No feed jobs")))

    def sleep(seconds):
        clock.now += seconds
        assert clock.now < 80, "Supervisor did not stop"

    def kind(args):
        return args[2] if len(args) > 2 else "daemon"

    def launch(args, **kwargs):
        worker = kind(args)
        first_of_kind = not any(kind(previous) == worker for previous in launches)
        launches.append(args)
        return SimpleNamespace(poll=lambda: 1 if first_of_kind and failed_worker == worker else None)

    def controls(args, restart):
        polls.append(clock.now)
        if len(polls) == 1:
            assert restart("sender_restart") is True
        elif len(polls) == 2:
            assert restart("git_pull") is True
        elif clock.now >= 65:
            stopped.set()

    monkeypatch.setattr(supervisor.time, "sleep", sleep)
    monkeypatch.setattr(supervisor, "_launch", launch)
    monkeypatch.setattr(supervisor, "_poll_runtime_controls", controls)
    args = Namespace(poll_seconds=5, restart_delay=1)
    assert supervisor._supervise_workers(args, safety) == 0
    assert {kind(args) for args in launches} == {"daemon", "run_gmail_worker"}
    assert sum(kind(args) == "daemon" for args in launches) >= 3
    assert sum(kind(args) == "run_gmail_worker" for args in launches) >= 3
    for args in launches:
        assert args[2:] == ([] if kind(args) == "daemon" else ["run_gmail_worker", "--account", "eddy_boundera"])
    assert len(notices) == (1 if failed_worker else 0)


def test_direct_daemon_reaches_startup_checks_with_retired_flag(monkeypatch):
    import runpy

    monkeypatch.setattr(supervisor.sys, "argv", ["manage.py"])
    monkeypatch.setenv("OPENOUTREACH_SUPERVISED", "1")
    # Stop before migrations, CRM initialization, or browser startup.
    check = Mock(side_effect=RuntimeError("Reached normal startup checks"))
    monkeypatch.setattr("linkedin.env_check.check_env_vars", check)
    with pytest.raises(RuntimeError, match="Reached normal startup checks"):
        runpy.run_path(str(supervisor.ROOT_DIR / "manage.py"), run_name="__main__")
    check.assert_called_once_with()
