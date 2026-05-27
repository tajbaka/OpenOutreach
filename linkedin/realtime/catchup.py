"""Daemon-startup catch-up for the realtime listener.

The listener only runs during active hours and only while the process is
up. The heartbeat file records when it was last alive; the gap between
that and now is exactly the window inbound messages may have been missed
(off-hours + any downtime). On startup, if the gap exceeds a threshold:

  - Interactive (TTY): prompt to run backfill_messages first.
  - Headless (no TTY): log a WARNING; don't auto-run a long unattended
    LinkedIn session.

Below the threshold (a quick restart), do nothing.
"""
from __future__ import annotations

import logging
import os
import select
import sys

from django.core.management import call_command
from django.utils import timezone

from linkedin.conf import LISTENER_CATCHUP_GAP_MINUTES
from linkedin.realtime.heartbeat import read_heartbeat

logger = logging.getLogger(__name__)

BACKFILL_PROMPT_TIMEOUT_SECONDS = 5


def compute_gap_minutes(username: str, now=None) -> float:
    """Minutes since the listener heartbeat. inf if there is no heartbeat."""
    last = read_heartbeat(username)
    if last is None:
        return float("inf")
    now = now or timezone.now()
    return (now - last).total_seconds() / 60.0


def _prompt_yes_no_default_no(prompt: str, *, timeout_seconds: int) -> str:
    """Prompt for y/N, defaulting to no after `timeout_seconds`.

    `input()` cannot time out, so use select on stdin for the daemon's TTY
    startup prompt. In test/non-standard stdin environments where select is not
    supported, fall back to `input()` so existing mocks still exercise the
    branch.
    """
    print(prompt, end="", flush=True)
    try:
        readable, _, _ = select.select([sys.stdin], [], [], timeout_seconds)
    except (OSError, ValueError):
        return input().strip().lower()

    if not readable:
        print("n")
        return "n"

    return sys.stdin.readline().strip().lower()


def run_startup_catchup(
    *,
    username: str,
    account_label: str,
    interactive: bool | None = None,
    prompt_timeout_seconds: int = BACKFILL_PROMPT_TIMEOUT_SECONDS,
) -> None:
    """Surface (and optionally backfill) the listener's missed window.

    `username` is the LinkedIn username of the daemon's account (keys the
    heartbeat file). `account_label` is the backfill_messages slot for that
    account ("primary"). `interactive` defaults to whether stdin is a TTY.
    """
    gap = compute_gap_minutes(username)
    if gap < LISTENER_CATCHUP_GAP_MINUTES:
        logger.info(
            "Realtime listener gap %.0f min (< %d min threshold) — no catch-up needed",
            gap, LISTENER_CATCHUP_GAP_MINUTES,
        )
        return

    hours = gap / 60.0
    gap_desc = "an unknown duration" if gap == float("inf") else f"~{hours:.1f}h"

    if os.getenv("OPENOUTREACH_RESTART_REASON") == "git_pull":
        logger.info(
            "Realtime listener was off for %s, but daemon restarted after "
            "supervisor git pull — skipping interactive backfill prompt",
            gap_desc,
        )
        return

    if interactive is None:
        interactive = sys.stdin.isatty()

    if not interactive:
        logger.warning(
            "Realtime listener was off for %s — inbound messages in that "
            "window were not detected. Run `manage.py backfill_messages "
            "--account %s` to catch up.",
            gap_desc, account_label,
        )
        return

    answer = _prompt_yes_no_default_no(
        f"Listener was off {gap_desc}. Run backfill_messages to catch up "
        f"first? [y/N] ",
        timeout_seconds=prompt_timeout_seconds,
    )
    if answer == "y":
        logger.info("Running backfill_messages catch-up for account=%s", account_label)
        call_command(
            "backfill_messages", account=account_label, skip_prereq_gate=True,
        )
    else:
        logger.info("Skipped backfill catch-up — continuing into the task loop")
