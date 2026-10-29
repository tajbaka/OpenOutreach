"""Queue one externally approved LinkedIn repost; idempotent per --external-ref."""
import json

from django.core.management.base import BaseCommand, CommandError
from django.utils.dateparse import parse_datetime

from linkedin.exceptions import LinkedInRepostValidationError
from linkedin.reposts import enqueue_repost, read_commentary_file


class Command(BaseCommand):
    help = (
        "Queue one approved LinkedIn repost for an operator's supervised repost worker. "
        "Same --external-ref and payload returns the existing row; a different payload is refused."
    )

    def add_arguments(self, parser):
        parser.add_argument("--operator", required=True, help="Arian, Chuka or Eddy (Eddy is Chuka).")
        parser.add_argument("--post-url", required=True, help="https://www.linkedin.com/feed/update/urn:li:...:<digits>/")
        parser.add_argument("--external-ref", required=True, help="Caller's unique reference, at most 100 characters.")
        parser.add_argument(
            "--commentary-file",
            help="UTF-8 file with the exact commentary. Omit (or empty file) for a plain repost.",
        )
        parser.add_argument(
            "--post-at",
            help="Not before this ISO 8601 time with a zone (e.g. 2026-10-30T13:15:00Z). Omit to post as soon as possible.",
        )

    def handle(self, *args, **opts):
        try:
            commentary = read_commentary_file(opts["commentary_file"]) if opts["commentary_file"] else ""
            post_at = None
            if opts["post_at"]:
                post_at = parse_datetime(opts["post_at"].replace("Z", "+00:00"))
                if post_at is None:
                    raise LinkedInRepostValidationError("--post-at must be an ISO 8601 time, for example 2026-10-30T13:15:00Z.")
            row, created = enqueue_repost(
                operator=opts["operator"], post_url=opts["post_url"],
                external_ref=opts["external_ref"], commentary=commentary, scheduled_for=post_at,
            )
        except LinkedInRepostValidationError as exc:
            raise CommandError(str(exc)) from None
        self.stdout.write(json.dumps({
            "external_ref": row.external_ref, "id": row.pk, "operator": row.operator,
            "status": row.status, "created": created,
        }, sort_keys=True))
