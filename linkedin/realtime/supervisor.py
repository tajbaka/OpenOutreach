"""Supervises the realtime listener child process from inside the daemon.

The daemon owns the browser; the listener (`manage.py listen_realtime`)
runs as a child process that shares it over CDP. The supervisor spawns the
child, restarts it if it dies, gives up after repeated spawn failures
(degrading to polling), and kills it when the daemon goes off-hours.

Each new listener process gets a missed-reply catch-up (`catchup`, a
`linkedin.realtime.catchup.ListenerCatchup`): its window is read from the old
heartbeat just before the spawn, then run on the daemon's thread.

Process management only — no Playwright, no browser. Fully unit-testable.
"""
from __future__ import annotations

import logging
import subprocess
import sys

from linkedin.conf import ROOT_DIR

logger = logging.getLogger(__name__)


class ListenerSupervisor:
    """Owns the lifecycle of the listener child process."""

    # Consecutive spawn failures after which the supervisor stops trying for
    # the rest of the current active period (daemon degrades to polling).
    MAX_SPAWN_FAILURES = 5
    STOP_TIMEOUT_SECONDS = 20

    def __init__(self, catchup=None):
        self._proc: subprocess.Popen | None = None
        self._spawn_failures = 0
        self._catchup = catchup

    def ensure_running(self) -> None:
        """Spawn the listener if it is not currently alive. Idempotent —
        call once per active-hours daemon loop iteration."""
        if self._proc is not None and self._proc.poll() is None:
            self._run_catchup()  # alive; a throttled window may now be due
            return
        if self._proc is not None:
            logger.warning("Realtime listener process exited (code=%s) — respawning",
                            self._proc.returncode)
            self._proc = None
        if self._spawn_failures >= self.MAX_SPAWN_FAILURES:
            return  # gave up for this active period
        # Read the window before the new child can stamp a fresh heartbeat.
        window = self._catchup.window() if self._catchup is not None else None
        if self._spawn() and self._catchup is not None:
            self._catchup.request(window)
            self._run_catchup()

    def _run_catchup(self) -> None:
        if self._catchup is not None:
            self._catchup.run_pending()

    def _spawn(self) -> bool:
        try:
            manage_py = ROOT_DIR / "manage.py"
            self._proc = subprocess.Popen(
                [sys.executable, str(manage_py), "listen_realtime"],
                cwd=str(ROOT_DIR),
            )
            self._spawn_failures = 0
            logger.info("Realtime listener child process spawned (pid=%s)", self._proc.pid)
            return True
        except Exception as e:
            self._spawn_failures += 1
            logger.warning(
                "Failed to spawn realtime listener (%d/%d): %s",
                self._spawn_failures, self.MAX_SPAWN_FAILURES, e,
            )
            if self._spawn_failures >= self.MAX_SPAWN_FAILURES:
                logger.error(
                    "Realtime listener spawn gave up — daemon continues without "
                    "realtime (polling still covers inbound messages)."
                )
            return False

    def stop(self) -> None:
        """Stop and reap the child before permitting another listener to start.

        Also clears the spawn-failure count so the next active period starts
        fresh (off-hours is a natural reset point)."""
        if self._proc is not None and self._proc.poll() is None:
            try:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=self.STOP_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    logger.warning("Realtime listener shutdown timed out — killing child")
                    self._proc.kill()
                    self._proc.wait(timeout=5)
                logger.info("Realtime listener child process terminated")
            except ProcessLookupError:
                self._proc.wait(timeout=5)
        elif self._proc is not None:
            self._proc.wait(timeout=5)
        self._proc = None
        self._spawn_failures = 0
