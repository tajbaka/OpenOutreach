"""List email assets, or show/check/download one."""
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count

from email_assets.display import describe_asset, human_size
from email_assets.models import EmailAsset
from email_assets.providers import AssetSourceError
from email_assets.resolution import check_asset_source
from email_assets.services import EmailAssetError, fetch_source_snapshot, get_asset


class Command(BaseCommand):
    help = (
        "List email assets, or show one with its assignments. --check confirms "
        "the provider file is readable and unchanged; --download writes the "
        "current provider bytes to a local file for review. Read-only."
    )

    def add_arguments(self, parser):
        parser.add_argument("key", nargs="?", default="")
        parser.add_argument("--check", action="store_true")
        parser.add_argument("--download", default="", help="Write the current file to this new path.")
        parser.add_argument("--assignments", type=int, default=25, help="How many assignments to list.")

    def handle(self, *args, **options):
        if not options["key"]:
            assets = EmailAsset.objects.annotate(assigned=Count("lead_assignments")).order_by("key")
            if not assets:
                self.stdout.write("No email assets are registered.")
            for asset in assets:
                self.stdout.write(
                    f"{asset.key:<40} {asset.get_status_display():<18} "
                    f"{asset.filename:<40} {human_size(asset.size_bytes):>9}  "
                    f"{asset.assigned} assigned"
                )
            return
        try:
            asset = get_asset(options["key"])
        except EmailAssetError as exc:
            raise CommandError(str(exc)) from None
        assignments = asset.lead_assignments.select_related("lead").order_by("lead_id")
        self.stdout.write("\n".join(describe_asset(asset, assignment_count=assignments.count())))
        for row in assignments[: max(options["assignments"], 0)]:
            lead = row.lead
            name = " ".join(f"{lead.first_name or ''} {lead.last_name or ''}".split())
            self.stdout.write(
                f"  Lead {lead.pk:<8} {name:<30} {lead.company_name or '':<30} "
                f"slot {row.slot} (by {row.assigned_by})"
            )
        if options["check"]:
            problem = check_asset_source(asset)
            if problem is None:
                self.stdout.write(self.style.SUCCESS("Check: the approved file is readable and unchanged."))
            else:
                self.stdout.write(self.style.WARNING(f"Check: {problem.code.value} - {problem.message}"))
        if options["download"]:
            target = Path(options["download"]).expanduser()
            if target.exists():
                raise CommandError(f"{target} already exists; choose a new path")
            try:
                snapshot = fetch_source_snapshot(provider=asset.provider, file_id=asset.provider_file_id)
            except AssetSourceError as exc:
                raise CommandError(f"{exc.code}: {exc.message}") from None
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(snapshot.content)
            match = "matches" if snapshot.sha256 == asset.sha256 else "DOES NOT match"
            self.stdout.write(
                f"Wrote {human_size(snapshot.size_bytes)} to {target}; SHA-256 "
                f"{snapshot.sha256} {match} the registered file."
            )
