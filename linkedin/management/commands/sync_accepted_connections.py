"""Preview or publish the accepted/no-reply tab without refreshing other CRM tabs."""
import json

from django.core.management.base import BaseCommand, CommandError

from linkedin.crm_lock import CrmRefreshAlreadyRunning, crm_refresh_lock
from linkedin.exceptions import SheetsError
from linkedin.notifications.accepted_connections_sheet import sync_accepted_connections


class Command(BaseCommand):
    help = "Preview accepted connections awaiting a reply; --apply publishes only that tab."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        try:
            with crm_refresh_lock():
                result = sync_accepted_connections(dry_run=not options["apply"])
        except (SheetsError, CrmRefreshAlreadyRunning) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(result, sort_keys=True))
