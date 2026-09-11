from argparse import Namespace
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.db import InterfaceError, OperationalError, ProgrammingError

import daemon_supervisor as supervisor
from linkedin.exceptions import SupervisorStopped


def args(**overrides):
    return Namespace(**(dict(once=False, no_update=True, no_install=True, no_migrate=True,
        requirements="requirements/local.txt", poll_seconds=300, restart_delay=10) | overrides))


@pytest.fixture
def safety(monkeypatch):
    value = SimpleNamespace(
        stopped=Event(), emergency=False, preflight=Mock(return_value=True),
        start=Mock(), close=Mock(), raise_if_failed=Mock(),
        launch=Mock(), request_stop=Mock(),
    )
    value.request_stop.side_effect = lambda **kwargs: value.stopped.set()
    monkeypatch.setattr(supervisor, "SupervisorSafety", Mock(return_value=value))
    monkeypatch.setattr(supervisor, "_safety", None)
    return value


@pytest.mark.parametrize("latched,expected", [(True, 0), (False, 1)])
def test_startup_stop_or_unknown_control_state_never_runs_git_or_workers(monkeypatch, safety, latched, expected):
    safety.preflight.return_value = False
    safety.emergency = latched
    run = Mock()
    monkeypatch.setattr(supervisor, "_supervise_workers", run)
    assert supervisor.supervise(args()) == expected
    run.assert_not_called()
    safety.start.assert_not_called()
    safety.close.assert_called_once()
    assert supervisor._safety is None


def test_valid_preflight_installs_watchdog_before_worker_loop(monkeypatch, safety):
    def run(options, control):
        safety.start.assert_called_once()
        assert supervisor._safety is control is safety
        return 0
    monkeypatch.setattr(supervisor, "_supervise_workers", run)
    assert supervisor.supervise(args()) == 0
    safety.request_stop.assert_called_once_with(emergency=False)


def test_cancelled_launch_exits_without_restarting_and_propagates_safety_errors(monkeypatch, safety):
    monkeypatch.setattr(supervisor, "_supervise_workers", Mock(side_effect=SupervisorStopped()))
    assert supervisor.supervise(args()) == 0
    safety.raise_if_failed.assert_called_once()
    safety.raise_if_failed.side_effect = ValueError("unexpected checker failure")
    with pytest.raises(ValueError):
        supervisor.supervise(args())
    assert supervisor._safety is None


def test_stop_during_initial_git_check_prevents_all_worker_launches(monkeypatch, safety):
    monkeypatch.setattr(supervisor.signal, "signal", Mock())
    monkeypatch.setattr(supervisor, "_gmail_account_for_supervisor", lambda: "eddy_boundera")
    def pull(options):
        safety.stopped.set()
        return True
    monkeypatch.setattr(supervisor, "_maybe_pull_update", pull)
    start = Mock(side_effect=AssertionError("must not launch after stop"))
    monkeypatch.setattr(supervisor, "_start_daemon", start)
    monkeypatch.setattr(supervisor, "_start_gmail_worker", start)
    assert supervisor._supervise_workers(args(), safety) == 0
    start.assert_not_called()


@pytest.mark.parametrize("launcher", ["daemon", "gmail", "feed", "git"])
def test_every_supervisor_subprocess_is_registered_with_safety(monkeypatch, safety, launcher):
    monkeypatch.setattr(supervisor, "_safety", safety)
    monkeypatch.setattr(supervisor, "_resolve_executable", lambda x: x)
    proc = SimpleNamespace(returncode=0, communicate=lambda: ("ok", ""))
    safety.launch.return_value = proc
    if launcher == "daemon":
        supervisor._start_daemon()
    elif launcher == "gmail":
        supervisor._start_gmail_worker("eddy_boundera")
    elif launcher == "feed":
        supervisor._start_feed_collector()
    else:
        assert supervisor._run(["git", "fetch"]).stdout == "ok"
    safety.launch.assert_called_once()


def test_git_interrupted_by_stop_is_cancellation_not_update_failure(monkeypatch, safety):
    monkeypatch.setattr(supervisor, "_safety", safety)
    monkeypatch.setattr(supervisor, "_resolve_executable", lambda x: x)
    def communicate():
        safety.stopped.set()
        return "", ""
    safety.launch.return_value = SimpleNamespace(returncode=-15, communicate=communicate)
    with pytest.raises(SupervisorStopped):
        supervisor._run(["git", "fetch"])


@pytest.mark.parametrize("acknowledged", [True, False])
def test_restart_notice_only_after_database_acknowledgement(monkeypatch, acknowledged):
    monkeypatch.setattr(supervisor, "_maybe_pull_update", lambda options: False)
    def consume(restart):
        assert restart() is True
        return acknowledged
    monkeypatch.setattr(supervisor, "_maybe_consume_sender_restart", consume)
    notice = Mock()
    monkeypatch.setattr(supervisor, "_notify_restart_trigger", notice)
    assert supervisor._poll_runtime_controls(args(), Mock(return_value=True)) is True
    assert notice.call_count == int(acknowledged)


def test_latched_stop_takes_priority_over_git_and_restart(monkeypatch, safety):
    monkeypatch.setattr(supervisor, "_safety", safety)
    safety.preflight.return_value = False
    pull = Mock()
    consume = Mock()
    restart = Mock()
    monkeypatch.setattr(supervisor, "_maybe_pull_update", pull)
    monkeypatch.setattr(supervisor, "_maybe_consume_sender_restart", consume)
    assert supervisor._poll_runtime_controls(args(), restart) is False
    pull.assert_not_called()
    consume.assert_not_called()
    restart.assert_not_called()


def test_stop_after_git_update_cannot_fall_back_to_restart(monkeypatch, safety):
    monkeypatch.setattr(supervisor, "_safety", safety)
    safety.preflight.side_effect = [True, False]
    monkeypatch.setattr(supervisor, "_maybe_pull_update", lambda options: True)
    monkeypatch.setattr(supervisor, "_maybe_consume_sender_restart", lambda restart: False)
    restart = Mock()
    assert supervisor._poll_runtime_controls(args(), restart) is False
    restart.assert_not_called()


def test_restart_notice_uses_operator_mapping_and_launch_only_wording(monkeypatch):
    monkeypatch.setenv("LINKEDIN_USERNAME", "chukyjack@gmail.com")
    notice = Mock()
    monkeypatch.setattr("linkedin.notifications.slack.notify_supervisor_restart", notice)
    supervisor._notify_restart_trigger()
    assert notice.call_args.kwargs["sender"] == "Chuka"
    assert "processes launched" in notice.call_args.kwargs["detail"]


def test_stop_notice_distinguishes_complete_stop_from_shutdown_failure(monkeypatch):
    monkeypatch.setenv("LINKEDIN_USERNAME", "ariantajbakh@gmail.com")
    notice = Mock()
    monkeypatch.setattr("linkedin.notifications.slack.notify_supervisor_stop", notice)
    supervisor._notify_emergency_stop("sender_stop")
    assert notice.call_args.kwargs["sender"] == "Arian"
    assert "have been stopped" in notice.call_args.kwargs["detail"]
    supervisor._notify_emergency_stop("shutdown_failed:AccessDenied")
    assert "confirm all owned processes stopped" in notice.call_args.kwargs["detail"]
    assert "have been stopped" not in notice.call_args.kwargs["detail"]


@pytest.fixture
def database(monkeypatch):
    value = SimpleNamespace(settings_dict={"OPTIONS": {}}, close=Mock())
    monkeypatch.setattr("django.setup", lambda: None)
    monkeypatch.setattr("django.db.connection", value)
    monkeypatch.setenv("LINKEDIN_USERNAME", "exact@example.invalid")
    return value


def test_stop_read_exact_identity_timeouts_and_connection_cleanup(monkeypatch, database):
    read = Mock(return_value=True)
    monkeypatch.setattr("linkedin.supervisor_control.read_sender_stop", read)
    assert supervisor._read_sender_stop_state() is True
    read.assert_called_once_with(linkedin_username="exact@example.invalid")
    assert database.settings_dict["OPTIONS"]["connect_timeout"] == 5
    assert database.close.call_count == 2


@pytest.mark.parametrize("error", [OperationalError, InterfaceError])
def test_stop_read_outage_is_unknown_and_logs_no_secrets(monkeypatch, database, caplog, error):
    monkeypatch.setattr("linkedin.supervisor_control.read_sender_stop", Mock(side_effect=error("secret-value")))
    assert supervisor._read_sender_stop_state() is None
    assert "secret-value" not in caplog.text


@pytest.mark.parametrize("column,handled", [("stop_requested", True), ("other_field", False)])
def test_stop_read_only_handles_exact_missing_migration(monkeypatch, database, column, handled):
    cause = Exception(f'column "{column}" does not exist')
    cause.sqlstate = "42703"
    error = ProgrammingError("schema error")
    error.__cause__ = cause
    monkeypatch.setattr("linkedin.supervisor_control.read_sender_stop", Mock(side_effect=error))
    if handled:
        assert supervisor._read_sender_stop_state() is None
    else:
        with pytest.raises(ProgrammingError):
            supervisor._read_sender_stop_state()
