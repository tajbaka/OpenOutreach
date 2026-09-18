"""Run one independent, account-scoped Gmail Task worker."""
from __future__ import annotations

import logging

from django.core.management.base import BaseCommand

from gmail.auth import GMAIL_ACCOUNTS, GMAIL_DATA_DIR
from gmail.worker import GmailWorker
from gmail.worker_logging import log_worker_event, worker_log
from linkedin.single_instance import SingleInstanceGuard

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Run Gmail Tasks for one configured OAuth mailbox and all of its aliases."

    def add_arguments(self, parser):
        parser.add_argument(
            "--account",
            required=True,
            choices=tuple(sorted(GMAIL_ACCOUNTS)),
            help="Configured Gmail OAuth account key.",
        )

    def handle(self, *args, **options):
        account_key = options["account"]
        with worker_log(account_key):
            log_worker_event(account_key, "worker_starting")
            self._run_worker(account_key)

    def _run_worker(self, account_key):
        marker = f"manage.py run_gmail_worker --account {account_key}"
        worker = None
        context = {"stage": "startup"}
        try:
            guard = SingleInstanceGuard(
                pidfile=GMAIL_DATA_DIR / f"run-gmail-worker-{account_key}.pid",
                marker=marker,
                logger=logger,
            )
            worker = GmailWorker(account_key=account_key)
            context = {"stage": "instance_guard"}
            guard.acquire()
            try:
                context = None
                worker.run_forever()
            finally:
                try:
                    worker.stop()
                finally:
                    guard.release()
        except KeyboardInterrupt:
            logger.info("Gmail worker interrupted (account=%s)", account_key)
            log_worker_event(account_key, "worker_interrupted")
        except Exception as exc:
            log_worker_event(
                account_key, "worker_crashed", exception=exc,
                **(context if context is not None else worker.diagnostic_context),
            )
            raise
        else:
            log_worker_event(account_key, "worker_stopped")
