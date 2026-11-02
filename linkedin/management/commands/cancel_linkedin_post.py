"""Cancel one personal LinkedIn post that has not started; any other state is refused."""
import json

from django.core.management.base import BaseCommand, CommandError

from linkedin.exceptions import LinkedInPostStateError, LinkedInPostValidationError
from linkedin.personal_posts import cancel_post


class Command(BaseCommand):
    help = "Mark one queued personal LinkedIn post skipped. Running, sent, failed and uncertain rows are refused."

    def add_arguments(self, parser):
        parser.add_argument("--external-ref", required=True)

    def handle(self, *args, **opts):
        try:
            result = cancel_post(opts["external_ref"])
        except (LinkedInPostStateError, LinkedInPostValidationError) as exc:
            raise CommandError(str(exc)) from None
        self.stdout.write(json.dumps(result, sort_keys=True))
