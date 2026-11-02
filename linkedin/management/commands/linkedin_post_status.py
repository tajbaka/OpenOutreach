"""Read one queued personal LinkedIn post's outcome as JSON (read-only)."""
import json

from django.core.management.base import BaseCommand, CommandError

from linkedin.exceptions import LinkedInPostStateError, LinkedInPostValidationError
from linkedin.personal_posts import post_status


class Command(BaseCommand):
    help = "Print one personal LinkedIn post's status. A running row past its bound reads as uncertain."

    def add_arguments(self, parser):
        parser.add_argument("--external-ref", required=True)

    def handle(self, *args, **opts):
        try:
            result = post_status(opts["external_ref"])
        except (LinkedInPostStateError, LinkedInPostValidationError) as exc:
            raise CommandError(str(exc)) from None
        self.stdout.write(json.dumps(result, sort_keys=True))
