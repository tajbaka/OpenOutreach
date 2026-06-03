"""In-daemon degraded-state detection.

Where `node_monitor` answers "is the daemon process alive at all" (a peer
watches it), this module answers "is the daemon alive but not actually
working" — a question only the daemon itself can answer about itself.

`TaskFailureTracker` posts to the ops Slack channel via `notify_degraded`
when the daemon is alive but every recent task is failing. One instance is
created per process, so it is sender-scoped by construction.

Dedup is in-process: each condition alerts at most once per
`DEGRADED_REALERT_HOURS`. Reset on restart — fine, these are best-effort.
"""
from __future__ import annotations

import logging
import time

from linkedin import conf
from linkedin.notifications.slack import notify_degraded

logger = logging.getLogger(__name__)

# {condition_key: last-alerted epoch}. Module-level so the cooldown spans
# the whole process; condition keys are fixed strings, so the dict is tiny.
_LAST_ALERTED: dict[str, float] = {}


def _should_alert(key: str) -> bool:
    """True at most once per `DEGRADED_REALERT_HOURS` for a given key."""
    now = time.time()
    last = _LAST_ALERTED.get(key)
    if last is not None and now - last < conf.DEGRADED_REALERT_HOURS * 3600:
        return False
    _LAST_ALERTED[key] = now
    return True


class TaskFailureTracker:
    """Consecutive-failure counter for the daemon's task-dispatch loop.

    One instance per daemon process — sender-scoped by construction. The
    daemon calls `record_success()` / `record_failure()` around each task;
    `TASK_FAILURE_STREAK_THRESHOLD` failures in a row fire one alert."""

    def __init__(self, sender: str):
        self._sender = sender
        self._streak = 0

    def record_success(self) -> None:
        self._streak = 0

    def record_failure(self) -> None:
        self._streak += 1
        if (
            self._streak >= conf.TASK_FAILURE_STREAK_THRESHOLD
            and _should_alert("task_failure_streak")
        ):
            logger.warning(
                "Degraded: %d consecutive task failures (sender=%s)",
                self._streak, self._sender,
            )
            notify_degraded(
                sender=self._sender,
                title=(
                    f"{self._sender}'s daemon: "
                    f"{self._streak} tasks failed in a row"
                ),
                detail=(
                    "The daemon is running but every recent task is "
                    "failing. Likely an expired LinkedIn session, an "
                    "account challenge/block, or a systemic bug — check "
                    "the logs."
                ),
            )
