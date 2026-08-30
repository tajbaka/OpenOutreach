from django.core.management.base import BaseCommand, CommandError

from drip.exceptions import EnrollmentPlanError
from drip.services.enrollment import (
    apply_reviewed_plan,
    load_enrollment_plan,
    validate_reviewed_plan,
)


class Command(BaseCommand):
    help = "Validate or apply an exact reviewed drip enrollment plan."

    def add_arguments(self, parser):
        parser.add_argument("campaign_key")
        parser.add_argument("--plan", required=True, help="Reviewed JSON plan artifact.")
        parser.add_argument(
            "--reviewed-by",
            required=True,
            help="Human/operator recorded on every created enrollment.",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Create enrollments. Default validates the current state without writes.",
        )

    def handle(self, *args, **options):
        try:
            plan = load_enrollment_plan(options["plan"])
            if not options["apply"]:
                _campaign, entries = validate_reviewed_plan(
                    campaign_key=options["campaign_key"],
                    plan=plan,
                )
                self.stdout.write(
                    f"Dry run: reviewed plan is current and would create "
                    f"{len(entries)} enrollment(s). Pass --apply to enroll.",
                )
                return
            result = apply_reviewed_plan(
                campaign_key=options["campaign_key"],
                plan=plan,
                reviewed_by=options["reviewed_by"],
            )
        except EnrollmentPlanError as exc:
            raise CommandError(str(exc)) from exc

        ids = ", ".join(map(str, result.created_enrollment_ids))
        self.stdout.write(
            self.style.SUCCESS(
                f"Created {len(result.created_enrollment_ids)} enrollment(s): {ids}",
            ),
        )
