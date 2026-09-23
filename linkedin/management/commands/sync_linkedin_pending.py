"""Preview or publish the incremental LinkedIn Pending Sheet view."""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from linkedin.crm_lock import CrmRefreshAlreadyRunning, crm_refresh_lock
from linkedin.exceptions import SheetsError
from linkedin.notifications.linkedin_pending_sheet import sync_linkedin_pending


class Command(BaseCommand):
    help = (
        "Incrementally reconcile LinkedIn messages from the last 14 days "
        "that still require a human reply."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        try:
            with crm_refresh_lock():
                report = sync_linkedin_pending(dry_run=not options["apply"])
        except (CrmRefreshAlreadyRunning, SheetsError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(report, sort_keys=True))
