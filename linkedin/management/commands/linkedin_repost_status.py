"""Read one queued LinkedIn repost's outcome as JSON (read-only)."""
import json

from django.core.management.base import BaseCommand, CommandError

from linkedin.exceptions import LinkedInRepostStateError, LinkedInRepostValidationError
from linkedin.reposts import repost_status


class Command(BaseCommand):
    help = "Print one LinkedIn repost's status. A running row past its bound reads as uncertain."

    def add_arguments(self, parser):
        parser.add_argument("--external-ref", required=True)

    def handle(self, *args, **opts):
        try:
            result = repost_status(opts["external_ref"])
        except (LinkedInRepostStateError, LinkedInRepostValidationError) as exc:
            raise CommandError(str(exc)) from None
        self.stdout.write(json.dumps(result, sort_keys=True))
