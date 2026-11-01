"""An explicit, preview-first update of one campaign's connection priority."""
import json

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


class Command(BaseCommand):
    help = "Preview or set one campaign's connection priority; does not activate or enqueue work."

    def add_arguments(self, parser):
        parser.add_argument("campaign_id", type=int)
        parser.add_argument("priority", type=int)
        parser.add_argument("--owner-username", required=True, help="Exact existing campaign owner's username.")
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        from linkedin.models import Campaign

        priority = options["priority"]
        if not 0 <= priority <= 32767:
            raise CommandError("priority must be an integer from 0 to 32767.")
        with transaction.atomic():
            campaign = Campaign.objects.select_for_update().select_related("user").filter(
                pk=options["campaign_id"], user__username=options["owner_username"],
            ).first()
            if campaign is None:
                raise CommandError("No campaign matches this ID and exact owner username.")
            previous = campaign.connection_priority
            if options["apply"] and priority != previous:
                campaign.connection_priority = priority
                campaign.save(update_fields=["connection_priority"])
            self.stdout.write(json.dumps({
                "campaign_id": campaign.pk, "name": campaign.name,
                "owner_username": campaign.user.username, "status": campaign.status,
                "previous_priority": previous, "connection_priority": priority,
                "applied": options["apply"],
            }))
