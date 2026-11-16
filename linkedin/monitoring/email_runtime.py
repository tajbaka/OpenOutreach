"""Heartbeat owned by the email-only supervisor, not either child alone.

Ticks run on the supervisor loop so a stalled supervisor cannot keep reporting
healthy children. Missing/exited workers stop refreshes; graceful shutdown
clears only this supervisor's last heartbeat after both children have exited.
"""
from __future__ import annotations

import logging
import time

from django.db import connection

from linkedin import conf
from linkedin.monitoring.node_monitor import check_peers, write_heartbeat

logger = logging.getLogger(__name__)


class EmailRuntimeHeartbeat:
    def __init__(self, sender: str):
        self.sender = sender
        self._next_tick = 0.0
        self._last_alive = None

    @staticmethod
    def _database(operation):
        # Match the supervisor control reads: heartbeat trouble must not hang
        # worker recovery or its independent emergency-stop watchdog.
        options = connection.settings_dict.setdefault("OPTIONS", {})
        options.update(connect_timeout=5, keepalives=1, keepalives_idle=5,
                       keepalives_interval=5, keepalives_count=2, tcp_user_timeout=15000)
        limits = "-c statement_timeout=5000 -c lock_timeout=1000"
        extra = options.get("options", "").strip()
        if not extra.endswith(limits):
            options["options"] = f"{extra} {limits}".strip()
        try:
            connection.close()
            return operation()
        finally:
            connection.close()

    def tick(self, runtime, gmail, *, stopped: bool) -> None:
        if stopped or runtime is None or gmail is None or runtime.poll() is not None or gmail.poll() is not None:
            self._next_tick = 0.0
            return
        now = time.monotonic()
        if now < self._next_tick:
            return
        self._next_tick = now + max(1, conf.MONITOR_INTERVAL_SECONDS)
        try:
            self._last_alive = self._database(
                lambda: write_heartbeat(self.sender, runtime_mode="email_only"),
            )
            self._database(lambda: check_peers(self.sender))
        except Exception as exc:
            logger.warning("Email runtime heartbeat unavailable (%s)", type(exc).__name__)

    def stopped(self, runtime, gmail) -> None:
        if self._last_alive is None or any(p is not None and p.poll() is None for p in (runtime, gmail)):
            return

        def clear_owned():
            from linkedin.models import DaemonHeartbeat

            # A replacement runtime may already have started. Never clear its
            # newer heartbeat, nor a LinkedIn daemon's resumed heartbeat.
            DaemonHeartbeat.objects.filter(
                sender=self.sender, runtime_mode="email_only", last_alive=self._last_alive,
            ).update(last_alive=None, down_alerted_at=None)

        try:
            self._database(clear_owned)
        except Exception as exc:
            logger.warning("Email runtime heartbeat cleanup unavailable (%s)", type(exc).__name__)
