from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.db import InterfaceError, OperationalError, ProgrammingError, connection, connections, transaction

import daemon_supervisor as supervisor


def _args(**overrides):
    values = dict(once=False, no_update=True, no_install=True, no_migrate=True,
                  requirements="requirements/local.txt", poll_seconds=300, restart_delay=10)
    return Namespace(**(values | overrides))


@pytest.mark.parametrize("updated,requested", [(False, False), (True, False), (False, True), (True, True)])
def test_one_poll_coalesces_code_and_sender_restart(monkeypatch, updated, requested):
    monkeypatch.setattr(supervisor, "_maybe_pull_update", lambda args: updated)
    monkeypatch.setattr(supervisor, "_maybe_consume_sender_restart", lambda restart: restart() if requested else False)
    restart = Mock(return_value=True)

    assert supervisor._poll_runtime_controls(_args(), restart) is (updated or requested)

    if updated or requested:
        restart.assert_called_once_with("git_pull" if updated else "sender_restart")
    else:
        restart.assert_not_called()


def test_no_update_still_processes_database_request(monkeypatch):
    pull = Mock(side_effect=AssertionError("Git must remain disabled"))
    monkeypatch.setattr(supervisor, "_pull_update", pull)
    monkeypatch.setattr(supervisor, "_maybe_consume_sender_restart", lambda restart: restart())
    restart = Mock(return_value=True)
    assert supervisor._poll_runtime_controls(_args(no_update=True), restart) is True
    restart.assert_called_once_with("sender_restart")
    pull.assert_not_called()


def test_failed_ack_after_start_does_not_repeat_restart_in_same_poll(monkeypatch):
    monkeypatch.setattr(supervisor, "_maybe_pull_update", lambda args: True)

    def unavailable_ack(restart):
        assert restart() is True
        return False

    monkeypatch.setattr(supervisor, "_maybe_consume_sender_restart", unavailable_ack)
    restart = Mock(return_value=True)
    assert supervisor._poll_runtime_controls(_args(), restart) is True
    restart.assert_called_once_with("git_pull")


def test_cancelled_requested_restart_is_not_repeated_for_simultaneous_code_pull(monkeypatch):
    monkeypatch.setattr(supervisor, "_maybe_pull_update", lambda args: True)
    monkeypatch.setattr(supervisor, "_maybe_consume_sender_restart", lambda restart: restart())
    restart = Mock(return_value=False)
    assert supervisor._poll_runtime_controls(_args(), restart) is False
    restart.assert_called_once_with("git_pull")


def test_once_never_consumes_restart_request(monkeypatch):
    monkeypatch.setattr(supervisor, "_maybe_pull_update", lambda args: False)
    consume = Mock(side_effect=AssertionError("--once starts no workers"))
    monkeypatch.setattr(supervisor, "_maybe_consume_sender_restart", consume)
    assert supervisor.supervise(_args(once=True)) == 1
    consume.assert_not_called()


@pytest.fixture
def database_check(monkeypatch):
    connection = SimpleNamespace(settings_dict={"OPTIONS": {}}, close=Mock())
    monkeypatch.setenv("LINKEDIN_USERNAME", "local@example.invalid")
    monkeypatch.setattr("django.setup", lambda: None)
    monkeypatch.setattr("django.db.connection", connection)
    return connection


def test_restart_check_passes_exact_local_identity_and_bounded_connection(monkeypatch, database_check):
    consume = Mock(return_value=True)
    monkeypatch.setattr("linkedin.supervisor_control.consume_sender_restart", consume)
    restart = Mock()
    assert supervisor._maybe_consume_sender_restart(restart) is True
    consume.assert_called_once_with(linkedin_username="local@example.invalid", restart=restart)
    assert database_check.settings_dict["OPTIONS"]["connect_timeout"] == 5
    assert database_check.settings_dict["OPTIONS"]["options"] == "-c statement_timeout=5000 -c lock_timeout=1000"
    assert database_check.settings_dict["OPTIONS"]["tcp_user_timeout"] == 15000
    assert database_check.close.call_count == 2


def test_control_timeouts_preserve_other_options_without_accumulating(monkeypatch, database_check):
    database_check.settings_dict["OPTIONS"].update(options="-c application_name=qa-control", connect_timeout=0)
    monkeypatch.setattr("linkedin.supervisor_control.consume_sender_restart", Mock(return_value=False))
    for _ in range(2):
        assert supervisor._maybe_consume_sender_restart(Mock()) is False
    options = database_check.settings_dict["OPTIONS"]
    assert options["options"] == "-c application_name=qa-control -c statement_timeout=5000 -c lock_timeout=1000"
    assert options["connect_timeout"] == 5


@pytest.mark.parametrize("error_type", [OperationalError, InterfaceError])
def test_database_outage_does_not_restart_or_expose_connection_details(monkeypatch, database_check, caplog, error_type):
    monkeypatch.setattr("linkedin.supervisor_control.consume_sender_restart", Mock(side_effect=error_type("private-connection-value")))
    restart = Mock()
    assert supervisor._maybe_consume_sender_restart(restart) is False
    restart.assert_not_called()
    assert "request stays pending" in caplog.text
    assert "private-connection-value" not in caplog.text


@pytest.mark.parametrize("column,handled", [("restart_requested", True), ("another_column", False)])
def test_only_exact_missing_control_column_is_expected_deploy_gap(monkeypatch, database_check, column, handled):
    cause = Exception(f'column "{column}" does not exist')
    cause.sqlstate = "42703"
    error = ProgrammingError("schema error")
    error.__cause__ = cause
    monkeypatch.setattr("linkedin.supervisor_control.consume_sender_restart", Mock(side_effect=error))
    restart = Mock()
    if handled:
        assert supervisor._maybe_consume_sender_restart(restart) is False
    else:
        with pytest.raises(ProgrammingError):
            supervisor._maybe_consume_sender_restart(restart)
    restart.assert_not_called()


def test_missing_local_identity_never_looks_for_first_active_sender(monkeypatch):
    monkeypatch.delenv("LINKEDIN_USERNAME", raising=False)
    setup = Mock(side_effect=AssertionError("No DB setup without identity"))
    monkeypatch.setattr("django.setup", setup)
    assert supervisor._maybe_consume_sender_restart(Mock()) is False
    setup.assert_not_called()


@pytest.fixture
def processes(monkeypatch):
    events = []

    def proc(label, code=None):
        return SimpleNamespace(label=label, poll=lambda: code)

    old = proc("old-linkedin")
    old_gmail = proc("old-gmail")
    feed = proc("old-feed")
    new = proc("new-linkedin")
    new_gmail = proc("new-gmail")
    monkeypatch.setattr(supervisor, "_stop_daemon", lambda p: events.append(("stop", p.label)))
    monkeypatch.setattr(supervisor, "_stop_process", lambda p, **kw: events.append(("stop", p.label)))

    def start_linkedin(**kw):
        events.append(("start", "linkedin", kw["restart_reason"]))
        return new

    def start_gmail(account):
        events.append(("start", "gmail", account))
        return new_gmail

    monkeypatch.setattr(supervisor, "_start_daemon", start_linkedin)
    monkeypatch.setattr(supervisor, "_start_gmail_worker", start_gmail)
    return SimpleNamespace(events=events, old=old, gmail=old_gmail, feed=feed, new=new, new_gmail=new_gmail,
        kwargs=dict(child=old, gmail_child=old_gmail, feed_child=feed, gmail_account="arian_boundera",
                    reason="sender_restart", should_stop=lambda: False))


def test_restart_replaces_only_owned_workers_and_stops_feed(processes):
    assert supervisor._restart_managed_children(**processes.kwargs) == (processes.new, processes.new_gmail)
    assert processes.events == [
        ("stop", "old-gmail"), ("stop", "old-feed"), ("stop", "old-linkedin"),
        ("start", "linkedin", "sender_restart"), ("start", "gmail", "arian_boundera"),
    ]


def test_restart_supports_linkedin_only_sender(processes):
    processes.kwargs.update(gmail_child=None, feed_child=None, gmail_account=None)
    assert supervisor._restart_managed_children(**processes.kwargs) == (processes.new, None)
    assert processes.events == [("stop", "old-linkedin"), ("start", "linkedin", "sender_restart")]


def test_gmail_launch_failure_cleans_up_new_linkedin_and_propagates(monkeypatch, processes):
    monkeypatch.setattr(supervisor, "_start_gmail_worker", Mock(side_effect=OSError("launch failed")))
    with pytest.raises(OSError):
        supervisor._restart_managed_children(**processes.kwargs)
    assert processes.events[-1] == ("stop", "new-linkedin")


@pytest.mark.parametrize("stop_checks", [[True], [False, True], [False, False, True], [False, False, False, True]])
def test_shutdown_cancellation_never_confirms_restart(processes, stop_checks):
    checks = iter(stop_checks)
    processes.kwargs["should_stop"] = lambda: next(checks)
    assert supervisor._restart_managed_children(**processes.kwargs) is None
    if ("start", "linkedin", "sender_restart") in processes.events:
        assert ("stop", "new-linkedin") in processes.events
    if ("start", "gmail", "arian_boundera") in processes.events:
        assert ("stop", "new-gmail") in processes.events


def test_immediately_failed_replacement_is_not_acknowledged(monkeypatch, processes):
    processes.new.poll = lambda: 1
    assert supervisor._restart_managed_children(**processes.kwargs) is None
    assert processes.events[-2:] == [("stop", "new-gmail"), ("stop", "new-linkedin")]


@pytest.mark.parametrize("source", ["new", "new_gmail"])
def test_signal_during_replacement_poll_does_not_acknowledge(processes, source):
    cancelled = SimpleNamespace(value=False)

    def interrupted_poll():
        cancelled.value = True
        return None

    getattr(processes, source).poll = interrupted_poll
    processes.kwargs["should_stop"] = lambda: cancelled.value
    assert supervisor._restart_managed_children(**processes.kwargs) is None
    assert processes.events[-2:] == [("stop", "new-gmail"), ("stop", "new-linkedin")]


@pytest.mark.django_db(transaction=True)
def test_table_lock_cannot_stall_restart_control_poll(monkeypatch, caplog):
    from linkedin.models import LinkedInProfile
    from tests.factories import UserFactory

    profile = LinkedInProfile.objects.create(user=UserFactory(),
        linkedin_username="qa-control@example.invalid", linkedin_password="unused", restart_requested=True)
    monkeypatch.setenv("LINKEDIN_USERNAME", profile.linkedin_username)
    monkeypatch.setitem(connection.settings_dict, "OPTIONS", dict(connection.settings_dict.get("OPTIONS", {})))
    entered, release = Event(), Event()

    def hold_table():
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("LOCK TABLE linkedin_linkedinprofile IN ACCESS EXCLUSIVE MODE")
                entered.set()
                assert release.wait(timeout=10), "Test did not release schema-lock fixture"
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as executor:
        holder = executor.submit(hold_table)
        try:
            assert entered.wait(timeout=5)
            callback = Mock(return_value=True)
            started = monotonic()
            assert supervisor._maybe_consume_sender_restart(callback) is False
            assert monotonic() - started < 4
            callback.assert_not_called()
            assert "request stays pending" in caplog.text
        finally:
            release.set()
        holder.result(timeout=5)
    profile.refresh_from_db()
    assert profile.restart_requested is True


def test_child_crashes_do_not_reset_or_starve_control_timer(monkeypatch):
    clock = SimpleNamespace(now=0)
    signals = {}
    starts = []
    polls = []
    monkeypatch.setattr(supervisor.signal, "signal", lambda key, handler: signals.setdefault(key, handler))
    monkeypatch.setattr(supervisor.time, "monotonic", lambda: clock.now)

    def sleep(seconds):
        clock.now += seconds
        assert clock.now <= 40, "Child crashes starved the restart-control poll"

    monkeypatch.setattr(supervisor.time, "sleep", sleep)
    monkeypatch.setattr(supervisor, "_notify", lambda *args: None)
    monkeypatch.setattr(supervisor, "_maybe_pull_update", lambda args: False)
    monkeypatch.setattr(supervisor, "_gmail_account_for_supervisor", lambda: None)
    monkeypatch.setattr(supervisor, "_stop_daemon", lambda proc: None)

    def start(**kwargs):
        starts.append(kwargs)
        return SimpleNamespace(poll=lambda: 1)

    def poll(args, restart):
        polls.append(clock.now)
        signals[supervisor.signal.SIGTERM](supervisor.signal.SIGTERM, None)
        return False

    monkeypatch.setattr(supervisor, "_start_daemon", start)
    monkeypatch.setattr(supervisor, "_poll_runtime_controls", poll)
    assert supervisor.supervise(_args(poll_seconds=30)) == 0
    assert polls == [30]
    assert len(starts) == 4
