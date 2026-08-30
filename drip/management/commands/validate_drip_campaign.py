from django.core.management.base import BaseCommand, CommandError

from drip.exceptions import ManifestValidationError
from drip.manifest import load_manifest


class Command(BaseCommand):
    help = "Validate a complete drip campaign manifest without writing database state."

    def add_arguments(self, parser):
        parser.add_argument("path", help="Path to the drip campaign JSON manifest.")

    def handle(self, *args, **options):
        try:
            manifest = load_manifest(options["path"])
        except ManifestValidationError as exc:
            raise CommandError(str(exc)) from exc
        audiences = len(manifest.normalized["audiences"])
        themes = sum(
            len(block["themes"])
            for block in manifest.normalized["audiences"].values()
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Valid drip campaign {manifest.campaign_key!r}: "
                f"{audiences} audience(s), {themes} theme(s), "
                f"sha256={manifest.content_hash}",
            ),
        )
