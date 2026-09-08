"""Preview or publish immutable versions from checked-in ICP JSON."""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from linkedin.exceptions import GeneralICPMessageError, SheetsError


class Command(BaseCommand):
    help = (
        "Validate checked-in shared ICP JSON and preview its immutable version diff; "
        "--apply publishes new or deduplicated MessageProgramVersion rows."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Apply the validated diff. Default is read-only preview.",
        )
        parser.add_argument(
            "--published-by",
            default="",
            help="Required reviewer identity for --apply.",
        )

    def handle(self, *args, **options):
        from linkedin.general_icp_json import load_general_message_programs
        from linkedin.general_icp_messages import (
            plan_message_program_publication,
            publish_message_programs,
        )

        apply = bool(options["apply"])
        published_by = str(options["published_by"] or "").strip()
        if apply and not published_by:
            raise CommandError("--published-by is required with --apply")
        try:
            drafts = load_general_message_programs()
            decisions = (
                publish_message_programs(drafts, published_by=published_by)
                if apply
                else plan_message_program_publication(drafts)
            )
        except (GeneralICPMessageError, SheetsError) as exc:
            raise CommandError(str(exc)) from exc

        payload = {
            "status": "applied" if apply else "preview",
            "message_count": sum(len(draft.messages) for draft in drafts),
            "program_count": len(drafts),
            "changes": [decision.as_dict() for decision in decisions],
        }
        self.stdout.write(json.dumps(payload, indent=2, sort_keys=True))
        if not apply:
            self.stdout.write(
                "No database changes made. Re-run with --apply --published-by <reviewer> "
                "to publish this exact checked-in JSON snapshot."
            )
