"""Daemon startup git update check.

Called once at daemon startup (manage.py, no-args branch) before migrations
so a pull can restart the process before stale code runs. Compares the
current branch's local HEAD against its configured upstream (`@{u}`), not a
hardcoded branch. Django-free.
"""
from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _git(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    """Run a git command in the repo root."""
    return subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=check,
    )


def _is_git_checkout() -> bool:
    """True only if the repo root is inside a git working tree."""
    try:
        result = _git(["rev-parse", "--is-inside-work-tree"], check=False)
    except FileNotFoundError:
        return False
    return result.returncode == 0 and result.stdout.strip() == "true"


def _upstream_ref() -> str | None:
    """Return the current branch's upstream tracking ref, if any."""
    result = _git(
        ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _commits_behind(upstream: str) -> int:
    """Return how many commits local HEAD is behind `upstream`."""
    result = _git(["rev-list", "--count", f"HEAD..{upstream}"], check=False)
    if result.returncode != 0:
        return 0
    return int(result.stdout.strip() or 0)


def _stdio_is_tty() -> bool:
    """True only when both stdin and stdout are interactive terminals."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def _pull_and_exit() -> None:
    """Run `git pull --ff-only`, then exit the process."""
    try:
        _git(["pull", "--ff-only"])
    except subprocess.CalledProcessError as exc:
        from linkedin.notifications.slack import notify_error

        stderr = (exc.stderr or "").strip()
        logger.error("version check: 'git pull --ff-only' failed:\n%s", stderr)
        notify_error(
            "daemon:update",
            exc,
            context={"stderr": stderr[:500]},
        )
        sys.exit(1)

    logger.info("version check: updated, restart required; exiting")
    sys.exit(0)


def check_for_updates() -> None:
    """Check whether the checkout is behind upstream and pull if approved."""
    if not _is_git_checkout():
        logger.debug("version check: not a git checkout, skipping")
        return

    upstream = _upstream_ref()
    if upstream is None:
        logger.debug("version check: no upstream tracking branch, skipping")
        return

    fetch = _git(["fetch"], check=False)
    if fetch.returncode != 0:
        logger.warning(
            "version check: 'git fetch' failed, continuing on current code: %s",
            fetch.stderr.strip(),
        )
        return

    behind = _commits_behind(upstream)
    if behind == 0:
        logger.debug("version check: up to date with %s", upstream)
        return

    logger.warning(
        "version check: %d new commit(s) available on %s",
        behind,
        upstream,
    )

    if _stdio_is_tty():
        answer = input(
            f"Pull {behind} new commit(s) and restart? [y/N] "
        ).strip().lower()
        if answer not in {"y", "yes"}:
            logger.warning(
                "version check: update declined, continuing on current code"
            )
            return
    else:
        logger.info("version check: no TTY, auto-pulling %d commit(s)", behind)

    _pull_and_exit()
