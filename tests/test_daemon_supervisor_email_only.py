from argparse import Namespace
from datetime import datetime
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

import daemon_supervisor as supervisor


@pytest.fixture
def email_only(monkeypatch):
    monkeypatch.setenv("ENABLE_LINKEDIN_DAEMON", "false")
    monkeypatch.setenv("ENABLE_LINKEDIN_FEED_COLLECTOR", "true")
    monkeypatch.setenv("LINKEDIN_USERNAME", "chukyjack@gmail.com")
    forbidden = Mock(side_effect=AssertionError("LinkedIn must not start"))
    monkeypatch.setattr(supervisor, "_start_daemon", forbidden)
    return forbidden


@pytest.mark.parametrize("value,expected", [(None, True), ("true", True), ("1", True), ("false", False), ("0", False)])
def test_linkedin_daemon_flag_preserves_enabled_default(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("ENABLE_LINKEDIN_DAEMON", raising=False)
    else:
        monkeypatch.setenv("ENABLE_LINKEDIN_DAEMON", value)
    assert supervisor._linkedin_daemon_enabled() is expected


def test_default_runtime_keeps_original_daemon(monkeypatch):
    start = Mock()
    monkeypatch.setattr(supervisor, "_start_daemon", start)
    assert supervisor._start_runtime_worker(restart_reason="git_pull") is start.return_value
    start.assert_called_once_with(restart_reason="git_pull")


def test_email_only_launch_is_sender_scoped_and_safety_registered(monkeypatch, email_only):
    safety = SimpleNamespace(launch=Mock())
    monkeypatch.setattr(supervisor, "_safety", safety)
    monkeypatch.setenv("OPENOUTREACH_RESTART_REASON", "old-reason")
    assert supervisor._start_runtime_worker(restart_reason="sender_restart") is safety.launch.return_value
    args, kwargs = safety.launch.call_args
    assert args[0] == [supervisor.sys.executable, "manage.py", "run_email_enrichment", "--operator", "Chuka"]
    assert kwargs["cwd"] == supervisor.ROOT_DIR
    assert kwargs["env"]["OPENOUTREACH_SUPERVISED"] == "1"
    assert "OPENOUTREACH_RESTART_REASON" not in kwargs["env"]
    assert supervisor._runtime_worker_label() == "Email enrichment worker"
    email_only.assert_not_called()


def test_email_only_refuses_unknown_sender(monkeypatch, email_only):
    monkeypatch.setenv("LINKEDIN_USERNAME", "unknown@example.invalid")
    launch = Mock()
    monkeypatch.setattr(supervisor, "_launch", launch)
    with pytest.raises(ValueError, match="Gmail account mapping"):
        supervisor._start_runtime_worker()
    launch.assert_not_called()


def test_email_only_never_checks_feed_backlog_even_when_feed_flag_is_on(monkeypatch, email_only):
    missed = Mock(side_effect=AssertionError("Paused feeds must not inspect or enqueue jobs"))
    monkeypatch.setattr(supervisor, "_missed_feed_collection_due", missed)
    now = datetime(2026, 9, 17, 19, tzinfo=ZoneInfo("America/Toronto"))
    assert supervisor._feed_collection_should_start(now) is False
    assert supervisor._feed_collection_due_today(now) is False
    missed.assert_not_called()


@pytest.mark.parametrize("failed_worker", [None, "run_email_enrichment", "run_gmail_worker"])
def test_email_only_supervision_restarts_workers_without_linkedin(monkeypatch, email_only, failed_worker):
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
    monkeypatch.setattr(supervisor, "_missed_feed_collection_due", Mock(side_effect=AssertionError("No feed jobs")))

    def sleep(seconds):
        clock.now += seconds
        assert clock.now < 80, "Supervisor did not stop"

    def launch(args, **kwargs):
        kind = args[2]
        first_of_kind = not any(previous[2] == kind for previous in launches)
        launches.append(args)
        return SimpleNamespace(poll=lambda: 1 if first_of_kind and failed_worker == kind else None)

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
    assert {args[2] for args in launches} == {"run_email_enrichment", "run_gmail_worker"}
    assert sum(args[2] == "run_email_enrichment" for args in launches) >= 3
    assert sum(args[2] == "run_gmail_worker" for args in launches) >= 3
    for args in launches:
        assert args[3:] == (["--operator", "Chuka"] if args[2] == "run_email_enrichment" else ["--account", "eddy_boundera"])
    if failed_worker:
        assert len(notices) == 1
        assert "Daemon" not in notices[0]
    email_only.assert_not_called()


def test_direct_daemon_exits_before_startup_when_disabled(monkeypatch):
    import runpy

    monkeypatch.setattr("linkedin.conf.ENABLE_LINKEDIN_DAEMON", False)
    monkeypatch.setattr(supervisor.sys, "argv", ["manage.py"])
    setup = Mock(side_effect=AssertionError("No CRM bootstrap when LinkedIn is disabled"))
    update = Mock(side_effect=AssertionError("No daemon update/login path"))
    monkeypatch.setattr("linkedin.management.setup_crm.setup_crm", setup)
    monkeypatch.setattr("linkedin.version_check.check_for_updates", update)
    with pytest.raises(SystemExit) as exited:
        runpy.run_path(str(supervisor.ROOT_DIR / "manage.py"), run_name="__main__")
    assert exited.value.code == 0
    setup.assert_not_called()
    update.assert_not_called()
