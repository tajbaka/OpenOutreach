"""Repair only uniquely re-identifiable synthetic Gmail-note meetings."""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from gmail.meeting_identity_repair import repair_gmail_note_meeting_identities


class Command(BaseCommand):
    help = (
        "Plan or apply conservative Gmail-note meeting identity repairs. "
        "Defaults to rollback dry-run and emits aggregate-only output."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")
        parser.add_argument(
            "--meeting-id",
            action="append",
            type=int,
            default=[],
            help="Restrict to one stable Meeting ID; repeatable.",
        )

    def handle(self, *args, **options):
        if any(value <= 0 for value in options["meeting_id"]):
            raise CommandError("--meeting-id must be positive")
        report = repair_gmail_note_meeting_identities(
            apply=bool(options["apply"]),
            meeting_ids=options["meeting_id"],
        )
        self.stdout.write(json.dumps(report.counts(), sort_keys=True))
