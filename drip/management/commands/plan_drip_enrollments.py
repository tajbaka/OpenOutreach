from django.core.management.base import BaseCommand, CommandError

from drip.exceptions import EnrollmentPlanError
from drip.services.enrollment import build_enrollment_plan, write_enrollment_plan


class Command(BaseCommand):
    help = "Create a private, immutable-review enrollment plan for explicit Lead IDs."

    def add_arguments(self, parser):
        parser.add_argument("campaign_key")
        parser.add_argument("--operator", required=True, help="Canonical sender/operator.")
        parser.add_argument(
            "--lead-id",
            action="append",
            type=int,
            required=True,
            dest="lead_ids",
            help="Exact Lead ID to review. Repeat for each Lead.",
        )
        parser.add_argument(
            "--output",
            required=True,
            help="New private JSON review artifact; existing files are never overwritten.",
        )

    def handle(self, *args, **options):
        try:
            plan = build_enrollment_plan(
                campaign_key=options["campaign_key"],
                operator=options["operator"],
                lead_ids=options["lead_ids"],
            )
            path = write_enrollment_plan(plan, options["output"])
        except EnrollmentPlanError as exc:
            raise CommandError(str(exc)) from exc

        eligible = sum(1 for entry in plan["leads"] if entry["eligible"])
        blocked = len(plan["leads"]) - eligible
        self.stdout.write(
            self.style.SUCCESS(
                f"Wrote reviewed enrollment plan {path}: {eligible} eligible, "
                f"{blocked} blocked; plan_hash={plan['plan_hash']}",
            ),
        )
