"""Cancel one LinkedIn repost that has not started; any other state is refused."""
import json

from django.core.management.base import BaseCommand, CommandError

from linkedin.exceptions import LinkedInRepostStateError, LinkedInRepostValidationError
from linkedin.reposts import cancel_repost


class Command(BaseCommand):
    help = "Mark one queued LinkedIn repost skipped. Running, sent, failed and uncertain rows are refused."

    def add_arguments(self, parser):
        parser.add_argument("--external-ref", required=True)

    def handle(self, *args, **opts):
        try:
            result = cancel_repost(opts["external_ref"])
        except (LinkedInRepostStateError, LinkedInRepostValidationError) as exc:
            raise CommandError(str(exc)) from None
        self.stdout.write(json.dumps(result, sort_keys=True))
