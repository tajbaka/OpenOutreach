from django.core.management.base import BaseCommand, CommandError

from drip.exceptions import ManifestValidationError, PublicationError
from drip.manifest import load_manifest
from drip.services.publication import preview_publication, publish_manifest


class Command(BaseCommand):
    help = "Preview or immutably publish a validated drip campaign manifest."

    def add_arguments(self, parser):
        parser.add_argument("path", help="Path to the drip campaign JSON manifest.")
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Persist and activate the immutable version. Default is no-write preview.",
        )

    def handle(self, *args, **options):
        try:
            manifest = load_manifest(options["path"])
            if not options["apply"]:
                preview = preview_publication(manifest)
                if preview.existing_version is not None:
                    detail = f"existing immutable version {preview.existing_version}"
                else:
                    detail = f"new immutable version {preview.next_version}"
                self.stdout.write(
                    f"Dry run: {manifest.campaign_key!r} would select {detail}; "
                    f"sha256={manifest.content_hash}. Pass --apply to publish.",
                )
                return
            result = publish_manifest(manifest)
        except (ManifestValidationError, PublicationError) as exc:
            raise CommandError(str(exc)) from exc

        verb = "Published" if result.created else "Selected existing"
        self.stdout.write(
            self.style.SUCCESS(
                f"{verb} {result.campaign.key!r} version {result.version.version} "
                f"(sha256={result.version.content_hash}); campaign={result.campaign.status}.",
            ),
        )
