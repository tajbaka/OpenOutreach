"""Approve the exact bytes of a registered email asset for sending."""
from django.core.management.base import BaseCommand, CommandError

from email_assets.display import describe_asset
from email_assets.services import EmailAssetError, approve_asset


class Command(BaseCommand):
    help = (
        "Preview or approve one email asset. --sha256 must be the full hash of the "
        "file you reviewed; the file is fetched again and must still match."
    )

    def add_arguments(self, parser):
        parser.add_argument("key")
        parser.add_argument("--sha256", required=True, help="Full SHA-256 shown by register/show/refresh.")
        parser.add_argument("--approved-by", required=True)
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        try:
            asset, already = approve_asset(
                key=options["key"],
                approved_by=options["approved_by"],
                sha256=options["sha256"],
                apply=options["apply"],
            )
        except EmailAssetError as exc:
            raise CommandError(str(exc)) from None
        self.stdout.write("\n".join(describe_asset(asset)))
        if already:
            self.stdout.write("Already approved for these exact bytes; nothing changed.")
        elif options["apply"]:
            self.stdout.write(self.style.SUCCESS(f"Approved {asset.key}."))
        else:
            self.stdout.write("Preview only: the current file matches. Re-run with --apply to approve.")
