"""Inspect or update the operator-only General ICP Messages review ledger."""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from linkedin.exceptions import GeneralICPMessageError, SheetsError
from linkedin.general_icp_review_status import GENERAL_ICP_REVIEW_FIELDS


class Command(BaseCommand):
    help = (
        "Compare General ICP Messages with its internal review ledger, or mark "
        "exact cells reviewed without changing the Sheet, campaign JSON, or runtime."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--icp",
            action="append",
            default=[],
            help="Exact four-part ICP label to mark; repeat for multiple rows.",
        )
        parser.add_argument(
            "--cohort",
            action="append",
            default=[],
            help=(
                "Exact three-part 'Role | Company Size | FedRAMP Stage' prefix; "
                "marks all four revenue-intent rows."
            ),
        )
        parser.add_argument(
            "--field",
            action="append",
            choices=sorted(GENERAL_ICP_REVIEW_FIELDS),
            default=[],
            help="Message field to mark; repeat for multiple fields.",
        )
        parser.add_argument("--reviewed-by")
        parser.add_argument(
            "--changed",
            action="store_true",
            help="Record that the selected cells were edited, not only reviewed.",
        )
        parser.add_argument(
            "--modified-at",
            help="Exact ISO 8601 modification time, including UTC offset.",
        )
        parser.add_argument(
            "--modified-date",
            help="YYYY-MM-DD date-only backfill when the exact edit time is unknown.",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Write the internal ledger. Never changes Sheet or campaign state.",
        )
        parser.add_argument(
            "--details",
            action="store_true",
            help="Include current, stale, and missing tracked cell identities.",
        )

    def handle(self, *args, **options):
        from linkedin.general_icp_messages import parse_general_icp_message_rows
        from linkedin.general_icp_messages_sheet import read_general_icp_messages_tab
        from linkedin.general_icp_review_status import (
            compare_general_icp_review_status,
            load_general_icp_review_status,
            mark_general_icp_fields_reviewed,
            select_general_icp_labels,
            write_general_icp_review_status,
        )

        wants_mark = bool(options["icp"] or options["cohort"])
        try:
            rows = read_general_icp_messages_tab()
            parse_general_icp_message_rows(rows)
            ledger = load_general_icp_review_status()

            marked_count = 0
            if wants_mark:
                if not options["field"]:
                    raise GeneralICPMessageError(
                        "--field is required with --icp or --cohort"
                    )
                if not options["reviewed_by"]:
                    raise GeneralICPMessageError(
                        "--reviewed-by is required with --icp or --cohort"
                    )
                labels = select_general_icp_labels(
                    rows,
                    exact_labels=options["icp"],
                    cohorts=options["cohort"],
                )
                reviewed_at = timezone.localtime().isoformat(timespec="seconds")
                modified_at = options["modified_at"]
                if options["changed"] and not (
                    modified_at or options["modified_date"]
                ):
                    modified_at = reviewed_at
                ledger = mark_general_icp_fields_reviewed(
                    rows,
                    ledger,
                    icp_labels=labels,
                    field_keys=options["field"],
                    reviewed_at=reviewed_at,
                    reviewed_by=options["reviewed_by"],
                    changed=bool(options["changed"]),
                    modified_at=modified_at,
                    modified_date=options["modified_date"],
                )
                marked_count = len(labels) * len(set(options["field"]))
                if options["apply"]:
                    write_general_icp_review_status(ledger)
            elif options["apply"]:
                raise GeneralICPMessageError(
                    "--apply requires at least one --icp or --cohort"
                )
            elif any(
                options[name]
                for name in (
                    "field",
                    "reviewed_by",
                    "changed",
                    "modified_at",
                    "modified_date",
                )
            ):
                raise GeneralICPMessageError(
                    "review mutation options require at least one --icp or --cohort"
                )

            comparison = compare_general_icp_review_status(rows, ledger)
        except (GeneralICPMessageError, SheetsError) as exc:
            raise CommandError(str(exc)) from exc

        output = {
            "status": (
                "applied" if wants_mark and options["apply"]
                else "preview" if wants_mark
                else "inspection"
            ),
            "marked_field_count": marked_count,
            **{
                key: value
                for key, value in comparison.items()
                if options["details"] or key not in {"current", "stale", "missing"}
            },
        }
        self.stdout.write(json.dumps(output, indent=2, sort_keys=True))
        if wants_mark and not options["apply"]:
            self.stdout.write("No files changed. Re-run with --apply to update the ledger.")
