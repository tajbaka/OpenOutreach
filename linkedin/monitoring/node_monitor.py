"""Peer-node liveness monitoring.

Each daemon is a "node". Its `NodeMonitor` background thread stamps the
node's own `DaemonHeartbeat` row every `MONITOR_INTERVAL_SECONDS` and scans
every other node's row; a node whose heartbeat is older than
`PEER_STALE_MINUTES` is reported down to the ops Slack channel.

No third-party service: the "always-up watcher" is just the other daemons
plus Neon. Coverage therefore needs >=2 daemons running — a lone daemon has
no peer to watch it (an accepted limitation, see the brainstorm).

`down_alerted_at` on the row is an atomic claim+cooldown marker: the peer
that wins the UPDATE posts (so N peers don't all alert for one outage), and
the row is re-claimable only after `DEGRADED_REALERT_HOURS`.
"""
from __future__ import annotations

import logging
import threading

from django.db import connection
from django.db.models import Q
from django.utils import timezone
from datetime import timedelta

from linkedin import conf
from linkedin.notifications.slack import notify_degraded

logger = logging.getLogger(__name__)


def write_heartbeat(sender: str) -> None:
    """Stamp this node's heartbeat. Clears `down_alerted_at` — a node that
    is beating is alive, so any prior down-claim is void and the next real
    outage should alert afresh."""
    from linkedin.models import DaemonHeartbeat

    DaemonHeartbeat.objects.update_or_create(
        sender=sender,
        defaults={"last_alive": timezone.now(), "down_alerted_at": None},
    )


def clear_heartbeat(sender: str) -> None:
    """Mark this node intentionally stopped (clean daemon exit).

    `last_alive = NULL` tells peers "not expected to be running" so they do
    not false-alarm on a daemon that exited on purpose (empty queue)."""
    from linkedin.models import DaemonHeartbeat

    DaemonHeartbeat.objects.filter(sender=sender).update(
        last_alive=None, down_alerted_at=None,
    )


def check_peers(self_sender: str) -> None:
    """Scan peer heartbeats; Slack-alert each peer that has gone stale.

    The claim is atomic: `filter(...).update(down_alerted_at=now)` returns
    the row count, and Postgres row locking serialises racing peers — so
    exactly one peer posts per outage. Re-claimable after the cooldown."""
    from linkedin.models import DaemonHeartbeat

    now = timezone.now()
    stale_before = now - timedelta(minutes=conf.PEER_STALE_MINUTES)
    realert_before = now - timedelta(hours=conf.DEGRADED_REALERT_HOURS)

    stale_peers = DaemonHeartbeat.objects.filter(
        last_alive__isnull=False,
        last_alive__lt=stale_before,
    ).exclude(sender=self_sender)

    for peer in stale_peers:
        claimed = (
            DaemonHeartbeat.objects.filter(pk=peer.pk)
            .filter(
                Q(down_alerted_at__isnull=True)
                | Q(down_alerted_at__lt=realert_before)
            )
            .update(down_alerted_at=now)
        )
        if not claimed:
            continue  # another peer already alerted within the cooldown
        age_min = int((now - peer.last_alive).total_seconds() // 60)
        logger.warning(
            "Peer node %r looks down — %d min since last heartbeat",
            peer.sender, age_min,
        )
        notify_degraded(
            sender=peer.sender,
            title=f"{peer.sender}'s daemon looks down",
            detail=(
                f"No heartbeat for {age_min} min "
                f"(threshold {conf.PEER_STALE_MINUTES} min). "
                f"Last seen {peer.last_alive:%Y-%m-%d %H:%M} UTC."
            ),
        )


class NodeMonitor:
    """Background thread that runs `write_heartbeat` + `check_peers` on a
    fixed cadence.

    A separate thread (same pattern as `EnrichmentWorker`) so monitoring
    keeps beating through the daemon's long off-hours sleeps — the
    heartbeat reflects "process alive", not "actively working".

    DB-only, no browser, no HTTP. Tick exceptions are logged and swallowed:
    monitoring is an enhancement and must never crash the outreach daemon
    (same posture as the realtime listener)."""

    def __init__(self, sender: str):
        self._sender = sender
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Spawn the monitor thread. Idempotent."""
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="node-monitor", daemon=True,
        )
        self._thread.start()
        logger.info("Node monitor started (sender=%s)", self._sender)

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the loop to exit and join the thread. Idempotent."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
            logger.info("Node monitor stopped")

    def _run(self) -> None:
        # First tick runs immediately — a freshly started daemon is visible
        # to peers without waiting one full interval.
        while not self._stop.is_set():
            try:
                write_heartbeat(self._sender)
                check_peers(self._sender)
            except Exception:
                logger.exception("Node monitor tick failed")
            finally:
                # This thread owns its own thread-local Neon connection;
                # close it each tick so Neon's idle timeout can't hand us a
                # dead socket across the interval sleep.
                connection.close()
            self._stop.wait(conf.MONITOR_INTERVAL_SECONDS)
