from django.core.management.base import BaseCommand, CommandError

from drip.exceptions import HandoffReviewError
from drip.services.handoff import review_handoff_not_applicable


class Command(BaseCommand):
    help = "Review an exact lane whose current channel sequence never applied."

    def add_arguments(self, parser):
        parser.add_argument("--lane-id", type=int, required=True)
        parser.add_argument(
            "--not-applicable",
            action="store_true",
            help="Explicitly attest that the current channel sequence never applied.",
        )
        parser.add_argument("--reviewed-by", required=True)
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Persist the reviewed decision. Default is a no-write safety check.",
        )

    def handle(self, *args, **options):
        if not options["not_applicable"]:
            raise CommandError("--not-applicable is required for this reviewed action.")
        try:
            result = review_handoff_not_applicable(
                lane_id=options["lane_id"],
                reviewed_by=options["reviewed_by"],
                apply=options["apply"],
            )
        except HandoffReviewError as exc:
            raise CommandError(str(exc)) from exc
        prefix = "Applied" if result.applied else "Dry run"
        self.stdout.write(f"{prefix}: lane {result.lane_id} {result.detail}.")
