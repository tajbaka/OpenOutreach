"""Collect and diff official FedRAMP marketplace JSON sources."""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = (
        "Diff official FedRAMP marketplace JSON feeds and persist new Rev5 Ready "
        "or 20x Initial Implementation signals for Codex review."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--lookback-days",
            type=int,
            help=(
                "On the first run only, create signals recorded within the last N days. "
                "Without this option the first run establishes a no-alert baseline."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Fetch and calculate differences without changing the database.",
        )
        parser.add_argument("--changelog-url", help="Override the changelog JSON URL.")
        parser.add_argument("--data-url", help="Override the marketplace snapshot JSON URL.")
        parser.add_argument("--timeout", type=int, help="Per-source HTTP timeout in seconds.")

    def handle(self, *args, **options):
        from linkedin.conf import (
            FEDRAMP_MARKETPLACE_CHANGELOG_URL,
            FEDRAMP_MARKETPLACE_DATA_URL,
            FEDRAMP_MARKETPLACE_FETCH_TIMEOUT_SECONDS,
        )
        from linkedin.exceptions import MarketplaceListenerError
        from linkedin.marketplace_listener import collect_fedramp_marketplace

        lookback_days = options.get("lookback_days")
        timeout = options.get("timeout") or FEDRAMP_MARKETPLACE_FETCH_TIMEOUT_SECONDS
        if lookback_days is not None and lookback_days <= 0:
            raise CommandError("--lookback-days must be positive.")
        if timeout <= 0:
            raise CommandError("--timeout must be positive.")

        try:
            summary = collect_fedramp_marketplace(
                changelog_url=(options.get("changelog_url") or FEDRAMP_MARKETPLACE_CHANGELOG_URL),
                data_url=(options.get("data_url") or FEDRAMP_MARKETPLACE_DATA_URL),
                timeout=timeout,
                lookback_days=lookback_days,
                dry_run=bool(options.get("dry_run")),
            )
        except (MarketplaceListenerError, ValueError) as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(json.dumps(summary, indent=2, default=str))
        if summary["dry_run"] and summary["baseline_created"] and lookback_days is None:
            self.stdout.write(
                self.style.WARNING(
                    "Dry run only: a normal run would initialize the baseline "
                    "without queuing historical signals."
                )
            )
        elif summary["baseline_created"] and lookback_days is None:
            self.stdout.write(
                self.style.WARNING(
                    "Baseline initialized. No historical signals were queued; future runs "
                    "will detect new transitions."
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Marketplace listener created {summary['signals_created']} signal(s)."
                )
            )
