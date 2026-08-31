from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from drip.exceptions import LinkAttributionError
from drip.link_attribution import validate_reference
from drip.models import DripTrackedLink


class Command(BaseCommand):
    help = "Resolve one OpenOutreach link reference without changing outreach state."

    def add_arguments(self, parser) -> None:
        parser.add_argument("reference")

    def handle(self, *args, **options):
        raw_reference = options["reference"]
        try:
            reference = validate_reference(raw_reference)
        except LinkAttributionError as exc:
            raise CommandError(str(exc)) from exc

        link = (
            DripTrackedLink.objects.select_related(
                "delivery__lane__enrollment__lead",
                "delivery__lane__enrollment__campaign",
                "delivery__lane__enrollment__campaign_version",
            )
            .filter(reference=reference)
            .first()
        )
        if link is None:
            raise CommandError(f"No drip tracked link found for {reference}")

        delivery = link.delivery
        lane = delivery.lane
        enrollment = lane.enrollment
        payload = {
            "reference": link.reference,
            "link_key": link.link_key,
            "destination_url": link.destination_url,
            "attributed_url": link.attributed_url,
            "delivery": {
                "id": delivery.pk,
                "status": delivery.status,
                "scheduled_at": delivery.scheduled_at.isoformat(),
                "sent_at": delivery.sent_at.isoformat() if delivery.sent_at else None,
                "theme_key": delivery.theme_key,
                "theme_index": delivery.theme_index,
                "step_index": delivery.step_index,
            },
            "lane": {
                "id": lane.pk,
                "channel": lane.channel,
                "operator": lane.operator,
                "provider_account": lane.provider_account,
                "sender_identity": lane.sender_identity,
                "recipient_identity": lane.recipient_identity,
            },
            "enrollment_id": enrollment.pk,
            "lead": {
                "id": enrollment.lead_id,
                "current_email": enrollment.lead.email,
            },
            "campaign": {
                "key": enrollment.campaign.key,
                "version": enrollment.campaign_version.version,
            },
        }
        self.stdout.write(json.dumps(payload, indent=2, sort_keys=True))
