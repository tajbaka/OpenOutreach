"""Plan or apply the reviewed one-time pre-drip outbound cutover."""
from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from linkedin.exceptions import LegacyOutboundCutoverError
from linkedin.legacy_outbound_cutover import (
    apply_legacy_outbound_cutover,
    build_legacy_outbound_cutover_plan,
    load_legacy_outbound_cutover_plan,
    write_legacy_outbound_cutover_plan,
    write_legacy_outbound_cutover_receipt,
)


class Command(BaseCommand):
    help = (
        "Dry-run or atomically apply the reviewed retirement of legacy "
        "outbound Tasks and active historical Campaigns."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--output",
            help=(
                "Write the dry-run review snapshot, or the --apply receipt, "
                "to this path."
            ),
        )
        parser.add_argument(
            "--plan",
            help="Exact reviewed JSON plan artifact required with --apply.",
        )
        parser.add_argument(
            "--reviewed-by",
            help="Reviewer recorded in every terminally retired Task.",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Apply an exact reviewed --plan. Default is no-write planning.",
        )
        parser.add_argument(
            "--confirm-processes-stopped",
            action="store_true",
            help=(
                "Required with --apply: attest that every LinkedIn daemon, "
                "Gmail worker, and supervisor has been stopped."
            ),
        )

    def handle(self, *args, **options):
        try:
            if options["apply"]:
                if not options["plan"]:
                    raise LegacyOutboundCutoverError(
                        "--plan is required with --apply",
                    )
                if not (options["reviewed_by"] or "").strip():
                    raise LegacyOutboundCutoverError(
                        "--reviewed-by is required with --apply",
                    )
                if not options["confirm_processes_stopped"]:
                    raise LegacyOutboundCutoverError(
                        "--confirm-processes-stopped is required with --apply",
                    )
                if options["output"] and (
                    Path(options["output"]).expanduser().resolve()
                    == Path(options["plan"]).expanduser().resolve()
                ):
                    raise LegacyOutboundCutoverError(
                        "--output must not overwrite the reviewed --plan snapshot",
                    )
                reviewed_plan = load_legacy_outbound_cutover_plan(options["plan"])
                result = apply_legacy_outbound_cutover(
                    reviewed_plan,
                    reviewed_by=options["reviewed_by"],
                    processes_stopped=options["confirm_processes_stopped"],
                )
                if options["output"]:
                    output = write_legacy_outbound_cutover_receipt(
                        result,
                        options["output"],
                    )
                    self.stdout.write(f"Apply receipt: {output}")
                self.stdout.write(self.style.SUCCESS(json.dumps(result, indent=2)))
                return

            if options["plan"]:
                raise LegacyOutboundCutoverError(
                    "--plan is only valid with --apply; use --output for a dry run",
                )
            if options["reviewed_by"]:
                raise LegacyOutboundCutoverError(
                    "--reviewed-by is only valid with --apply",
                )
            if options["confirm_processes_stopped"]:
                raise LegacyOutboundCutoverError(
                    "--confirm-processes-stopped is only valid with --apply",
                )
            plan = build_legacy_outbound_cutover_plan()
            if options["output"]:
                output = write_legacy_outbound_cutover_plan(plan, options["output"])
                self.stdout.write(f"Review snapshot: {output}")
            self.stdout.write(json.dumps(plan, indent=2, sort_keys=True))
            self.stdout.write(
                self.style.WARNING(
                    "Dry run only: no Task or Campaign row was changed. "
                    "Review the snapshot, stop sender processes, then pass "
                    "--plan <path> --reviewed-by <name> --apply.",
                ),
            )
        except LegacyOutboundCutoverError as exc:
            raise CommandError(str(exc)) from exc
