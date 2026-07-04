#!/usr/bin/env python
"""Terminal-run supervisor for the OpenOutreach daemon.

Runs `manage.py` as a child process, polls the current Git upstream for new
commits, and restarts the daemon after applying updates. This is intentionally
OS-agnostic: run it in a terminal on macOS, Windows, or Linux.
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
from datetime import datetime
from pathlib import Path
from shutil import which
from zoneinfo import ZoneInfo


ROOT_DIR = Path(__file__).resolve().parent
DEFAULT_POLL_SECONDS = 300
FEED_COLLECTOR_CHECK_SECONDS = 60
REQUIREMENT_PATHS = (
    "requirements/base.txt",
    "requirements/local.txt",
    "requirements/production.txt",
    "requirements/crm.txt",
)

logger = logging.getLogger("daemon_supervisor")


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
    proc = subprocess.run(
        args,
        cwd=ROOT_DIR,
        capture_output=True,
        text=True,
        check=False,
    )
    result = CommandResult(proc.returncode, proc.stdout or "", proc.stderr or "")
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
    except Exception as exc:
        if stash_message and not stash_popped:
            try:
                _pop_stash(stash_message)
            except Exception as pop_exc:
                logger.error("Could not re-apply auto-stash after failed update: %s", pop_exc)
        logger.error("Update failed: %s", exc)
        _notify("Daemon supervisor update failed", f"```{str(exc)[:2500]}```")
        return False


def _start_daemon(*, restart_reason: str = "") -> subprocess.Popen:
    env = os.environ.copy()
    env["OPENOUTREACH_SUPERVISED"] = "1"
    if restart_reason:
        env["OPENOUTREACH_RESTART_REASON"] = restart_reason
    else:
        env.pop("OPENOUTREACH_RESTART_REASON", None)
    logger.warning("Starting daemon child")
    return subprocess.Popen([sys.executable, "manage.py"], cwd=ROOT_DIR, env=env)


def _start_feed_collector() -> subprocess.Popen:
    env = os.environ.copy()
    env["OPENOUTREACH_SUPERVISED"] = "1"
    logger.warning("Starting LinkedIn feed collector child")
    return subprocess.Popen(
        [sys.executable, "manage.py", "collect_linkedin_feed"],
        cwd=ROOT_DIR,
        env=env,
    )


def _stop_daemon(proc: subprocess.Popen, timeout_seconds: int = 30) -> None:
    if proc.poll() is not None:
        return
    logger.warning("Stopping daemon child pid=%s", proc.pid)
    try:
        proc.terminate()
        proc.wait(timeout=timeout_seconds)
        return
    except subprocess.TimeoutExpired:
        logger.warning("Daemon did not stop after %ss; killing", timeout_seconds)
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


def _env_bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _feed_collector_enabled() -> bool:
    return _env_bool("ENABLE_LINKEDIN_FEED_COLLECTOR", "false")


def _feed_collection_local_now() -> datetime:
    tz_name = os.getenv("LINKEDIN_FEED_COLLECTION_TIMEZONE", "America/Toronto")
    return datetime.now(ZoneInfo(tz_name))


def _feed_collection_due_today(now: datetime | None = None) -> bool:
    if not _feed_collector_enabled():
        return False
    now = now or _feed_collection_local_now()
    hour = int(os.getenv("LINKEDIN_FEED_COLLECTION_HOUR", "17") or 17)
    minute = int(os.getenv("LINKEDIN_FEED_COLLECTION_MINUTE", "0") or 0)
    return (now.hour, now.minute) >= (hour, minute)


def _feed_collection_retry_seconds() -> int:
    return int(os.getenv("LINKEDIN_FEED_COLLECTION_RETRY_MINUTES", "60") or 60) * 60


def supervise(args: argparse.Namespace) -> int:
    if args.once:
        updated = _pull_update(
            install=not args.no_install,
            migrate=not args.no_migrate,
            requirements_file=args.requirements,
        )
        return 0 if updated else 1

    stop = False
    child: subprocess.Popen | None = None
    feed_child: subprocess.Popen | None = None
    feed_spawned_date = None
    feed_retry_after = 0.0

    def _handle_signal(signum, _frame):
        nonlocal stop
        logger.warning("Received signal %s; stopping supervisor", signum)
        stop = True
        if child is not None:
            _stop_daemon(child)
        if feed_child is not None:
            _stop_process(feed_child, label="LinkedIn feed collector")

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    initial_updated = _pull_update(
        install=not args.no_install,
        migrate=not args.no_migrate,
        requirements_file=args.requirements,
    )
    child = _start_daemon(restart_reason="git_pull" if initial_updated else "")
    next_poll = time.monotonic() + args.poll_seconds
    next_feed_check = time.monotonic() + FEED_COLLECTOR_CHECK_SECONDS

    while not stop:
        code = child.poll()
        if code is not None:
            if code != 0:
                logger.error("Daemon exited with status %s; restarting in %ss", code, args.restart_delay)
                _notify("Daemon child exited unexpectedly", f"Exit status `{code}`. Restarting.")
                time.sleep(args.restart_delay)
            else:
                logger.warning("Daemon exited cleanly; restarting in %ss", args.restart_delay)
                time.sleep(args.restart_delay)
            if stop:
                break
            if feed_child is not None and feed_child.poll() is None:
                _stop_process(feed_child, label="LinkedIn feed collector")
                feed_child = None
                feed_retry_after = time.monotonic() + _feed_collection_retry_seconds()
            child = _start_daemon(restart_reason="process_exit")
            next_poll = time.monotonic() + args.poll_seconds
            continue

        if feed_child is not None:
            feed_code = feed_child.poll()
            if feed_code is not None:
                if feed_code == 0:
                    feed_spawned_date = _feed_collection_local_now().date()
                    logger.warning("LinkedIn feed collector exited cleanly")
                else:
                    feed_retry_after = time.monotonic() + _feed_collection_retry_seconds()
                    logger.error(
                        "LinkedIn feed collector exited with status %s; retrying later",
                        feed_code,
                    )
                    _notify(
                        "LinkedIn feed collector failed",
                        f"Exit status `{feed_code}`. Supervisor will retry later.",
                    )
                feed_child = None

        if time.monotonic() >= next_poll:
            if _pull_update(
                install=not args.no_install,
                migrate=not args.no_migrate,
                requirements_file=args.requirements,
            ):
                if feed_child is not None and feed_child.poll() is None:
                    _stop_process(feed_child, label="LinkedIn feed collector")
                    feed_child = None
                    feed_retry_after = time.monotonic() + _feed_collection_retry_seconds()
                _stop_daemon(child)
                if stop:
                    break
                child = _start_daemon(restart_reason="git_pull")
            next_poll = time.monotonic() + args.poll_seconds

        if time.monotonic() >= next_feed_check:
            local_now = _feed_collection_local_now()
            if (
                _feed_collection_due_today(local_now)
                and feed_child is None
                and feed_spawned_date != local_now.date()
                and time.monotonic() >= feed_retry_after
            ):
                feed_child = _start_feed_collector()
            next_feed_check = time.monotonic() + FEED_COLLECTOR_CHECK_SECONDS

        time.sleep(1)

    if child is not None:
        _stop_daemon(child)
    if feed_child is not None:
        _stop_process(feed_child, label="LinkedIn feed collector")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run and auto-update the OpenOutreach daemon")
    parser.add_argument("--poll-seconds", type=int, default=DEFAULT_POLL_SECONDS)
    parser.add_argument("--restart-delay", type=int, default=10)
    parser.add_argument("--requirements", default="requirements/local.txt")
    parser.add_argument("--no-install", action="store_true", help="Do not install requirements after dependency changes")
    parser.add_argument("--no-migrate", action="store_true", help="Do not run migrations after pulling updates")
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
    logger.warning(
        "Supervisor polling git every %ss; requirements=%s",
        args.poll_seconds,
        args.requirements,
    )
    return supervise(args)


if __name__ == "__main__":
    raise SystemExit(main())
