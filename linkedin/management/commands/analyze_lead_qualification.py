"""Prepare/apply Codex review decisions for lead qualification candidates."""
from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = (
        "Export leads awaiting qualification for Codex review, or apply a "
        "Codex-produced lead decision JSON file. This command does not call an LLM."
    )

    def add_arguments(self, parser):
        parser.add_argument("--campaign", type=int, help="Restrict export/apply to one Campaign id.")
        parser.add_argument("--limit", type=int, help="Optional cap for manual/debug exports.")
        parser.add_argument(
            "--all-campaigns",
            action="store_true",
            help="Export all campaigns instead of only active campaigns.",
        )
        parser.add_argument(
            "--ready",
            action="store_true",
            help="Apply qualified decisions as READY_TO_CONNECT instead of QUALIFIED.",
        )
        parser.add_argument("--output", help="Write review queue JSON to this path instead of stdout.")
        parser.add_argument("--apply-json", help="Apply Codex decision JSON from this path.")

    def handle(self, *args, **opts):
        from linkedin.enums import ProfileState
        from linkedin.lead_analysis import (
            apply_decision,
            load_decisions,
            qualification_rows,
            serialize_leads_for_codex,
        )

        campaign_id = opts.get("campaign")
        limit = opts.get("limit")
        if limit is not None and limit <= 0:
            raise CommandError("--limit must be positive.")

        if opts.get("apply_json"):
            positive_state = (
                ProfileState.READY_TO_CONNECT if opts.get("ready") else ProfileState.QUALIFIED
            )
            decisions = load_decisions(opts["apply_json"])
            applied = [
                apply_decision(
                    decision,
                    default_campaign_id=campaign_id,
                    positive_state=positive_state,
                )
                for decision in decisions
            ]

            qualified = sum(1 for row in applied if row.qualified)
            rejected = len(applied) - qualified
            self.stdout.write(
                self.style.SUCCESS(
                    f"Applied {len(applied)} Codex lead decisions "
                    f"({qualified} qualified, {rejected} rejected).",
                ),
            )
            return

        rows = qualification_rows(
            campaign_id=campaign_id,
            active_only=not opts.get("all_campaigns"),
            limit=limit,
        )
        payload = serialize_leads_for_codex(rows)
        encoded = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
        if opts.get("output"):
            path = Path(opts["output"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(encoded, encoding="utf-8")
            self.stdout.write(f"Wrote Codex lead review queue to {path}")
        else:
            self.stdout.write(encoded)
