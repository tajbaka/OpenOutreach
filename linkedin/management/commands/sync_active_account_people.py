"""Publish People from the manual Active Accounts company scope."""
from __future__ import annotations

import json
from contextlib import nullcontext

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Rebuild People from exact companies listed in Active Accounts."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Discard all existing People rows instead of preserving manual cells.",
        )
        parser.add_argument(
            "--backup-dir",
            default="artifacts/crm-backups",
        )

    def handle(self, *args, **options):
        from linkedin.crm_lock import CrmRefreshAlreadyRunning, crm_refresh_lock
        from linkedin.exceptions import SheetsError
        from linkedin.notifications.active_account_people import (
            sync_people_from_active_accounts,
        )

        lock_held = bool(options.pop("_crm_refresh_lock_held", False))
        lock = nullcontext() if lock_held else crm_refresh_lock()
        try:
            with lock:
                report = sync_people_from_active_accounts(
                    dry_run=not options["apply"],
                    reset=options["reset"],
                    backup_dir=options["backup_dir"],
                )
        except (CrmRefreshAlreadyRunning, SheetsError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(report, sort_keys=True))
