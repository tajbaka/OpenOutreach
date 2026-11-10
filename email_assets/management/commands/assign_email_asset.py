"""Assign one email asset to a slot for one or more Leads."""
from django.core.management.base import BaseCommand, CommandError

from email_assets.services import (
    EmailAssetError,
    assign_asset,
    leads_for_company,
    remove_assignments,
)


class Command(BaseCommand):
    help = (
        "Preview or assign an email asset to a slot (e.g. marketplace_pdf) for "
        "Leads chosen by ID or by exact company name within one campaign. Several "
        "contacts can share one asset. --remove clears the slot instead."
    )

    def add_arguments(self, parser):
        parser.add_argument("key", nargs="?", default="", help="Asset key (omit with --remove).")
        parser.add_argument("--slot", required=True)
        parser.add_argument("--lead", type=int, action="append", default=[], help="Lead ID; repeatable.")
        parser.add_argument("--campaign", type=int, help="With --company: choose that campaign's Leads.")
        parser.add_argument("--company", default="", help="Exact company name (case and spacing ignored).")
        parser.add_argument("--assigned-by", default="")
        parser.add_argument("--remove", action="store_true")
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        lead_ids = list(options["lead"])
        if options["company"]:
            if not options["campaign"]:
                raise CommandError("--company requires --campaign so the match stays bounded")
            matches = leads_for_company(campaign_id=options["campaign"], company=options["company"])
            if not matches:
                raise CommandError(
                    f"no Lead in campaign {options['campaign']} has company "
                    f"{options['company']!r}"
                )
            lead_ids.extend(lead.pk for lead in matches)
        if not lead_ids:
            raise CommandError("choose Leads with --lead or --campaign/--company")
        try:
            if options["remove"]:
                changes = remove_assignments(
                    slot=options["slot"],
                    lead_ids=lead_ids,
                    apply=options["apply"],
                )
            else:
                if not options["key"]:
                    raise CommandError("an asset key is required unless --remove is used")
                if not options["assigned_by"]:
                    raise CommandError("--assigned-by is required")
                changes = assign_asset(
                    key=options["key"],
                    slot=options["slot"],
                    lead_ids=lead_ids,
                    assigned_by=options["assigned_by"],
                    apply=options["apply"],
                )
        except EmailAssetError as exc:
            raise CommandError(str(exc)) from None
        for change in changes:
            detail = {
                "create": f"-> {change.asset_key}",
                "replace": f"{change.previous_asset_key} -> {change.asset_key}",
                "unchanged": f"already {change.asset_key}",
                "remove": f"remove {change.previous_asset_key}",
                "absent": "nothing assigned",
            }[change.action]
            self.stdout.write(
                f"Lead {change.lead_id:<8} {change.lead_label:<40} "
                f"{change.slot}: {change.action:<9} {detail}"
            )
        counts = {}
        for change in changes:
            counts[change.action] = counts.get(change.action, 0) + 1
        summary = ", ".join(f"{count} {action}" for action, count in sorted(counts.items()))
        if options["apply"]:
            self.stdout.write(self.style.SUCCESS(f"Applied: {summary}."))
        else:
            self.stdout.write(f"Preview only ({summary}). Re-run with --apply to save.")
