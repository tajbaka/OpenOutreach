"""Queue one failed personal LinkedIn post again, as the same row, after its cause was fixed."""
import json

from django.core.management.base import BaseCommand, CommandError

from linkedin.exceptions import LinkedInPostStateError, LinkedInPostValidationError
from linkedin.personal_posts import MAX_POST_RETRIES, retry_post


class Command(BaseCommand):
    help = (
        "Requeue one failed personal LinkedIn post (no final click, so nothing was posted) as the same row, "
        f"at most {MAX_POST_RETRIES} times. Uncertain, sent, skipped, queued and running rows are refused. "
        "Use this after fixing a bug; never insert a new row for the same post."
    )

    def add_arguments(self, parser):
        parser.add_argument("--external-ref", required=True)

    def handle(self, *args, **opts):
        try:
            result = retry_post(opts["external_ref"])
        except (LinkedInPostStateError, LinkedInPostValidationError) as exc:
            raise CommandError(str(exc)) from None
        self.stdout.write(json.dumps(result, sort_keys=True))
