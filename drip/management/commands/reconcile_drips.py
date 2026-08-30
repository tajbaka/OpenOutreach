import json

from django.core.management.base import BaseCommand, CommandError

from drip.exceptions import ReconciliationBusy
from drip.services.reconciliation import reconcile_drips


class Command(BaseCommand):
    help = "Preview or apply one finite database-only drip reconciliation pass."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Persist handoffs, progress, and due Tasks. Default is no-write dry run.",
        )
        parser.add_argument(
            "--campaign",
            default="",
            help="Optional exact drip campaign key for a bounded pass.",
        )

    def handle(self, *args, **options):
        try:
            result = reconcile_drips(
                apply=options["apply"],
                campaign_key=options["campaign"],
            )
        except ReconciliationBusy as exc:
            raise CommandError(str(exc)) from exc

        payload = {
            "mode": "apply" if result.applied else "dry_run",
            "counts": result.counts,
            "workflow_run_id": result.workflow_run_id,
            "decisions": [decision.as_dict() for decision in result.decisions],
        }
        self.stdout.write(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))
