"""Supervisor gating for the one-shot personal post child (no processes, no DB I/O)."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.db import ProgrammingError

import daemon_supervisor as supervisor
from tests.test_daemon_supervisor_reposts import harness, repost_hooks  # noqa: F401  (shared fixtures)


def _forbidden(name):
    return Mock(side_effect=AssertionError(f"{name} must not run while posts are off"))


@pytest.fixture
def post_hooks(monkeypatch):
    hooks = SimpleNamespace(queued=False, children=[], queue_checks=[], closes=[])

    def queued():
        hooks.queue_checks.append(supervisor.time.monotonic())
        return hooks.queued

    def start():
        child = SimpleNamespace(code=None, label=f"post-{len(hooks.children) + 1}")
        child.poll = lambda: child.code
        hooks.children.append(child)
        return child

    monkeypatch.setattr(supervisor, "_post_work_queued", queued)
    monkeypatch.setattr(supervisor, "_start_post_worker", start)
    monkeypatch.setattr(supervisor, "_close_owned_post_tabs", lambda: hooks.closes.append(supervisor.time.monotonic()))
    return hooks


@pytest.mark.parametrize("daemon,posts,expected", [
    (None, None, False), ("true", None, False), ("true", "false", False),
    ("true", "true", True), ("false", "true", False), (None, "1", True),
])
def test_posts_need_their_own_flag_and_the_linkedin_daemon(monkeypatch, daemon, posts, expected):
    for name, value in (("ENABLE_LINKEDIN_DAEMON", daemon), ("ENABLE_LINKEDIN_POSTS", posts)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    assert supervisor._linkedin_posts_enabled() is expected


def test_the_repost_flag_does_not_turn_on_posts(monkeypatch):
    monkeypatch.setenv("ENABLE_LINKEDIN_REPOSTS", "true")
    monkeypatch.delenv("ENABLE_LINKEDIN_POSTS", raising=False)
    assert supervisor._linkedin_posts_enabled() is False


def test_flag_off_supervisor_never_touches_posts(monkeypatch, harness):  # noqa: F811
    monkeypatch.delenv("ENABLE_LINKEDIN_POSTS", raising=False)
    monkeypatch.delenv("ENABLE_LINKEDIN_REPOSTS", raising=False)
    for name in ("_post_work_queued", "_start_post_worker", "_close_owned_post_tabs", "_stop_post_worker"):
        monkeypatch.setattr(supervisor, name, _forbidden(name))
    assert harness.run(700) == 0
    assert harness.events == [("start", "daemon", ""), ("poll", 300), ("poll", 600), ("stop", "daemon")]


@pytest.fixture
def posts_on(monkeypatch):
    monkeypatch.setenv("ENABLE_LINKEDIN_POSTS", "true")
    monkeypatch.delenv("ENABLE_LINKEDIN_REPOSTS", raising=False)


def test_posts_on_without_queued_rows_only_checks_every_two_minutes(posts_on, harness, post_hooks):  # noqa: F811
    harness.run(250, poll_seconds=10_000)
    assert post_hooks.queue_checks == [120, 240] and post_hooks.children == []
    assert ("stop", "LinkedIn post worker") not in harness.events


def test_posts_on_launches_one_child_only_when_a_row_is_due(posts_on, harness, post_hooks):  # noqa: F811
    post_hooks.queued = True

    def finish(now):
        if now == 200:
            post_hooks.children[0].code = 0
            post_hooks.queued = False

    harness.run(400, poll_seconds=10_000, on_tick=finish)
    assert len(post_hooks.children) == 1
    assert post_hooks.queue_checks == [120, 240, 360], "no check while the child ran"
    assert post_hooks.closes == [200]


def test_post_child_is_stopped_at_its_timeout(posts_on, harness, post_hooks, monkeypatch):  # noqa: F811
    monkeypatch.setattr(supervisor, "POST_TIMEOUT_SECONDS", 300)
    post_hooks.queued = True
    harness.run(700, poll_seconds=10_000)
    assert post_hooks.queue_checks == [120, 420]
    assert harness.events.count(("stop", "LinkedIn post worker")) == 2
    assert harness.events[-2:] == [("stop", "LinkedIn post worker"), ("stop", "daemon")]


def test_a_post_child_and_a_repost_child_never_run_together(monkeypatch, harness, post_hooks, repost_hooks):  # noqa: F811
    monkeypatch.setenv("ENABLE_LINKEDIN_POSTS", "true")
    monkeypatch.setenv("ENABLE_LINKEDIN_REPOSTS", "true")
    repost_hooks.queued = True
    post_hooks.queued = True

    def finish_repost(now):
        if now == 200 and repost_hooks.children:
            repost_hooks.children[0].code = 0
            repost_hooks.queued = False

    harness.run(400, poll_seconds=10_000, on_tick=finish_repost)
    # The repost child started first at 120; the post check at 120 waited for it.
    assert len(repost_hooks.children) == 1
    assert post_hooks.queue_checks and post_hooks.queue_checks[0] >= 200, "no post check while the repost child ran"
    assert len(post_hooks.children) == 1


def test_restart_and_daemon_exit_stop_the_post_child_first(posts_on, harness, post_hooks, monkeypatch):  # noqa: F811
    post_hooks.queued = True
    order = []
    monkeypatch.setattr(supervisor, "_stop_post_worker", lambda proc: order.append(("stop_post", proc.label)))
    monkeypatch.setattr(supervisor, "_stop_daemon", lambda proc: order.append(("stop_daemon",)))

    def restart_children(**kwargs):
        order.append(("restart_managed", kwargs["reason"]))
        return SimpleNamespace(label="daemon-2", poll=lambda: None), None

    monkeypatch.setattr(supervisor, "_restart_managed_children", restart_children)

    def controls(restart):
        if harness.clock.now >= 300:
            assert restart("git_pull") is True

    harness.run(301, poll_seconds=300, controls=controls)
    assert order[:2] == [("stop_post", "post-1"), ("restart_managed", "git_pull")]


def test_post_child_launch_is_supervised(monkeypatch):
    launch = Mock()
    monkeypatch.setattr(supervisor, "_launch", launch)
    supervisor._start_post_worker()
    args, kwargs = launch.call_args
    assert args[0] == [supervisor.sys.executable, "manage.py", "run_linkedin_post_once"]
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
    monkeypatch.setattr("linkedin.personal_posts.has_queued_post", check)
    assert supervisor._post_work_queued() is True
    check.assert_called_once_with("Chuka")
    assert database_check.settings_dict["OPTIONS"]["options"] == "-c statement_timeout=5000 -c lock_timeout=1000"


def test_missing_post_table_is_reported_not_crashing(monkeypatch, database_check, caplog):
    cause = Exception("relation does not exist")
    cause.sqlstate = "42P01"
    error = ProgrammingError("schema")
    error.__cause__ = cause
    monkeypatch.setattr("linkedin.personal_posts.has_queued_post", Mock(side_effect=error))
    assert supervisor._post_work_queued() is False
    assert "migration 0042" in caplog.text


def test_non_post_sender_never_opens_the_database(monkeypatch):
    monkeypatch.setenv("LINKEDIN_USERNAME", "leili.ash2011@yahoo.com")
    monkeypatch.setattr("django.setup", _forbidden("django.setup"))
    assert supervisor._post_work_queued() is False


def test_tab_cleanup_closes_only_recorded_post_targets(monkeypatch, tmp_path):
    from linkedin.personal_posts import PostTargetReceipt
    from linkedin.reposts import RepostTargetReceipt

    monkeypatch.setattr(supervisor, "ROOT_DIR", tmp_path)
    monkeypatch.setenv("LINKEDIN_USERNAME", "eddy@boundera.io")
    monkeypatch.setenv("LISTENER_CDP_PORT", "9555")
    closed = []
    monkeypatch.setattr("linkedin.feed_sampling.close_sample_target", lambda port, target: closed.append((port, target)) or True)
    PostTargetReceipt(tmp_path, "eddy@boundera.io", 9555).add("post-target")
    RepostTargetReceipt(tmp_path, "eddy@boundera.io", 9555).add("repost-target")
    supervisor._close_owned_post_tabs()
    assert closed == [(9555, "post-target")]
