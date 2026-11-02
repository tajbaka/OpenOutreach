"""Queue one externally approved personal LinkedIn post; idempotent per --external-ref.

Social Studio's dashboard normally inserts rows itself; this command is for
manual use and checks. See docs/linkedin-posts.md.
"""
import json

from django.core.management.base import BaseCommand, CommandError
from django.utils.dateparse import parse_datetime

from linkedin.exceptions import LinkedInPostValidationError
from linkedin.personal_posts import enqueue_post, read_text_file


class Command(BaseCommand):
    help = (
        "Queue one approved personal LinkedIn post (text, image or document) for an operator's supervised post worker. "
        "Same --external-ref and payload returns the existing row; a different payload is refused."
    )

    def add_arguments(self, parser):
        parser.add_argument("--operator", required=True, help="Arian, Chuka or Eddy (Eddy is Chuka).")
        parser.add_argument("--external-ref", required=True, help="Caller's unique reference, at most 100 characters.")
        parser.add_argument("--text-file", help="UTF-8 file with the exact post text.")
        parser.add_argument("--media-kind", default="", choices=["", "image", "document"])
        parser.add_argument("--media-url", default="", help="https://cdn.sanity.io/files/... address of the exact file.")
        parser.add_argument("--media-sha256", default="")
        parser.add_argument("--media-bytes", type=int)
        parser.add_argument("--media-name", default="", help="File name, e.g. image.png or carousel.pdf.")
        parser.add_argument("--document-title", default="", help="LinkedIn document title (documents only).")
        parser.add_argument(
            "--post-at",
            help="Not before this ISO 8601 time with a zone (e.g. 2026-11-02T13:15:00Z). Omit to post as soon as possible.",
        )

    def handle(self, *args, **opts):
        try:
            text = read_text_file(opts["text_file"]) if opts["text_file"] else ""
            post_at = None
            if opts["post_at"]:
                post_at = parse_datetime(opts["post_at"].replace("Z", "+00:00"))
                if post_at is None:
                    raise LinkedInPostValidationError("--post-at must be an ISO 8601 time, for example 2026-11-02T13:15:00Z.")
            row, created = enqueue_post(
                operator=opts["operator"], external_ref=opts["external_ref"], commentary=text,
                media_kind=opts["media_kind"], media_url=opts["media_url"], media_sha256=opts["media_sha256"],
                media_bytes=opts["media_bytes"], media_name=opts["media_name"], document_title=opts["document_title"],
                scheduled_for=post_at,
            )
        except LinkedInPostValidationError as exc:
            raise CommandError(str(exc)) from None
        self.stdout.write(json.dumps({
            "external_ref": row.external_ref, "id": row.pk, "operator": row.operator,
            "status": row.status, "created": created,
        }, sort_keys=True))
