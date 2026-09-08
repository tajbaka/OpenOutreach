"""Render sender role-persona tabs into general wide staging rows."""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from linkedin.exceptions import GeneralICPMessageError, SheetsError


class Command(BaseCommand):
    help = (
        "Render existing sender role-persona rows into General ICP Messages format. "
        "Default is read-only; --apply stages a new/empty general tab and never "
        "changes sender tabs."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--sender",
            action="append",
            default=[],
            help="Sender tab to read; repeatable. Defaults to Arian and Chuka.",
        )
        parser.add_argument(
            "--program-key",
            default="fedramp-marketplace-csp",
        )
        parser.add_argument(
            "--program-name",
            default="FedRAMP Marketplace CSP Outreach",
        )
        parser.add_argument(
            "--output",
            default="",
            help="Optional local CSV path for the rendered rows.",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Stage the rows into a missing or completely empty general tab.",
        )

    def handle(self, *args, **options):
        from linkedin.general_icp_message_migration import (
            render_role_persona_migration_rows,
        )
        from linkedin.general_icp_messages_sheet import stage_general_icp_messages_tab
        from linkedin.notifications.sheets import read_icp_messages_tab
        from linkedin.operators import resolve_operator

        senders = []
        for raw_sender in options["sender"] or ("Arian", "Chuka"):
            sender = resolve_operator(raw_sender)
            if not sender:
                raise CommandError("--sender must not be blank")
            if sender not in senders:
                senders.append(sender)
        try:
            source_rows = {
                sender: read_icp_messages_tab(sender)
                for sender in senders
            }
            rows = render_role_persona_migration_rows(
                source_rows,
                program_key=str(options["program_key"] or "").strip(),
                program_name=str(options["program_name"] or "").strip(),
            )
            if options["apply"]:
                stage_general_icp_messages_tab(rows)
        except (GeneralICPMessageError, SheetsError) as exc:
            raise CommandError(str(exc)) from exc

        output = str(options["output"] or "").strip()
        if output:
            path = Path(output)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", newline="", encoding="utf-8") as file_handle:
                csv.writer(file_handle).writerows(rows)
        summary = {
            "status": "staged" if options["apply"] else "preview",
            "source_tabs": [f"{sender} ICP Messages" for sender in senders],
            "rendered_rows": len(rows) - 1,
            "output": output,
        }
        self.stdout.write(json.dumps(summary, sort_keys=True))
        if not options["apply"] and not output:
            buffer = io.StringIO()
            csv.writer(buffer).writerows(rows)
            self.stdout.write(buffer.getvalue().rstrip("\r\n"))
