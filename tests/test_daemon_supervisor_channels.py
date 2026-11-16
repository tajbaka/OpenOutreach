"""The supervisor keeps Gmail running in both LinkedIn startup modes."""
from argparse import Namespace
from datetime import datetime
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

import daemon_supervisor as supervisor


@pytest.fixture(autouse=True)
def sender_settings(monkeypatch):
    monkeypatch.setenv("ENABLE_GMAIL_SEQUENCE", "false")
    monkeypatch.setenv("ENABLE_LINKEDIN_FEED_COLLECTOR", "false")
    monkeypatch.setenv("LINKEDIN_USERNAME", "chukyjack@gmail.com")


@pytest.mark.parametrize("linkedin_enabled", [True, False])
def test_runtime_launch_is_registered_with_sender_safety(monkeypatch, linkedin_enabled):
    monkeypatch.setenv("ENABLE_LINKEDIN_DAEMON", str(linkedin_enabled))
    safety = SimpleNamespace(launch=Mock())
    monkeypatch.setattr(supervisor, "_safety", safety)
    monkeypatch.setenv("OPENOUTREACH_RESTART_REASON", "old_reason")
    assert supervisor._start_runtime_worker(restart_reason="sender_restart") is safety.launch.return_value
    args, kwargs = safety.launch.call_args
    assert args[0][:2] == [supervisor.sys.executable, "manage.py"]
    assert kwargs["cwd"] == supervisor.ROOT_DIR
    assert kwargs["env"]["OPENOUTREACH_SUPERVISED"] == "1"
    if linkedin_enabled:
        assert args[0][2:] == []
        assert kwargs["env"]["OPENOUTREACH_RESTART_REASON"] == "sender_restart"
    else:
        assert args[0][2:] == ["run_email_enrichment", "--operator", "Chuka"]
        assert "OPENOUTREACH_RESTART_REASON" not in kwargs["env"]


def test_feed_requires_both_linkedin_and_feed_settings(monkeypatch):
    now = datetime(2026, 11, 16, 10, tzinfo=ZoneInfo("America/Toronto"))
    schedule = SimpleNamespace(due=Mock(return_value=True))
    assert supervisor._feed_collection_should_start(now, schedule=schedule) is False
    schedule.due.assert_not_called()
    monkeypatch.setenv("ENABLE_LINKEDIN_FEED_COLLECTOR", "true")
    monkeypatch.setenv("ENABLE_LINKEDIN_DAEMON", "false")
    assert supervisor._feed_collection_should_start(now, schedule=schedule) is False
    schedule.due.assert_not_called()
    monkeypatch.setenv("ENABLE_LINKEDIN_DAEMON", "true")
    assert supervisor._feed_collection_should_start(now, schedule=schedule) is True
    schedule.due.assert_called_once_with(now)


@pytest.mark.parametrize("runtime_args", [[], ["run_email_enrichment", "--operator", "Chuka"]])
@pytest.mark.parametrize("failed_worker", [None, "runtime", "gmail"])
def test_supervision_keeps_the_selected_mode_across_restarts(monkeypatch, runtime_args, failed_worker):
    monkeypatch.setenv("ENABLE_LINKEDIN_DAEMON", "false" if runtime_args else "true")
    if runtime_args:
        monkeypatch.setenv("ENABLE_LINKEDIN_FEED_COLLECTOR", "true")
    gmail_args = ["run_gmail_worker", "--account", "eddy_boundera"]
    expected_commands = {"runtime": runtime_args, "gmail": gmail_args}
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

    def launch(args, **kwargs):
        command = args[2:]
        assert command in expected_commands.values()
        worker = "gmail" if command == gmail_args else "runtime"
        first_of_kind = command not in launches
        launches.append(command)
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
    for worker, command in expected_commands.items():
        assert launches.count(command) == (4 if failed_worker == worker else 3)
    assert len(notices) == (1 if failed_worker else 0)


def test_direct_daemon_exits_before_startup_when_disabled(monkeypatch):
    import runpy

    monkeypatch.setattr("linkedin.conf.ENABLE_LINKEDIN_DAEMON", False)
    monkeypatch.setattr(supervisor.sys, "argv", ["manage.py"])
    monkeypatch.setenv("OPENOUTREACH_SUPERVISED", "1")
    # Stop before migrations, CRM initialization, or browser startup.
    check = Mock(side_effect=RuntimeError("Reached normal startup checks"))
    monkeypatch.setattr("linkedin.env_check.check_env_vars", check)
    with pytest.raises(SystemExit) as exited:
        runpy.run_path(str(supervisor.ROOT_DIR / "manage.py"), run_name="__main__")
    assert exited.value.code == 0
    check.assert_not_called()
