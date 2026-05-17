"""EnrichmentWorker — the daemon's phone-enrichment task loop.

Runs as a SINGLE background thread spawned by run_daemon. Claims
ENRICH_PHONE tasks (the outbound loop excludes them), runs the waterfall via
handle_enrich_phone, and sets each task's final status.

Single-threaded by design: Task.objects.next_enrichment is a plain ordered
read, not a locking claim — a second worker would double-process tasks (and
double-bill providers). Do not scale this without select_for_update.

Crash recovery: the daemon has no clean SIGTERM shutdown, so a killed worker
leaves its task RUNNING. `start()` reclaims stale RUNNING enrich_phone tasks
back to PENDING — that, plus the persisted bettercontact_request_id, is the
real crash-safety net.
"""
from __future__ import annotations

import logging
import threading
import traceback

logger = logging.getLogger(__name__)

from linkedin.notifications.slack import notify_error
from linkedin.tasks.enrich_phone import handle_enrich_phone


class EnrichmentWorker:
    def __init__(self, poll_interval: float = 10.0):
        self._poll_interval = poll_interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Reclaim stale tasks, then spawn the worker thread. Idempotent."""
        if self._thread is not None:
            return
        self._reclaim_stale()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="enrichment-worker", daemon=True,
        )
        self._thread.start()
        logger.info("Enrichment worker started")

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the loop to exit and join the thread. Idempotent, never raises."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
            logger.info("Enrichment worker stopped")

    def _reclaim_stale(self) -> None:
        from linkedin.models import Task

        reclaimed = Task.objects.filter(
            task_type=Task.TaskType.ENRICH_PHONE,
            status=Task.Status.RUNNING,
        ).update(status=Task.Status.PENDING)
        if reclaimed:
            logger.info(
                "Enrichment worker reclaimed %d stale running task(s)", reclaimed,
            )

    def _run(self) -> None:
        from django.db import connection

        while not self._stop.is_set():
            # Connections are thread-local. close() is thread-scoped (unlike
            # connections.close_all(), which would also close the daemon main
            # thread's connection). Recycle so a Neon idle-timeout drop is
            # never reused.
            connection.close()
            handled = self._run_once()
            if not handled:
                self._stop.wait(self._poll_interval)

    def _run_once(self) -> bool:
        """Claim and process one enrichment task. Returns True if one ran.

        Pure DB + HTTP — safe to call directly from tests (no thread, no
        connection recycling)."""
        from linkedin.enrichment.base import EnrichmentStatus
        from linkedin.models import Task

        task = Task.objects.next_enrichment()
        if task is None:
            return False

        task.mark_running()
        try:
            result = handle_enrich_phone(task)
        except Exception as exc:
            logger.exception("enrich_phone task %s failed", task.id)
            task.mark_failed(traceback.format_exc())
            notify_error(
                "daemon:enrich_phone", exc,
                context={"task_id": task.id, "payload": task.payload},
            )
            return True

        if result is not None and result.status == EnrichmentStatus.API_FAILURE:
            task.mark_failed(
                f"All enrichment providers failed (last={result.provider})",
            )
        else:
            task.mark_completed()
        return True
