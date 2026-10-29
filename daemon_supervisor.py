#!/usr/bin/env python
"""Canonical process supervisor for the OpenOutreach runtime.

Runs the browser-backed LinkedIn daemon and the mapped browserless Gmail worker
as independent child processes, optionally polls the current Git upstream for
new commits, and restarts the children after applying updates. This is
also where a sender's database restart request is consumed, independently of
Git updates. ENABLE_LINKEDIN_DAEMON=false replaces LinkedIn with browserless
email enrichment. Run it in a terminal on macOS, Windows, or Linux.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from shutil import which

from linkedin.exceptions import SupervisorStopped
from supervisor_safety import SupervisorSafety
from linkedin.feed_sampling import (
    FeedSampleSchedule, MAX_SECONDS, PROCESS_GRACE_SECONDS, close_sample_target,
)
from linkedin.reposts import (
    REPOST_OPERATORS, SUPERVISOR_TIMEOUT_SECONDS as REPOST_TIMEOUT_SECONDS, RepostTargetReceipt,
)


ROOT_DIR = Path(__file__).resolve().parent
DEFAULT_POLL_SECONDS = 300
FEED_COLLECTOR_CHECK_SECONDS = 60
REPOST_CHECK_SECONDS = 120
REQUIREMENT_PATHS = (
    "requirements/base.txt",
    "requirements/local.txt",
    "requirements/production.txt",
    "requirements/crm.txt",
)

logger = logging.getLogger("daemon_supervisor")
_safety: SupervisorSafety | None = None


def _launch(args, **kwargs):
    """Register every supervisor child, including Git, behind the stop gate."""
    if _safety is not None:
        return _safety.launch(args, **kwargs)
    return subprocess.Popen(args, **kwargs)


def _load_env() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT_DIR / ".env")
    except Exception as exc:
        logger.debug("Could not load .env: %s", exc)


def _resolve_executable(name: str) -> str:
    found = which(name)
    if found:
        return found

    if name == "git":
        for candidate in (
            Path(os.environ.get("ProgramFiles", "")) / "Git" / "cmd" / "git.exe",
            Path(os.environ.get("ProgramFiles(x86)", "")) / "Git" / "cmd" / "git.exe",
        ):
            if candidate.exists():
                return str(candidate)

    return name


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


def _run(args: list[str], *, check: bool = False) -> CommandResult:
    args = [_resolve_executable(args[0]), *args[1:]]
    logger.debug("$ %s", " ".join(args))
    proc = _launch(
        args,
        cwd=ROOT_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stdout, stderr = proc.communicate()
    if _safety is not None and _safety.stopped.is_set():
        raise SupervisorStopped("Supervisor stop interrupted its command")
    result = CommandResult(proc.returncode, stdout or "", stderr or "")
    if check and result.returncode != 0:
        raise RuntimeError(
            f"{' '.join(args)} failed with exit {result.returncode}\n"
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return result


def _notify(title: str, detail: str) -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT_DIR / ".env")
        from linkedin.notifications.slack import notify_degraded
        from linkedin.operators import resolve_operator

        handle = os.getenv("LINKEDIN_USERNAME", "").strip()
        sender = resolve_operator(handle) or "supervisor"
        notify_degraded(sender=sender, title=title, detail=detail[:2800])
    except Exception as exc:
        logger.debug("Slack supervisor notification failed: %s", exc)


def _notify_restart_trigger() -> None:
    from linkedin.notifications.slack import notify_supervisor_restart
    from linkedin.operators import resolve_operator

    sender = resolve_operator(os.getenv("LINKEDIN_USERNAME", "").strip()) or "supervisor"
    notify_supervisor_restart(
        sender=sender,
        detail="The database restart trigger was acknowledged after replacement worker processes launched. The restart flag is now cleared.",
    )


def _notify_emergency_stop(reason: str) -> None:
    from linkedin.notifications.slack import notify_supervisor_stop
    from linkedin.operators import resolve_operator

    sender = resolve_operator(os.getenv("LINKEDIN_USERNAME", "").strip()) or "supervisor"
    if reason == "sender_stop":
        detail = (
            "The sender's emergency-stop flag is active. This supervisor's owned process trees "
            "have been stopped and new launches are blocked. The stop flag stays set until explicitly "
            "cleared; clearing it alone does not restart the supervisor."
        )
    else:
        detail = (
            f"Supervisor safety shutdown encountered `{reason}`. New launches are blocked; "
            "check the supervisor terminal and confirm all owned processes stopped before restarting."
        )
    notify_supervisor_stop(sender=sender, detail=detail)


def _is_git_checkout() -> bool:
    result = _run(["git", "rev-parse", "--is-inside-work-tree"])
    return result.returncode == 0 and result.stdout.strip() == "true"


def _upstream_ref() -> str | None:
    result = _run(["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _head() -> str:
    return _run(["git", "rev-parse", "HEAD"], check=True).stdout.strip()


def _commits_behind(upstream: str) -> int:
    result = _run(["git", "rev-list", "--count", f"HEAD..{upstream}"])
    if result.returncode != 0:
        return 0
    return int(result.stdout.strip() or 0)


def _worktree_dirty() -> bool:
    return bool(_run(["git", "status", "--porcelain"]).stdout.strip())


def _stash_local_changes() -> str | None:
    if not _worktree_dirty():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    message = f"openoutreach-supervisor-autostash-{stamp}"
    result = _run(["git", "stash", "push", "-u", "-m", message])
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    if "No local changes" in result.stdout:
        return None
    logger.warning("Stashed local changes before update: %s", message)
    return message


def _pop_stash(message: str) -> None:
    result = _run(["git", "stash", "list"])
    if result.returncode != 0 or message not in result.stdout:
        logger.warning("Auto-stash %s was not found after pull", message)
        return

    pop = _run(["git", "stash", "pop"])
    if pop.returncode != 0:
        raise RuntimeError(
            "git stash pop failed; local conflict may need manual resolution:\n"
            f"{pop.stderr.strip() or pop.stdout.strip()}"
        )
    logger.warning("Re-applied local changes from auto-stash: %s", message)


def _changed_files(before: str, after: str) -> set[str]:
    if before == after:
        return set()
    result = _run(["git", "diff", "--name-only", f"{before}..{after}"])
    if result.returncode != 0:
        return set()
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def _requirements_changed(changed: set[str]) -> bool:
    return any(path in changed for path in REQUIREMENT_PATHS) or any(
        path.startswith("requirements/") for path in changed
    )


def _install_requirements(requirements_file: str) -> None:
    if shutil := _uv_available():
        cmd = [shutil, "pip", "install", "-r", requirements_file]
    else:
        cmd = [sys.executable, "-m", "pip", "install", "-r", requirements_file]
    result = _run(cmd)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())


def _uv_available() -> str | None:
    from shutil import which

    return which("uv")


def _migrate() -> None:
    result = _run([sys.executable, "manage.py", "migrate", "--no-input"])
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())


def _pull_update(*, install: bool, migrate: bool, requirements_file: str) -> bool:
    if not _is_git_checkout():
        return False

    upstream = _upstream_ref()
    if upstream is None:
        logger.debug("No upstream tracking branch; skipping update check")
        return False

    fetch = _run(["git", "fetch"])
    if fetch.returncode != 0:
        detail = fetch.stderr.strip() or fetch.stdout.strip()
        logger.warning("git fetch failed: %s", detail)
        _notify("Daemon supervisor git fetch failed", f"```{detail[:2500]}```")
        return False

    behind = _commits_behind(upstream)
    if behind == 0:
        logger.debug("Up to date with %s", upstream)
        return False

    before = _head()
    stash_message = None
    stash_popped = False
    logger.warning("Found %d new commit(s) on %s; updating", behind, upstream)

    try:
        stash_message = _stash_local_changes()
        pull = _run(["git", "pull", "--ff-only"])
        if pull.returncode != 0:
            raise RuntimeError(pull.stderr.strip() or pull.stdout.strip())
        after = _head()
        changed = _changed_files(before, after)

        if stash_message:
            _pop_stash(stash_message)
            stash_popped = True

        if install and _requirements_changed(changed):
            logger.warning("Requirements changed; installing %s", requirements_file)
            _install_requirements(requirements_file)

        if migrate:
            logger.warning("Running migrations after update")
            _migrate()

        logger.warning("Updated %s -> %s; daemon restart required", before[:8], after[:8])
        _notify(
            "Daemon supervisor updated code",
            f"Pulled {behind} commit(s) from `{upstream}` and restarted daemon.",
        )
        return True
    except SupervisorStopped:
        raise
    except Exception as exc:
        if stash_message and not stash_popped:
            try:
                _pop_stash(stash_message)
            except Exception as pop_exc:
                logger.error("Could not re-apply auto-stash after failed update: %s", pop_exc)
        logger.error("Update failed: %s", exc)
        _notify("Daemon supervisor update failed", f"```{str(exc)[:2500]}```")
        return False


def _maybe_pull_update(args: argparse.Namespace) -> bool:
    """Apply one supervisor update check unless deployment owns code updates."""
    if args.no_update:
        return False
    return _pull_update(
        install=not args.no_install,
        migrate=not args.no_migrate,
        requirements_file=args.requirements,
    )


def _maybe_consume_sender_restart(restart) -> bool:
    """Read this checkout's exact sender flag without interrupting on DB outages."""
    identity = os.getenv("LINKEDIN_USERNAME", "").strip()
    if not identity:
        logger.warning("Sender restart check skipped: LINKEDIN_USERNAME is not configured")
        return False

    import django
    from django.db import InterfaceError, OperationalError, ProgrammingError, connection

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "linkedin.django_settings")
    django.setup()
    from linkedin.supervisor_control import consume_sender_restart

    # This supervisor owns its connection, not the child workers' connections.
    # Reconnect per poll with a bounded timeout instead of hanging on an idle
    # connection while child health checks wait behind the DB control read.
    options = connection.settings_dict.setdefault("OPTIONS", {})
    options.update(
        connect_timeout=5, keepalives=1, keepalives_idle=5,
        keepalives_interval=5, keepalives_count=2, tcp_user_timeout=15000,
    )
    # SKIP LOCKED handles competing row consumers, not schema/table locks.
    # Bound SQL waits too. Preserve other connection options, appending once.
    limits = "-c statement_timeout=5000 -c lock_timeout=1000"
    extra_options = options.get("options", "").strip()
    if not extra_options.endswith(limits):
        options["options"] = f"{extra_options} {limits}".strip()
    try:
        connection.close()
        return consume_sender_restart(linkedin_username=identity, restart=restart)
    except (OperationalError, InterfaceError) as exc:
        logger.warning(
            "Sender restart check unavailable (%s); request stays pending for the next poll",
            type(exc).__name__,
        )
        return False
    except ProgrammingError as exc:
        cause = exc.__cause__
        if getattr(cause, "sqlstate", None) == "42703" and "restart_requested" in str(cause):
            logger.warning("Sender restart control migration is not installed; request check skipped")
            return False
        raise
    finally:
        connection.close()


def _read_sender_stop_state() -> bool | None:
    """Thread-local bounded control read; None means unavailable, not cleared."""
    import django
    from django.db import InterfaceError, OperationalError, ProgrammingError, connection

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "linkedin.django_settings")
    django.setup()
    from linkedin.supervisor_control import read_sender_stop

    identity = os.getenv("LINKEDIN_USERNAME", "").strip()
    options = connection.settings_dict.setdefault("OPTIONS", {})
    options.update(
        connect_timeout=5, keepalives=1, keepalives_idle=5,
        keepalives_interval=5, keepalives_count=2, tcp_user_timeout=15000,
    )
    limits = "-c statement_timeout=5000 -c lock_timeout=1000"
    extra = options.get("options", "").strip()
    if not extra.endswith(limits):
        options["options"] = f"{extra} {limits}".strip()
    try:
        connection.close()
        return read_sender_stop(linkedin_username=identity)
    except (OperationalError, InterfaceError) as exc:
        logger.warning("Emergency-stop check unavailable (%s)", type(exc).__name__)
        return None
    except ProgrammingError as exc:
        cause = exc.__cause__
        if getattr(cause, "sqlstate", None) == "42703" and "stop_requested" in str(cause):
            logger.error("Emergency-stop schema is missing; install migration 0033 before starting workers")
            return None
        raise
    finally:
        connection.close()


def _poll_runtime_controls(args: argparse.Namespace, restart) -> bool:
    """Coalesce a code update and a sender request into one child restart."""
    if _safety is not None and not _safety.preflight():
        return False
    updated = _maybe_pull_update(args)
    attempted = False
    restarted = False

    def perform_restart():
        nonlocal attempted, restarted
        attempted = True
        restarted = restart("git_pull" if updated else "sender_restart")
        return restarted

    acknowledged = _maybe_consume_sender_restart(perform_restart)
    if acknowledged:
        _notify_restart_trigger()
    # If acknowledgement failed after children started, do not restart them a
    # second time in this same poll. The unacknowledged flag remains retryable.
    if updated and not attempted:
        if _safety is not None and not _safety.preflight():
            return False
        perform_restart()
    return restarted


def _start_daemon(*, restart_reason: str = "") -> subprocess.Popen:
    env = os.environ.copy()
    env["OPENOUTREACH_SUPERVISED"] = "1"
    if restart_reason:
        env["OPENOUTREACH_RESTART_REASON"] = restart_reason
    else:
        env.pop("OPENOUTREACH_RESTART_REASON", None)
    logger.warning("Starting daemon child")
    return _launch([sys.executable, "manage.py"], cwd=ROOT_DIR, env=env)


def _start_runtime_worker(*, restart_reason: str = "") -> subprocess.Popen:
    if _linkedin_daemon_enabled():
        return _start_daemon(restart_reason=restart_reason)

    from linkedin.operators import resolve_operator

    operator = resolve_operator(os.getenv("LINKEDIN_USERNAME", "").strip())
    if operator is None or _gmail_account_for_supervisor() is None:
        raise ValueError("Email-only mode requires a configured sender with a Gmail account mapping")
    env = os.environ.copy()
    env["OPENOUTREACH_SUPERVISED"] = "1"
    env.pop("OPENOUTREACH_RESTART_REASON", None)
    logger.warning("LinkedIn daemon disabled; starting browserless email enrichment for %s", operator)
    return _launch(
        [sys.executable, "manage.py", "run_email_enrichment", "--operator", operator],
        cwd=ROOT_DIR,
        env=env,
    )


def _runtime_worker_label() -> str:
    return "Daemon" if _linkedin_daemon_enabled() else "Email enrichment worker"


def _start_feed_collector(run_id: str) -> subprocess.Popen:
    env = os.environ.copy()
    env["OPENOUTREACH_SUPERVISED"] = "1"
    logger.warning("Starting bounded LinkedIn feed sample")
    return _launch(
        [sys.executable, "manage.py", "sample_linkedin_feed", "--run-id", run_id],
        cwd=ROOT_DIR,
        env=env,
    )


def _start_repost_worker() -> subprocess.Popen:
    env = os.environ.copy()
    env["OPENOUTREACH_SUPERVISED"] = "1"
    logger.warning("Starting one-shot LinkedIn repost worker")
    return _launch(
        [sys.executable, "manage.py", "run_linkedin_repost_once"],
        cwd=ROOT_DIR,
        env=env,
    )


def _repost_work_queued() -> bool:
    """Bounded read: does this exact sender have a queued repost that is due? Fails closed."""
    from linkedin.operators import resolve_operator

    operator = resolve_operator(os.getenv("LINKEDIN_USERNAME", "").strip())
    if operator not in REPOST_OPERATORS:
        logger.warning("Repost check skipped: LINKEDIN_USERNAME is not an enabled repost operator")
        return False

    import django
    from django.db import InterfaceError, OperationalError, ProgrammingError, connection

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "linkedin.django_settings")
    django.setup()
    from linkedin.reposts import has_queued_repost

    options = connection.settings_dict.setdefault("OPTIONS", {})
    options.update(
        connect_timeout=5, keepalives=1, keepalives_idle=5,
        keepalives_interval=5, keepalives_count=2, tcp_user_timeout=15000,
    )
    limits = "-c statement_timeout=5000 -c lock_timeout=1000"
    extra = options.get("options", "").strip()
    if not extra.endswith(limits):
        options["options"] = f"{extra} {limits}".strip()
    try:
        connection.close()
        return has_queued_repost(operator)
    except (OperationalError, InterfaceError) as exc:
        logger.warning("Repost queue check unavailable (%s); queued rows wait for the next check", type(exc).__name__)
        return False
    except ProgrammingError as exc:
        # 42P01: no table; 42703: no scheduled_for column yet.
        if getattr(exc.__cause__, "sqlstate", None) in ("42P01", "42703"):
            logger.error("LinkedIn repost table is missing or not migrated; apply migrations 0035-0036 before enabling reposts")
            return False
        raise
    finally:
        connection.close()


def _close_owned_repost_tabs() -> None:
    """Close only targets recorded by this sender's repost worker."""
    try:
        RepostTargetReceipt(
            ROOT_DIR, os.getenv("LINKEDIN_USERNAME", ""), int(os.getenv("LISTENER_CDP_PORT") or 9222),
        ).close_all()
    except Exception as exc:
        logger.error("Repost target cleanup needs review: %s", type(exc).__name__)


def _stop_repost_worker(proc: subprocess.Popen) -> None:
    # Terminate first: the child records an attempted click as uncertain and
    # closes its own tab. Then close anything a killed child left behind.
    _stop_process(proc, label="LinkedIn repost worker")
    _close_owned_repost_tabs()


def _gmail_account_for_supervisor() -> str | None:
    """Resolve this checkout's LinkedIn operator to its shared Gmail mailbox."""
    from gmail.auth import GMAIL_OPERATOR_MAPPING
    from linkedin.operators import resolve_operator

    linkedin_identity = os.getenv("LINKEDIN_USERNAME", "").strip()
    operator = resolve_operator(linkedin_identity)
    mapping = GMAIL_OPERATOR_MAPPING.get(operator)
    if mapping is None:
        return None
    return mapping["gmail_account"]


def _start_gmail_worker(account_key: str) -> subprocess.Popen:
    env = os.environ.copy()
    env["OPENOUTREACH_SUPERVISED"] = "1"
    logger.warning("Starting Gmail worker child for %s", account_key)
    return _launch(
        [
            sys.executable,
            "manage.py",
            "run_gmail_worker",
            "--account",
            account_key,
        ],
        cwd=ROOT_DIR,
        env=env,
    )


def _stop_daemon(proc: subprocess.Popen, timeout_seconds: int = 30) -> None:
    if proc.poll() is not None:
        return
    logger.warning("Stopping %s child pid=%s", _runtime_worker_label(), proc.pid)
    try:
        proc.terminate()
        proc.wait(timeout=timeout_seconds)
        return
    except subprocess.TimeoutExpired:
        logger.warning("%s did not stop after %ss; killing", _runtime_worker_label(), timeout_seconds)
    except ProcessLookupError:
        return
    proc.kill()
    proc.wait(timeout=10)


def _stop_process(proc: subprocess.Popen, *, label: str, timeout_seconds: int = 30) -> None:
    if proc.poll() is not None:
        return
    logger.warning("Stopping %s child pid=%s", label, proc.pid)
    try:
        proc.terminate()
        proc.wait(timeout=timeout_seconds)
        return
    except subprocess.TimeoutExpired:
        logger.warning("%s did not stop after %ss; killing", label, timeout_seconds)
    except ProcessLookupError:
        return
    proc.kill()
    proc.wait(timeout=10)


def _restart_managed_children(
    *, child, gmail_child, feed_child, gmail_account, reason: str, should_stop,
):
    """Replace only this supervisor's children; return None on cancellation.

    Successfully launching processes is not a claim that auth or outbound work
    is healthy. Normal child-exit supervision continues after acknowledgement.
    """
    if should_stop():
        return None
    if gmail_child is not None:
        _stop_process(gmail_child, label="Gmail worker")
    if feed_child is not None:
        _stop_process(feed_child, label="LinkedIn feed collector")
    _stop_daemon(child)
    if should_stop():
        return None

    replacement = _start_runtime_worker(restart_reason=reason)
    replacement_gmail = None
    complete = False
    try:
        if should_stop():
            return None
        if gmail_account is not None:
            replacement_gmail = _start_gmail_worker(gmail_account)
        if should_stop():
            return None
        if replacement.poll() is not None or (
            replacement_gmail is not None and replacement_gmail.poll() is not None
        ):
            logger.error("Replacement worker exited during launch; restart request remains pending")
            return None
        if should_stop():
            return None
        complete = True
        return replacement, replacement_gmail
    finally:
        if not complete:
            if replacement_gmail is not None:
                _stop_process(replacement_gmail, label="Gmail worker")
            _stop_daemon(replacement)


def _env_bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _linkedin_daemon_enabled() -> bool:
    return _env_bool("ENABLE_LINKEDIN_DAEMON", "true")


def _feed_collector_enabled() -> bool:
    return _linkedin_daemon_enabled() and _env_bool("ENABLE_LINKEDIN_FEED_COLLECTOR", "false")


def _linkedin_reposts_enabled() -> bool:
    return _linkedin_daemon_enabled() and _env_bool("ENABLE_LINKEDIN_REPOSTS", "false")


def _feed_schedule() -> FeedSampleSchedule:
    return FeedSampleSchedule(ROOT_DIR, os.getenv("LINKEDIN_USERNAME", ""))


def _feed_collection_should_start(now: datetime | None = None, *, schedule=None) -> bool:
    if not _feed_collector_enabled():
        return False
    return (schedule or _feed_schedule()).due(now or datetime.now(timezone.utc))


def _close_owned_sample_tab(schedule: FeedSampleSchedule) -> None:
    import json

    try:
        state = schedule.read()
        path = schedule.receipt_path(state["run_id"])
        if path.exists():
            receipt = json.loads(path.read_text())
            if receipt.get("target_id"):
                close_sample_target(receipt["cdp_port"], receipt["target_id"])
    except Exception as exc:
        logger.error("Feed target cleanup needs review: %s", type(exc).__name__)


def _next_feed_check_delay(schedule, now: datetime) -> float:
    delay = float(FEED_COLLECTOR_CHECK_SECONDS)
    if schedule is not None:
        due = schedule.read().get("next_due_at")
        if due:
            remaining = (datetime.fromisoformat(due) - now).total_seconds()
            if remaining > 0:
                delay = min(delay, remaining)
    return delay


def supervise(args: argparse.Namespace) -> int:
    """Run no workers until the persistent stop latch has been read safely."""
    global _safety
    if args.once:
        updated = _maybe_pull_update(args)
        return 0 if updated else 1

    safety = SupervisorSafety(check_stop=_read_sender_stop_state, on_stop=_notify_emergency_stop)
    _safety = safety
    try:
        if not safety.preflight():
            logger.warning("Supervisor startup blocked by emergency stop or unavailable control state")
            return 0 if safety.emergency else 1
        safety.start()
        return _supervise_workers(args, safety)
    except SupervisorStopped:
        safety.raise_if_failed()
        return 0
    finally:
        try:
            safety.request_stop(emergency=False)
        finally:
            safety.close()
            _safety = None


def _supervise_workers(args: argparse.Namespace, safety: SupervisorSafety) -> int:

    stop = False
    child: subprocess.Popen | None = None
    gmail_child: subprocess.Popen | None = None
    gmail_account = _gmail_account_for_supervisor()
    feed_child: subprocess.Popen | None = None
    feed_schedule = None
    feed_started_at = None
    feed_schedule_failed = False
    # Off by default; when off none of the repost steps below run.
    reposts_enabled = _linkedin_reposts_enabled()
    repost_child: subprocess.Popen | None = None
    repost_started_at = None

    def _handle_signal(signum, _frame):
        nonlocal stop
        logger.warning("Received signal %s; stopping supervisor", signum)
        stop = True
        safety.request_stop(emergency=False)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    def hold_sample(reason: str) -> None:
        nonlocal feed_schedule_failed
        feed_schedule_failed = True
        _close_owned_sample_tab(feed_schedule)
        try:
            feed_schedule.hold(reason)
        except Exception as exc:
            logger.error("Feed hold needs local state review: %s", type(exc).__name__)

    def stop_repost_child() -> None:
        nonlocal repost_child, repost_started_at
        if repost_child is not None:
            _stop_repost_worker(repost_child)
            repost_child = None
            repost_started_at = None

    def restart_children(reason: str) -> bool:
        nonlocal child, gmail_child, feed_child, feed_started_at
        # Separate step: the repost child attaches to the daemon browser, so
        # it stops before that browser's worker is replaced.
        stop_repost_child()
        result = _restart_managed_children(
            child=child, gmail_child=gmail_child, feed_child=feed_child,
            gmail_account=gmail_account, reason=reason,
            should_stop=lambda: stop or safety.stopped.is_set(),
        )
        if result is None:
            return False
        child, gmail_child = result
        if feed_child is not None:
            hold_sample("Collection interrupted by sender restart; review before resuming.")
            feed_child = None
            feed_started_at = None
        logger.warning("Restarted this sender's managed workers: %s", reason)
        return not stop and not safety.stopped.is_set()

    initial_updated = _maybe_pull_update(args)
    safety.raise_if_failed()
    if safety.stopped.is_set():
        return 0
    child = _start_runtime_worker(restart_reason="git_pull" if initial_updated else "")
    if gmail_account is None:
        logger.warning(
            "No Gmail account mapping for LINKEDIN_USERNAME; "
            "Gmail worker was not started"
        )
    else:
        gmail_child = _start_gmail_worker(gmail_account)
    next_poll = time.monotonic() + args.poll_seconds
    next_feed_check = time.monotonic() + FEED_COLLECTOR_CHECK_SECONDS
    if reposts_enabled:
        next_repost_check = time.monotonic() + REPOST_CHECK_SECONDS

    while not stop and not safety.stopped.is_set():
        safety.raise_if_failed()
        # Child exits must not keep resetting/starving the five-minute control
        # timer. --no-update only disables Git; DB restart requests still work.
        if time.monotonic() >= next_poll:
            _poll_runtime_controls(args, restart_children)
            next_poll = time.monotonic() + args.poll_seconds
            if stop or safety.stopped.is_set():
                break

        if gmail_child is not None:
            gmail_code = gmail_child.poll()
            if gmail_code is not None:
                logger.error(
                    "Gmail worker exited with status %s; restarting in %ss",
                    gmail_code,
                    args.restart_delay,
                )
                _notify(
                    "Gmail worker exited unexpectedly",
                    f"Account `{gmail_account}` exited with status `{gmail_code}`. Restarting. "
                    f"Local diagnostics: `data/logs/gmail-worker-{gmail_account}.log` "
                    "(look for worker_crashed; abrupt or pre-startup exits may have no entry).",
                )
                time.sleep(args.restart_delay)
                if stop or safety.stopped.is_set():
                    break
                gmail_child = _start_gmail_worker(gmail_account)

        code = child.poll()
        if code is not None:
            if code != 0:
                logger.error("%s exited with status %s; restarting in %ss", _runtime_worker_label(), code, args.restart_delay)
                _notify(f"{_runtime_worker_label()} child exited unexpectedly", f"Exit status `{code}`. Restarting.")
                time.sleep(args.restart_delay)
            else:
                logger.warning("%s exited cleanly; restarting in %ss", _runtime_worker_label(), args.restart_delay)
                time.sleep(args.restart_delay)
            if stop or safety.stopped.is_set():
                break
            if feed_child is not None and feed_child.poll() is None:
                _stop_process(feed_child, label="LinkedIn feed collector")
                hold_sample("Collection interrupted by worker exit; review before resuming.")
                feed_child = None
                feed_started_at = None
            stop_repost_child()
            child = _start_runtime_worker(restart_reason="process_exit")
            continue

        if feed_child is not None:
            feed_code = feed_child.poll()
            if feed_code is None and time.monotonic() - feed_started_at >= MAX_SECONDS + PROCESS_GRACE_SECONDS:
                _close_owned_sample_tab(feed_schedule)
                _stop_process(feed_child, label="LinkedIn feed sample")
                feed_code = 124
            if feed_code is not None:
                if feed_code != 0:
                    _close_owned_sample_tab(feed_schedule)
                try:
                    feed_schedule.finish(datetime.now(timezone.utc), feed_code)
                    held = bool(feed_schedule.read().get("hold"))
                except Exception:
                    hold_sample("Sample completion state could not be saved; review before resuming.")
                    held = True
                if held:
                    _notify("LinkedIn feed collection held", "A sample did not complete cleanly. Review its local receipt before resuming; no automatic retry will run.")
                else:
                    logger.warning("Feed sample complete; next check: %s", feed_schedule.read()["next_due_at"])
                feed_child = None
                feed_started_at = None

        if time.monotonic() >= next_feed_check:
            if _feed_collector_enabled() and not feed_schedule_failed and feed_child is None:
                try:
                    feed_schedule = feed_schedule or _feed_schedule()
                    if _feed_collection_should_start(schedule=feed_schedule):
                        run_id = feed_schedule.begin(datetime.now(timezone.utc))
                        feed_started_at = time.monotonic()
                        feed_child = _start_feed_collector(run_id)
                except Exception as exc:
                    # Invalid/corrupt local scheduling state cannot trigger a loop
                    # or take down unrelated sender/Gmail workers.
                    feed_schedule_failed = True
                    logger.error("Feed scheduling held: %s", type(exc).__name__)
                    _notify("LinkedIn feed scheduling held", "Review local feed-sampling state and configuration before restarting the supervisor.")
            delay = FEED_COLLECTOR_CHECK_SECONDS
            if not feed_schedule_failed:
                try:
                    delay = _next_feed_check_delay(feed_schedule, datetime.now(timezone.utc))
                except Exception:
                    hold_sample("Invalid feed schedule; review before resuming.")
            next_feed_check = time.monotonic() + delay

        if reposts_enabled:
            if repost_child is not None:
                repost_code = repost_child.poll()
                if repost_code is None and time.monotonic() - repost_started_at >= REPOST_TIMEOUT_SECONDS:
                    logger.error("LinkedIn repost worker exceeded %ss; stopping it", REPOST_TIMEOUT_SECONDS)
                    _stop_repost_worker(repost_child)
                    repost_code = 124
                if repost_code is not None:
                    if repost_code != 0:
                        logger.warning("LinkedIn repost worker exited with status %s", repost_code)
                    _close_owned_repost_tabs()
                    repost_child = None
                    repost_started_at = None
            if repost_child is None and time.monotonic() >= next_repost_check:
                next_repost_check = time.monotonic() + REPOST_CHECK_SECONDS
                if _repost_work_queued():
                    repost_started_at = time.monotonic()
                    repost_child = _start_repost_worker()

        time.sleep(1)

    safety.raise_if_failed()
    stop_repost_child()
    if child is not None:
        _stop_daemon(child)
    if gmail_child is not None:
        _stop_process(gmail_child, label="Gmail worker")
    if feed_child is not None:
        hold_sample("Collection interrupted by supervisor shutdown; review before resuming.")
        _stop_process(feed_child, label="LinkedIn feed collector")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the supervised LinkedIn and Gmail OpenOutreach runtime"
    )
    parser.add_argument("--poll-seconds", type=int, default=DEFAULT_POLL_SECONDS)
    parser.add_argument("--restart-delay", type=int, default=10)
    parser.add_argument("--requirements", default="requirements/local.txt")
    parser.add_argument("--no-install", action="store_true", help="Do not install requirements after dependency changes")
    parser.add_argument("--no-migrate", action="store_true", help="Do not run migrations after pulling updates")
    parser.add_argument(
        "--no-update",
        action="store_true",
        help="Do not fetch or pull Git updates (for immutable/container deployments)",
    )
    parser.add_argument("--once", action="store_true", help="Check/pull once and exit without starting the daemon")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args()


def main() -> int:
    _load_env()
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    if args.no_update:
        logger.warning("Supervisor code updates disabled by --no-update")
    else:
        logger.warning(
            "Supervisor polling git every %ss; requirements=%s",
            args.poll_seconds,
            args.requirements,
        )
    logger.warning("Supervisor polling sender restart requests every %ss", args.poll_seconds)
    logger.warning("Supervisor emergency-stop watchdog checks every 15s, independently of Git")
    return supervise(args)


if __name__ == "__main__":
    raise SystemExit(main())
