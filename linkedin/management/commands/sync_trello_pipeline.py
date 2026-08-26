"""Dry-run-first command for the curated Trello sales pipeline."""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from linkedin.exceptions import TrelloError


class Command(BaseCommand):
    help = (
        "Safely synchronize Opportunities with nonblank pipeline_stage values "
        "to the configured Trello board. Defaults to dry-run."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Apply the reviewed card/list and CRM stage changes.",
        )
        parser.add_argument(
            "--bootstrap-lists",
            action="store_true",
            help=(
                "Explicitly create missing canonical pipeline lists. In "
                "dry-run this reports the required list count without writing."
            ),
        )

    def handle(self, *args, **options):
        from linkedin import conf
        from linkedin.trello import TrelloClient
        from linkedin.trello_pipeline import sync_trello_pipeline

        missing_config = [
            label
            for label, value in (
                ("TRELLO_API_KEY", conf.TRELLO_API_KEY),
                ("TRELLO_API_TOKEN", conf.TRELLO_API_TOKEN),
                ("TRELLO_BOARD_ID", conf.TRELLO_BOARD_ID),
            )
            if not str(value or "").strip()
        ]
        if missing_config:
            raise CommandError(
                "Trello pipeline configuration is incomplete: "
                + ", ".join(missing_config)
            )

        try:
            client = TrelloClient(
                api_key=conf.TRELLO_API_KEY,
                api_token=conf.TRELLO_API_TOKEN,
                base_url=conf.TRELLO_API_BASE,
                timeout=conf.TRELLO_HTTP_TIMEOUT_SECONDS,
            )
            # Keep selected Opportunity rows stable through compare-before-
            # write and the final mapping commit.  Dry-run also uses the exact
            # same planner, but does not mutate either side.
            with transaction.atomic():
                report = sync_trello_pipeline(
                    client=client,
                    board_id=conf.TRELLO_BOARD_ID,
                    apply=bool(options["apply"]),
                    bootstrap_lists=bool(options["bootstrap_lists"]),
                )
                if not options["apply"]:
                    transaction.set_rollback(True)
        except TrelloError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(json.dumps(report, sort_keys=True))

