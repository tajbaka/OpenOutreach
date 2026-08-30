"""Run one independent, account-scoped Gmail Task worker."""
from __future__ import annotations

import logging

from django.core.management.base import BaseCommand

from gmail.auth import GMAIL_ACCOUNTS, GMAIL_DATA_DIR
from gmail.worker import GmailWorker
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
        marker = f"manage.py run_gmail_worker --account {account_key}"
        guard = SingleInstanceGuard(
            pidfile=GMAIL_DATA_DIR / f"run-gmail-worker-{account_key}.pid",
            marker=marker,
            logger=logger,
        )
        worker = GmailWorker(account_key=account_key)
        guard.acquire()
        try:
            worker.run_forever()
        except KeyboardInterrupt:
            logger.info("Gmail worker interrupted (account=%s)", account_key)
        finally:
            worker.stop()
            guard.release()
