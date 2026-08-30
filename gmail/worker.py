"""Account-scoped browserless Gmail task worker."""
from __future__ import annotations

import logging
import threading
import traceback
from datetime import timedelta

from django.db import connection
from django.db.models import Q
from django.utils import timezone

from drip.tasks.gmail import handle_drip_gmail, recover_stale_drip_gmail_task
from gmail.auth import account_for_key, operators_for_account
from gmail.submission import (
    recover_stale_current_gmail_task,
    reschedule_persisted_current_gmail_task,
)
from gmail.tasks.follow_up import handle_gmail_follow_up
from linkedin.conf import TASK_RUNNING_STALE_MINUTES
from linkedin.notifications.slack import notify_error

logger = logging.getLogger(__name__)


class GmailWorker:
    """Consume Gmail Tasks belonging to one resolved OAuth mailbox.

    Operators remain the durable Task payload identity, while OAuth ownership
    is account-scoped. One worker therefore claims work for every configured
    operator/Send-As alias routed through its account, and no others.
    """

    def __init__(self, *, account_key: str, poll_interval: float = 10.0):
        account_for_key(account_key)
        self.account_key = account_key
        self._operators = operators_for_account(account_key)
        self._poll_interval = poll_interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start in a background thread for bounded/test supervisors."""
        if self._thread is not None:
            return
        self._prepare_run()
        self._thread = threading.Thread(
            target=self._run,
            name=f"gmail-worker-{self.account_key}",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "Gmail worker started (account=%s operators=%s)",
            self.account_key,
            ",".join(self._operators),
        )

    def run_forever(self) -> None:
        """Run synchronously until interrupted or :meth:`stop` is called."""
        self._prepare_run()
        logger.info(
            "Gmail worker running (account=%s operators=%s)",
            self.account_key,
            ",".join(self._operators),
        )
        self._run()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        logger.info("Gmail worker stopped (account=%s)", self.account_key)

    def _prepare_run(self) -> None:
        self._reclaim_stale()
        self._stop.clear()

    def _gmail_task_types(self) -> tuple[str, ...]:
        from linkedin.models import Task

        return (
            Task.TaskType.GMAIL_FOLLOW_UP,
            Task.TaskType.DRIP_GMAIL,
        )

    def _owned_tasks(self):
        return self._task_model().objects.filter(
            task_type__in=self._gmail_task_types(),
            payload__operator__in=self._operators,
        )

    @staticmethod
    def _task_model():
        from linkedin.models import Task

        return Task

    def _reclaim_stale(self) -> None:
        Task = self._task_model()
        stale_before = timezone.now() - timedelta(
            minutes=TASK_RUNNING_STALE_MINUTES,
        )
        stale_scope = (
            self._owned_tasks()
            .filter(status=Task.Status.RUNNING)
            .filter(Q(started_at__lt=stale_before) | Q(started_at__isnull=True))
        )
        drip_task_ids = list(
            stale_scope.filter(task_type=Task.TaskType.DRIP_GMAIL).values_list(
                "pk",
                flat=True,
            ),
        )
        drip_recovered = sum(
            recover_stale_drip_gmail_task(task_id)
            for task_id in drip_task_ids
        )
        current_task_ids = list(
            stale_scope.filter(task_type=Task.TaskType.GMAIL_FOLLOW_UP).values_list(
                "pk",
                flat=True,
            ),
        )
        current_reclaimed = sum(
            recover_stale_current_gmail_task(task_id)
            for task_id in current_task_ids
        )
        reclaimed = drip_recovered + current_reclaimed
        if reclaimed:
            logger.info(
                "Gmail worker reclaimed %d stale running task(s) for %s",
                reclaimed,
                self.account_key,
            )

    def _claim_next(self):
        """Atomically transition the next owned due Task to running."""
        Task = self._task_model()
        while True:
            candidate = (
                self._owned_tasks()
                .filter(
                    status=Task.Status.PENDING,
                    scheduled_at__lte=timezone.now(),
                )
                .order_by("scheduled_at", "pk")
                .first()
            )
            if candidate is None:
                return None
            started_at = timezone.now()
            updated = Task.objects.filter(
                pk=candidate.pk,
                status=Task.Status.PENDING,
            ).update(
                status=Task.Status.RUNNING,
                started_at=started_at,
            )
            if updated:
                candidate.status = Task.Status.RUNNING
                candidate.started_at = started_at
                return candidate
            # Another process won the guarded transition. Re-read the queue.

    def _run(self) -> None:
        while not self._stop.is_set():
            connection.close()
            handled = self._run_once()
            if not handled:
                self._stop.wait(self._poll_interval)

    def _run_once(self) -> bool:
        task = self._claim_next()
        if task is None:
            return False

        handlers = {
            self._task_model().TaskType.GMAIL_FOLLOW_UP: handle_gmail_follow_up,
            self._task_model().TaskType.DRIP_GMAIL: handle_drip_gmail,
        }
        handler = handlers.get(task.task_type)
        if handler is None:
            # `_gmail_task_types` and this dispatch table must move together.
            raise RuntimeError(f"No Gmail handler registered for {task.task_type!r}")

        try:
            handler(task)
        except Exception as exc:
            logger.exception("%s task %s failed", task.task_type, task.id)
            rescheduled = (
                task.task_type == self._task_model().TaskType.GMAIL_FOLLOW_UP
                and reschedule_persisted_current_gmail_task(task.id)
            )
            if not rescheduled:
                task.mark_failed(traceback.format_exc())
            notify_error(
                f"gmail-worker:{task.task_type}",
                exc,
                context={
                    "account": self.account_key,
                    "task_id": task.id,
                    "payload": task.payload,
                },
            )
            return True

        task.mark_completed()
        return True
