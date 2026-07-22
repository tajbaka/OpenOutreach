"""Prepare/apply Codex decisions for new FedRAMP marketplace signals."""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone


class Command(BaseCommand):
    help = (
        "Export new FedRAMP marketplace signals for Codex review, or apply a "
        "Codex-produced decision JSON file and post high-signal Slack alerts."
    )

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, help="Optional export cap.")
        parser.add_argument(
            "--since-days",
            type=int,
            help="Only export signals first seen in the last N days.",
        )
        parser.add_argument("--reanalyze", action="store_true", help="Include already analyzed signals.")
        parser.add_argument("--output", help="Write Codex review queue JSON to this path.")
        parser.add_argument("--apply-json", help="Apply Codex decision JSON from this path.")
        parser.add_argument("--no-slack", action="store_true", help="Save decisions without Slack alerts.")

    def handle(self, *args, **options):
        from linkedin.marketplace_analysis import (
            group_marketplace_signals_for_alert,
            load_decisions,
            mark_marketplace_signals_slack_notified,
            save_marketplace_analysis,
            serialize_signals_for_codex,
            should_notify_marketplace_signal,
        )
        from linkedin.models import FedRAMPMarketplaceSignal
        from linkedin.notifications.slack import notify_marketplace_signal_group

        if options.get("apply_json"):
            try:
                decisions = load_decisions(options["apply_json"])
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                raise CommandError(str(exc)) from exc
            applied = 0
            alert_signals = []
            for signal_id, result in decisions:
                try:
                    signal = FedRAMPMarketplaceSignal.objects.get(pk=signal_id)
                except FedRAMPMarketplaceSignal.DoesNotExist as exc:
                    raise CommandError(f"Marketplace signal {signal_id} does not exist.") from exc
                save_marketplace_analysis(signal, result)
                applied += 1
                if should_notify_marketplace_signal(signal):
                    alert_signals.append(signal)

            alert_groups_sent = 0
            if not options.get("no_slack"):
                for group in group_marketplace_signals_for_alert(alert_signals):
                    if notify_marketplace_signal_group(signals=group):
                        mark_marketplace_signals_slack_notified(group)
                        alert_groups_sent += 1
            self.stdout.write(
                self.style.SUCCESS(
                    f"Applied {applied} Codex marketplace decision(s); "
                    f"Slack alert groups sent: {alert_groups_sent}."
                )
            )
            return

        limit = options.get("limit")
        since_days = options.get("since_days")
        if limit is not None and limit <= 0:
            raise CommandError("--limit must be positive.")
        if since_days is not None and since_days <= 0:
            raise CommandError("--since-days must be positive.")

        queryset = FedRAMPMarketplaceSignal.objects.order_by("recorded_at", "id")
        if not options.get("reanalyze"):
            queryset = queryset.filter(analyzed_at__isnull=True)
        if since_days is not None:
            queryset = queryset.filter(
                first_seen_at__gte=timezone.now() - timedelta(days=since_days)
            )
        signals = list(queryset[:limit]) if limit is not None else list(queryset)
        payload = serialize_signals_for_codex(signals)
        encoded = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
        if options.get("output"):
            path = Path(options["output"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(encoded, encoding="utf-8")
            self.stdout.write(
                f"Wrote {len(signals)} marketplace signal(s) to {path}"
            )
        else:
            self.stdout.write(encoded)
