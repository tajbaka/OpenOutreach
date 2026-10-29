"""Supervisor gating for the one-shot repost child (no processes, no DB I/O)."""
from argparse import Namespace
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.db import OperationalError, ProgrammingError

import daemon_supervisor as supervisor


def _forbidden(name):
    return Mock(side_effect=AssertionError(f"{name} must not run while reposts are off"))


@pytest.fixture
def harness(monkeypatch):
    clock = SimpleNamespace(now=0)
    events = []
    stopped = Event()
    safety = SimpleNamespace(stopped=stopped, raise_if_failed=lambda: None, request_stop=lambda **kw: stopped.set())
    daemon = SimpleNamespace(label="daemon", code=None)
    daemon.poll = lambda: daemon.code
    state = SimpleNamespace(until=None, controls=None, on_tick=None)

    monkeypatch.setattr(supervisor, "_safety", None)
    monkeypatch.setattr(supervisor.signal, "signal", lambda *args: None)
    monkeypatch.setattr(supervisor.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(supervisor, "_maybe_pull_update", lambda args: False)
    monkeypatch.setattr(supervisor, "_gmail_account_for_supervisor", lambda: None)
    monkeypatch.setattr(supervisor, "_notify", lambda title, detail: events.append(("notify", title)))

    def start_daemon(**kwargs):
        events.append(("start", "daemon", kwargs.get("restart_reason")))
        daemon.code = None
        return daemon

    monkeypatch.setattr(supervisor, "_start_daemon", start_daemon)
    monkeypatch.setattr(supervisor, "_stop_daemon", lambda proc: events.append(("stop", "daemon")))
    monkeypatch.setattr(supervisor, "_stop_process", lambda proc, **kw: events.append(("stop", kw["label"])))

    def controls(args, restart):
        events.append(("poll", clock.now))
        if state.controls is not None:
            state.controls(restart)

    monkeypatch.setattr(supervisor, "_poll_runtime_controls", controls)

    def sleep(seconds):
        clock.now += seconds
        if state.on_tick is not None:
            state.on_tick(clock.now)
        if clock.now >= state.until:
            stopped.set()

    monkeypatch.setattr(supervisor.time, "sleep", sleep)

    def run(until, *, poll_seconds=300, controls=None, on_tick=None):
        state.until, state.controls, state.on_tick = until, controls, on_tick
        return supervisor._supervise_workers(Namespace(poll_seconds=poll_seconds, restart_delay=10), safety)

    return SimpleNamespace(clock=clock, events=events, daemon=daemon, run=run)


@pytest.fixture
def repost_hooks(monkeypatch):
    hooks = SimpleNamespace(queued=False, children=[], queue_checks=[], closes=[])

    def queued():
        hooks.queue_checks.append(supervisor.time.monotonic())
        return hooks.queued

    def start():
        child = SimpleNamespace(code=None, label=f"repost-{len(hooks.children) + 1}")
        child.poll = lambda: child.code
        hooks.children.append(child)
        return child

    monkeypatch.setattr(supervisor, "_repost_work_queued", queued)
    monkeypatch.setattr(supervisor, "_start_repost_worker", start)
    monkeypatch.setattr(supervisor, "_close_owned_repost_tabs", lambda: hooks.closes.append(supervisor.time.monotonic()))
    return hooks


# --------------------------------------------------------------------------- flag


@pytest.mark.parametrize("daemon,reposts,expected", [
    (None, None, False), ("true", None, False), ("true", "false", False),
    ("true", "true", True), ("false", "true", False), (None, "1", True),
])
def test_reposts_need_the_flag_and_the_linkedin_daemon(monkeypatch, daemon, reposts, expected):
    for name, value in (("ENABLE_LINKEDIN_DAEMON", daemon), ("ENABLE_LINKEDIN_REPOSTS", reposts)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    assert supervisor._linkedin_reposts_enabled() is expected


def test_flag_off_supervisor_never_touches_reposts_and_keeps_its_sequence(monkeypatch, harness):
    monkeypatch.delenv("ENABLE_LINKEDIN_REPOSTS", raising=False)
    for name in ("_repost_work_queued", "_start_repost_worker", "_close_owned_repost_tabs", "_stop_repost_worker"):
        monkeypatch.setattr(supervisor, name, _forbidden(name))

    def controls(restart):
        if harness.clock.now == 300:
            assert restart("sender_restart") is True

    restarts = []

    def restart_children(**kwargs):
        restarts.append(kwargs["reason"])
        return SimpleNamespace(label="daemon-2", poll=lambda: None), None

    monkeypatch.setattr(supervisor, "_restart_managed_children", restart_children)

    def crash_daemon(now):
        if now == 100:
            harness.daemon.code = 1

    assert harness.run(700, controls=controls, on_tick=crash_daemon) == 0
    # The exact pre-feature sequence: start, crash restart, poll+sender restart, poll, shutdown.
    assert harness.events == [
        ("start", "daemon", ""),
        ("notify", "Daemon child exited unexpectedly"),
        ("start", "daemon", "process_exit"),
        ("poll", 300),
        ("poll", 600),
        ("stop", "daemon"),
    ]
    assert restarts == ["sender_restart"]


# --------------------------------------------------------------------------- flag on


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("ENABLE_LINKEDIN_REPOSTS", "true")


def test_flag_on_without_queued_rows_only_checks_every_two_minutes(enabled, harness, repost_hooks):
    harness.run(500, poll_seconds=10_000)
    assert repost_hooks.queue_checks == [120, 240, 360, 480]
    assert repost_hooks.children == []
    assert ("stop", "LinkedIn repost worker") not in harness.events


def test_flag_on_launches_one_child_only_when_a_row_is_queued(enabled, harness, repost_hooks):
    repost_hooks.queued = True

    def finish_first(now):
        if now == 200:
            repost_hooks.children[0].code = 0
            repost_hooks.queued = False

    harness.run(400, poll_seconds=10_000, on_tick=finish_first)
    assert len(repost_hooks.children) == 1
    # Launched at 120; no check while it ran (the 240 check came after its exit at 200).
    assert repost_hooks.queue_checks == [120, 240, 360]
    assert repost_hooks.closes == [200]
    assert ("stop", "LinkedIn repost worker") not in harness.events


def test_child_still_running_at_a_check_is_not_doubled(enabled, harness, repost_hooks):
    repost_hooks.queued = True
    harness.run(400, poll_seconds=10_000)
    assert len(repost_hooks.children) == 1
    assert repost_hooks.queue_checks == [120]


def test_running_child_is_never_doubled_and_is_stopped_at_its_timeout(enabled, harness, repost_hooks, monkeypatch):
    monkeypatch.setattr(supervisor, "REPOST_TIMEOUT_SECONDS", 300)
    repost_hooks.queued = True
    harness.run(700, poll_seconds=10_000)
    # Launched at 120, stopped (and its tabs closed) at 120 + 300; only then
    # could the next check launch another child for a still-queued row.
    assert repost_hooks.queue_checks == [120, 420]
    assert repost_hooks.closes[:2] == [420, 420]
    assert len(repost_hooks.children) == 2
    # One stop at the timeout, one for the second child at shutdown.
    assert harness.events.count(("stop", "LinkedIn repost worker")) == 2
    assert harness.events[-2:] == [("stop", "LinkedIn repost worker"), ("stop", "daemon")]


def test_restart_stops_repost_child_as_a_separate_first_step(enabled, harness, repost_hooks, monkeypatch):
    repost_hooks.queued = True
    order = []
    monkeypatch.setattr(supervisor, "_stop_repost_worker", lambda proc: order.append(("stop_repost", proc.label)))

    def restart_children(**kwargs):
        order.append(("restart_managed", kwargs["reason"]))
        return SimpleNamespace(label="daemon-2", poll=lambda: None), None

    monkeypatch.setattr(supervisor, "_restart_managed_children", restart_children)

    def controls(restart):
        if harness.clock.now >= 300:
            assert restart("git_pull") is True

    harness.run(301, poll_seconds=300, controls=controls)
    assert order[:2] == [("stop_repost", "repost-1"), ("restart_managed", "git_pull")]


def test_daemon_exit_and_shutdown_stop_the_repost_child_first(enabled, harness, repost_hooks, monkeypatch):
    repost_hooks.queued = True
    order = []
    monkeypatch.setattr(supervisor, "_stop_repost_worker", lambda proc: order.append(("stop_repost", proc.label)))
    monkeypatch.setattr(supervisor, "_stop_daemon", lambda proc: order.append(("stop_daemon",)))

    def crash(now):
        if now == 150:
            harness.daemon.code = 1

    harness.run(400, poll_seconds=10_000, on_tick=crash)
    starts = [event for event in harness.events if event[0] == "start"]
    assert starts == [("start", "daemon", ""), ("start", "daemon", "process_exit")]
    # Crash: the repost child stops before the daemon restarts; shutdown: before the daemon stops.
    assert order[0] == ("stop_repost", "repost-1")
    assert order[-2:] == [("stop_repost", "repost-2"), ("stop_daemon",)]


# --------------------------------------------------------------------------- helpers


def test_repost_child_launch_is_supervised_and_sender_scoped(monkeypatch):
    launch = Mock()
    monkeypatch.setattr(supervisor, "_launch", launch)
    supervisor._start_repost_worker()
    args, kwargs = launch.call_args
    assert args[0] == [supervisor.sys.executable, "manage.py", "run_linkedin_repost_once"]
    assert kwargs["cwd"] == supervisor.ROOT_DIR
    assert kwargs["env"]["OPENOUTREACH_SUPERVISED"] == "1"


@pytest.fixture
def database_check(monkeypatch):
    connection = SimpleNamespace(settings_dict={"OPTIONS": {}}, close=Mock())
    monkeypatch.setenv("LINKEDIN_USERNAME", "eddy@boundera.io")
    monkeypatch.setattr("django.setup", lambda: None)
    monkeypatch.setattr("django.db.connection", connection)
    return connection


def test_queue_check_is_exact_operator_and_bounded(monkeypatch, database_check):
    check = Mock(return_value=True)
    monkeypatch.setattr("linkedin.reposts.has_queued_repost", check)
    assert supervisor._repost_work_queued() is True
    check.assert_called_once_with("Chuka")
    options = database_check.settings_dict["OPTIONS"]
    assert options["options"] == "-c statement_timeout=5000 -c lock_timeout=1000"
    assert options["connect_timeout"] == 5
    assert database_check.close.call_count == 2


def test_queue_check_outage_waits_without_details(monkeypatch, database_check, caplog):
    monkeypatch.setattr("linkedin.reposts.has_queued_repost", Mock(side_effect=OperationalError("private-dsn")))
    assert supervisor._repost_work_queued() is False
    assert "private-dsn" not in caplog.text


@pytest.mark.parametrize("sqlstate", ["42P01", "42703"])
def test_missing_repost_table_or_column_is_reported_not_crashing(monkeypatch, database_check, caplog, sqlstate):
    cause = Exception("relation or column does not exist")
    cause.sqlstate = sqlstate
    error = ProgrammingError("schema")
    error.__cause__ = cause
    monkeypatch.setattr("linkedin.reposts.has_queued_repost", Mock(side_effect=error))
    assert supervisor._repost_work_queued() is False
    assert "migrations 0035-0036" in caplog.text


def test_non_repost_sender_never_opens_the_database(monkeypatch):
    monkeypatch.setenv("LINKEDIN_USERNAME", "leili.ash2011@yahoo.com")
    monkeypatch.setattr("django.setup", _forbidden("django.setup"))
    assert supervisor._repost_work_queued() is False


def test_tab_cleanup_closes_only_recorded_repost_targets(monkeypatch, tmp_path):
    from linkedin.reposts import RepostTargetReceipt

    monkeypatch.setattr(supervisor, "ROOT_DIR", tmp_path)
    monkeypatch.setenv("LINKEDIN_USERNAME", "eddy@boundera.io")
    monkeypatch.setenv("LISTENER_CDP_PORT", "9555")
    closed = []
    monkeypatch.setattr("linkedin.feed_sampling.close_sample_target", lambda port, target: closed.append((port, target)) or True)
    RepostTargetReceipt(tmp_path, "eddy@boundera.io", 9555).add("repost-target")
    supervisor._close_owned_repost_tabs()
    assert closed == [(9555, "repost-target")]
    supervisor._close_owned_repost_tabs()
    assert closed == [(9555, "repost-target")]
