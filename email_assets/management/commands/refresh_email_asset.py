"""Re-read an email asset's provider file; a content change requires re-approval."""
from django.core.management.base import BaseCommand, CommandError

from email_assets.display import describe_asset
from email_assets.services import EmailAssetError, refresh_asset, retire_asset


class Command(BaseCommand):
    help = (
        "Preview or record the provider file's current bytes. If they changed, "
        "the asset returns to 'awaiting approval' and emails using it are held "
        "until the new hash is approved. --retire stops all use of the asset."
    )

    def add_arguments(self, parser):
        parser.add_argument("key")
        parser.add_argument("--retire", action="store_true", help="Retire the asset instead of refreshing it.")
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        try:
            if options["retire"]:
                asset = retire_asset(key=options["key"], apply=options["apply"])
                self.stdout.write("\n".join(describe_asset(asset)))
                self.stdout.write(
                    self.style.SUCCESS(f"Retired {asset.key}; emails needing it will be held.")
                    if options["apply"]
                    else "Preview only: re-run with --apply to retire."
                )
                return
            asset, changed, _snapshot = refresh_asset(key=options["key"], apply=options["apply"])
        except EmailAssetError as exc:
            raise CommandError(str(exc)) from None
        self.stdout.write("\n".join(describe_asset(asset)))
        if not changed:
            self.stdout.write("The provider file is unchanged.")
        else:
            self.stdout.write(self.style.WARNING(
                "The provider file changed. Review it, then approve the new hash:\n"
                f"  manage.py approve_email_asset {asset.key} --sha256 {asset.sha256} "
                "--approved-by <name> --apply"
            ))
        if not options["apply"]:
            self.stdout.write("Preview only: nothing was saved. Re-run with --apply to record it.")
