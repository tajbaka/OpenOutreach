"""Run sender-scoped email enrichment without a LinkedIn browser."""
from __future__ import annotations

import logging

from django.core.management.base import BaseCommand

from linkedin.conf import ROOT_DIR
from linkedin.enrichment.worker import EnrichmentWorker
from linkedin.operators import CANONICAL_OPERATOR_HANDLES
from linkedin.single_instance import SingleInstanceGuard

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Run only email-enrichment Tasks for one canonical operator, without LinkedIn."

    def add_arguments(self, parser):
        parser.add_argument("--operator", required=True, choices=tuple(sorted(CANONICAL_OPERATOR_HANDLES)))

    def handle(self, *args, **options):
        operator = options["operator"]
        guard = SingleInstanceGuard(
            pidfile=ROOT_DIR / "data" / f"run-email-enrichment-{operator.lower()}.pid",
            marker=f"manage.py run_email_enrichment --operator {operator}",
            logger=logger,
        )
        worker = EnrichmentWorker(operator=operator, include_phone=False)
        guard.acquire()
        try:
            worker.run_forever()
        except KeyboardInterrupt:
            logger.info("Email enrichment interrupted (operator=%s)", operator)
        finally:
            worker.stop()
            guard.release()
