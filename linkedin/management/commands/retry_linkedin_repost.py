"""Queue one failed LinkedIn repost again, as the same row, after its cause was fixed."""
import json

from django.core.management.base import BaseCommand, CommandError

from linkedin.exceptions import LinkedInRepostStateError, LinkedInRepostValidationError
from linkedin.reposts import MAX_REPOST_RETRIES, retry_repost

class Command(BaseCommand):
    help = (
        "Requeue one failed LinkedIn repost (no final click, so nothing was posted) as the same row, "
        f"at most {MAX_REPOST_RETRIES} times. Uncertain, sent, skipped, queued and running rows are refused. "
        "Use this after fixing a bug; never enqueue a new row for the same repost."
    )

    def add_arguments(self, parser):
        parser.add_argument("--external-ref", required=True)

    def handle(self, *args, **opts):
        try:
            result = retry_repost(opts["external_ref"])
        except (LinkedInRepostStateError, LinkedInRepostValidationError) as exc:
            raise CommandError(str(exc)) from None
        self.stdout.write(json.dumps(result, sort_keys=True))
