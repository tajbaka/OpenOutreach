"""Register a private Drive file as a reusable email asset (awaiting approval)."""
from django.core.management.base import BaseCommand, CommandError

from email_assets.display import describe_asset
from email_assets.providers import AssetSourceError, provider_for_link, source_for_provider
from email_assets.services import EmailAssetError, register_asset


class Command(BaseCommand):
    help = (
        "Preview or register a Google Drive file as an email asset. The file is "
        "read through the OpenOutreach service account and stays private. "
        "Registering never approves, assigns or sends it."
    )

    def add_arguments(self, parser):
        parser.add_argument("link", help="Google Drive file link (or file ID).")
        parser.add_argument("--key", required=True, help="Stable lowercase asset key, e.g. acme-marketplace-pdf.")
        parser.add_argument("--name", required=True, help="Human label for reviewers.")
        parser.add_argument("--registered-by", required=True)
        parser.add_argument("--filename", default="", help="Attachment filename recipients see (defaults to the Drive name).")
        parser.add_argument("--notes", default="")
        parser.add_argument("--apply", action="store_true", help="Save the asset; without it, preview only.")

    def handle(self, *args, **options):
        try:
            asset, snapshot = register_asset(
                url=options["link"],
                key=options["key"],
                name=options["name"],
                registered_by=options["registered_by"],
                filename=options["filename"],
                notes=options["notes"],
                apply=options["apply"],
            )
        except EmailAssetError as exc:
            hint = ""
            try:
                hint = source_for_provider(provider_for_link(options["link"])).access_hint()
            except AssetSourceError:
                pass
            raise CommandError(f"{exc}{(' ' + hint) if hint and hint not in str(exc) else ''}") from None
        self.stdout.write("\n".join(describe_asset(asset)))
        self.stdout.write(f"Drive name:   {snapshot.metadata.name}")
        if not options["apply"]:
            self.stdout.write("Preview only: nothing was saved. Re-run with --apply to register.")
            return
        self.stdout.write(self.style.SUCCESS(f"Registered {asset.key} awaiting approval."))
        self.stdout.write(
            "Review the file (show_email_asset "
            f"{asset.key} --download <path>), then approve exactly these bytes:\n"
            f"  manage.py approve_email_asset {asset.key} --sha256 {asset.sha256} "
            "--approved-by <name> --apply"
        )
