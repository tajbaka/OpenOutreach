"""Export/apply Codex-drafted followup sheet rows."""
from __future__ import annotations

import json
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = (
        "Export followup candidates for Codex drafting, or apply a "
        "Codex-produced followup rows JSON file to the operator Followups tabs. "
        "This command does not call an LLM."
    )

    def add_arguments(self, parser):
        parser.add_argument("--operator", action="append", help="Operator to include. Repeatable.")
        parser.add_argument("--campaign", type=int, help="Restrict candidates to one Campaign id.")
        parser.add_argument("--limit", type=int, help="Optional cap for manual/debug exports.")
        parser.add_argument(
            "--no-active",
            action="store_true",
            help="Exclude active-in-flight visibility rows from export.",
        )
        parser.add_argument(
            "--no-sheet-read",
            action="store_true",
            help="Do not read People/Followups/ICP Goals tabs during export.",
        )
        parser.add_argument(
            "--sync-gmail-context",
            action="store_true",
            help="Run sync_gmail_context before exporting candidates.",
        )
        parser.add_argument(
            "--sync-sheets",
            action="store_true",
            help="Run sync_sheets before exporting candidates.",
        )
        parser.add_argument("--output", help="Write Codex review queue JSON to this path.")
        parser.add_argument("--apply-json", help="Apply Codex followup rows JSON from this path.")
        parser.add_argument(
            "--no-record-workflow",
            action="store_true",
            help="Do not write WorkflowRun(name='followup') after apply.",
        )

    def handle(self, *args, **opts):
        from linkedin.followup_analysis import (
            apply_followup_decisions,
            load_followup_decisions,
            serialize_followup_queue,
            write_review_queue,
        )

        limit = opts.get("limit")
        if limit is not None and limit <= 0:
            raise CommandError("--limit must be positive.")

        if opts.get("apply_json"):
            decisions = load_followup_decisions(opts["apply_json"])
            counts = apply_followup_decisions(
                decisions,
                record_workflow=not opts["no_record_workflow"],
            )
            total = sum(counts.values())
            self.stdout.write(
                self.style.SUCCESS(
                    f"Wrote {total} followup row(s) across "
                    f"{len(counts)} operator tab(s): {counts}"
                )
            )
            return

        if opts.get("sync_gmail_context"):
            call_command("sync_gmail_context")
        if opts.get("sync_sheets"):
            call_command("sync_sheets")

        payload = serialize_followup_queue(
            operators=opts.get("operator") or None,
            campaign_id=opts.get("campaign"),
            limit=limit,
            include_active=not opts["no_active"],
            read_sheet=not opts["no_sheet_read"],
        )
        encoded = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
        if opts.get("output"):
            path = Path(opts["output"])
            write_review_queue(path, payload)
            self.stdout.write(
                f"Wrote Codex followup queue to {path} "
                f"({len(payload['candidates'])} candidates)."
            )
        else:
            self.stdout.write(encoded)
