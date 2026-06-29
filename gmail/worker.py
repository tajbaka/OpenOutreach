"""GmailWorker - browserless Gmail sequence task loop."""
from __future__ import annotations

import logging
import threading
import traceback

logger = logging.getLogger(__name__)

from linkedin.notifications.slack import notify_error
from gmail.tasks.follow_up import handle_gmail_follow_up


class GmailWorker:
    def __init__(self, *, operator: str = "", poll_interval: float = 10.0):
        self._operator = operator
        self._poll_interval = poll_interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._reclaim_stale()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="gmail-worker",
            daemon=True,
        )
        self._thread.start()
        logger.info("Gmail worker started (operator=%s)", self._operator or "unscoped")

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
            logger.info("Gmail worker stopped")

    def _reclaim_stale(self) -> None:
        from linkedin.models import Task

        reclaimed = Task.objects.filter(
            task_type=Task.TaskType.GMAIL_FOLLOW_UP,
            status=Task.Status.RUNNING,
        ).update(status=Task.Status.PENDING)
        if reclaimed:
            logger.info("Gmail worker reclaimed %d stale running task(s)", reclaimed)

    def _run(self) -> None:
        from django.db import connection

        while not self._stop.is_set():
            connection.close()
            handled = self._run_once()
            if not handled:
                self._stop.wait(self._poll_interval)

    def _run_once(self) -> bool:
        from linkedin.models import Task

        task = Task.objects.next_gmail(operator=self._operator)
        if task is None:
            return False

        task.mark_running()
        try:
            handle_gmail_follow_up(task)
        except Exception as exc:
            logger.exception("gmail_follow_up task %s failed", task.id)
            task.mark_failed(traceback.format_exc())
            notify_error(
                "daemon:gmail_follow_up",
                exc,
                context={"task_id": task.id, "payload": task.payload},
            )
            return True

        task.mark_completed()
        return True
