"""EnrichmentWorker — the daemon's HTTP-only enrichment task loop.

Each sender daemon starts one background thread. Email work belongs to that
exact canonical operator; the legacy phone queue remains shared. A row-locking
claim makes concurrent workers safe without changing Task or provider identity.

Startup only reclaims age-qualified RUNNING work in the same scope. Fresh work
and another sender's email stay untouched; recovery retains BetterContact's
persisted request id rather than purchasing a new lookup.
"""
from __future__ import annotations

import json
import logging
import threading
import traceback
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger(__name__)

from linkedin.notifications.slack import notify_error
from linkedin.notifications.slack import notify_degraded
from linkedin.monitoring.degraded import _should_alert
from gmail.tasks.enrich_email import handle_enrich_email
from linkedin.tasks.enrich_phone import handle_enrich_phone
from linkedin.conf import TASK_RUNNING_STALE_MINUTES
from linkedin.operators import CANONICAL_OPERATOR_HANDLES


def _api_failure_detail(result) -> dict:
    return {
        "provider": result.provider,
        "status": result.status.value,
        "raw": result.raw,
    }


def _format_api_failure(result) -> str:
    detail = _api_failure_detail(result)
    return "Enrichment provider API failure: " + json.dumps(
        detail,
        ensure_ascii=True,
        default=str,
        sort_keys=True,
    )


def _notify_api_failure(*, task, result) -> None:
    raw = result.raw or {}
    reason = str(raw.get("reason") or "unknown")
    status = raw.get("status")
    key = f"enrichment_api_failure:{result.provider}:{reason}:{status}"
    if not _should_alert(key):
        return

    operator = (task.payload or {}).get("operator") or "enrichment-worker"
    detail = _format_api_failure(result)
    if len(detail) > 2500:
        detail = detail[:2497] + "..."
    notify_degraded(
        sender=operator,
        title=f"Enrichment provider failure: {result.provider}",
        detail=(
            f"*Task:* `{task.id}` `{task.task_type}`\n"
            f"*Lead:* `{(task.payload or {}).get('lead_id')}`\n"
            f"*Reason:* `{reason}`\n"
            f"*HTTP status:* `{status}`\n"
            f"```{detail}```"
        ),
    )


class EnrichmentWorker:
    def __init__(self, *, operator: str, poll_interval: float = 10.0):
        if not isinstance(operator, str) or operator not in CANONICAL_OPERATOR_HANDLES:
            raise ValueError("EnrichmentWorker requires a canonical operator")
        self.operator = operator
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
            target=self._run, name=f"enrichment-worker-{self.operator}", daemon=True,
        )
        self._thread.start()
        logger.info("Enrichment worker started for %s", self.operator)

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the loop to exit and join the thread. Idempotent, never raises."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
            logger.info("Enrichment worker stopped for %s", self.operator)

    def _owned_tasks(self):
        from linkedin.models import Task

        return Task.objects.filter(
            Q(task_type=Task.TaskType.ENRICH_PHONE)
            | Q(task_type=Task.TaskType.ENRICH_EMAIL, payload__operator=self.operator),
        )

    def _reclaim_stale(self) -> None:
        from linkedin.models import Task

        stale_before = timezone.now() - timedelta(minutes=TASK_RUNNING_STALE_MINUTES)
        reclaimed = self._owned_tasks().filter(status=Task.Status.RUNNING).filter(
            Q(started_at__lt=stale_before)
            | Q(started_at__isnull=True, created_at__lt=stale_before),
        ).update(status=Task.Status.PENDING, started_at=None)
        if reclaimed:
            logger.info(
                "Enrichment worker reclaimed %d stale running task(s) for %s",
                reclaimed, self.operator,
            )

    @transaction.atomic
    def _claim_next(self):
        """Atomically claim one due owned email or shared legacy phone Task."""
        from linkedin.models import Task

        task = (
            self._owned_tasks()
            .filter(status=Task.Status.PENDING, scheduled_at__lte=timezone.now())
            .select_for_update(skip_locked=True)
            .order_by("scheduled_at", "pk")
            .first()
        )
        if task is not None:
            task.mark_running()
        return task

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

        task = self._claim_next()
        if task is None:
            return False

        try:
            if task.task_type == Task.TaskType.ENRICH_EMAIL:
                result = handle_enrich_email(task)
            else:
                result = handle_enrich_phone(task)
        except Exception as exc:
            logger.exception("%s task %s failed", task.task_type, task.id)
            task.mark_failed(traceback.format_exc())
            notify_error(
                f"daemon:{task.task_type}", exc,
                context={"task_id": task.id, "payload": task.payload},
            )
            return True

        if result is not None and result.status == EnrichmentStatus.API_FAILURE:
            task.mark_failed(_format_api_failure(result))
            _notify_api_failure(task=task, result=result)
        else:
            task.mark_completed()
        return True
