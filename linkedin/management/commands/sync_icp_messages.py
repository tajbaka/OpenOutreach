"""Round-trip rigid ICP outbound templates between JSON and Google Sheets.

Usage:
    python manage.py sync_icp_messages --sender Leili --push
    python manage.py sync_icp_messages --sender Leili --pull
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Push/pull one sender's rigid ICP messages between JSON and Google Sheets."

    def add_arguments(self, parser):
        parser.add_argument("--sender", required=True,
                            help="Canonical sender handle (e.g. Arian, Chuka, Leili).")
        parser.add_argument("--push", action="store_true",
                            help="Write the sender's current JSON block to Sheets.")
        parser.add_argument("--pull", action="store_true",
                            help="Read the sender's worksheet from Sheets back into JSON.")

    def handle(self, *args, **options):
        from linkedin.exceptions import SheetsError
        from linkedin.icp_outbound import (
            icp_messages_rows,
            parse_icp_messages_rows,
            save_icp_messages,
        )
        from linkedin.notifications.sheets import (
            icp_messages_tab_name,
            read_icp_messages_tab,
            write_icp_messages_tab,
        )

        sender = options["sender"].strip()
        push = bool(options["push"])
        pull = bool(options["pull"])
        if push == pull:
            raise CommandError("Choose exactly one of --push or --pull.")

        try:
            if push:
                rows = icp_messages_rows(sender)
                write_icp_messages_tab(sender, rows)
                self.stdout.write(self.style.SUCCESS(
                    f"Wrote {len(rows) - 1} template row(s) to {icp_messages_tab_name(sender)}."
                ))
                return

            rows = read_icp_messages_tab(sender)
            block = parse_icp_messages_rows(rows)
            save_icp_messages(sender, block)
            self.stdout.write(self.style.SUCCESS(
                f"Imported {sum(len(v) for channels in block.values() for v in channels.values())} "
                f"variant(s) from {icp_messages_tab_name(sender)} into linkedin/icp_messages.json."
            ))
        except SheetsError as e:
            raise CommandError(str(e)) from e
