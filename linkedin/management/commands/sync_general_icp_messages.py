"""Import the wide General ICP Messages Sheet into checked-in JSON stores."""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from linkedin.exceptions import GeneralICPMessageError, SheetsError


class Command(BaseCommand):
    help = (
        "Validate General ICP Messages and preview its JSON import; "
        "--apply replaces shared_programs in linkedin/icp_messages.json and "
        "gmail/icp_emails.json."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Write the validated Sheet copy to both checked-in JSON stores.",
        )

    def handle(self, *args, **options):
        from linkedin.general_icp_json import (
            render_general_icp_stores,
            write_general_icp_stores,
        )
        from linkedin.general_icp_messages import parse_general_icp_message_rows
        from linkedin.general_icp_messages_sheet import read_general_icp_messages_tab

        apply = bool(options["apply"])
        try:
            drafts = parse_general_icp_message_rows(read_general_icp_messages_tab())
            linkedin_store, gmail_store = render_general_icp_stores(drafts)
            if apply:
                write_general_icp_stores(linkedin_store, gmail_store)
        except (GeneralICPMessageError, SheetsError) as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(json.dumps({
            "status": "applied" if apply else "preview",
            "program_count": len(drafts),
            "icp_count": len({
                (draft.key, message.audience_key)
                for draft in drafts
                for message in draft.messages
            }),
            "linkedin_message_count": sum(
                message.channel != "gmail"
                for draft in drafts
                for message in draft.messages
            ),
            "gmail_message_count": sum(
                message.channel == "gmail"
                for draft in drafts
                for message in draft.messages
            ),
            "content_hashes": {
                draft.key: draft.content_hash for draft in drafts
            },
        }, indent=2, sort_keys=True))
        if not apply:
            self.stdout.write(
                "No files changed. Re-run with --apply after the Sheet rows are reviewed."
            )
