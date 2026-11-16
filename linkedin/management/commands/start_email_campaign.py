"""Schedule a version-bound email-only campaign's first step."""
from datetime import timedelta
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from gmail import handoff
from linkedin.enums import ProfileState
from linkedin.message_delivery import MessageDeliveryError, index_message_version
from linkedin.models import Campaign, CampaignMessageEnrollment, LinkedInProfile, OutboundDelivery
from linkedin.operators import resolve_operator


class Command(BaseCommand):
    help = "Preview or schedule Email 1 for a version-bound Gmail-only campaign."

    def add_arguments(self, parser):
        parser.add_argument("--campaign", required=True, type=int)
        parser.add_argument("--start-at", required=True, help="ISO 8601 timestamp with a UTC offset.")
        parser.add_argument("--interval-minutes", required=True, type=int, help="Spacing between first-email anchors, before send-window deferrals.")
        parser.add_argument("--audience-map", type=Path, help="JSON object mapping Deal IDs to reviewed audience keys.")
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        try:
            start_at = parse_datetime(options["start_at"])
            overrides = json.loads(options["audience_map"].read_text()) if options["audience_map"] else {}
        except (ValueError, OSError) as exc:
            raise CommandError("Invalid start time or audience map.") from exc
        interval = options["interval_minutes"]
        if start_at is None or timezone.is_naive(start_at) or not 1 <= interval <= 1440:
            raise CommandError("Supply a timezone-aware start time and an interval of 1 to 1440 minutes.")
        if not isinstance(overrides, dict) or any(not isinstance(value, str) for value in overrides.values()):
            raise CommandError("The audience map must map Deal IDs to audience keys.")
        campaign = Campaign.objects.select_related("active_message_version__program").filter(pk=options["campaign"]).first()
        if campaign is None or campaign.enable_linkedin or not campaign.enable_gmail or not campaign.active_message_version_id:
            raise CommandError("Choose a version-bound campaign with Gmail enabled and LinkedIn disabled.")
        if campaign.status == Campaign.Status.FINISHED or (options["apply"] and campaign.status != Campaign.Status.ACTIVE):
            raise CommandError("Applying requires an active campaign.")
        profile = LinkedInProfile.objects.filter(user_id=campaign.user_id, active=True).first()
        operator = resolve_operator(profile.linkedin_username) if profile else ""
        if profile is None or not handoff._operator_can_send_gmail(operator):
            raise CommandError("The campaign owner needs an active profile with a Gmail sender mapping.")
        if options["apply"] and profile.stop_requested:
            raise CommandError("The campaign owner's sender stop flag is on.")

        deals = list(campaign.deals.filter(
            state__in=[ProfileState.QUALIFIED, ProfileState.READY_TO_CONNECT],
        ).select_related("lead").order_by("pk"))
        if not overrides.keys() <= {str(deal.pk) for deal in deals}:
            raise CommandError("The audience map includes a Deal outside the campaign's startable roster.")
        enrollments = {item.deal_id: item for item in CampaignMessageEnrollment.objects.filter(deal__campaign=campaign)}
        version = campaign.active_message_version
        try:
            index = index_message_version(version)
            plan = []
            for deal in deals:
                enrollment = enrollments.get(deal.pk)
                audience = overrides.get(str(deal.pk), enrollment.audience_key if enrollment else deal.lead.icp)
                key = index.resolve_audience(audience)
                if enrollment and (
                    enrollment.operator, enrollment.message_version_id, enrollment.audience_key,
                ) != (operator, version.pk, key):
                    raise CommandError(f"Deal {deal.pk} has a conflicting frozen enrollment.")
                if not index.effective_routes(audience_key=key, operator=operator, channel=OutboundDelivery.Channel.GMAIL):
                    raise CommandError(f"Deal {deal.pk} has no Gmail route for {key}.")
                plan.append((deal, key))
        except MessageDeliveryError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(f"Campaign {campaign.pk}, sender {operator}, message version {version.pk}, {len(plan)} Deals.")
        if not options["apply"]:
            self.stdout.write("Preview only. No Tasks queued.")
            return

        queued = 0
        for position, (deal, key) in enumerate(plan):
            task = handoff._maybe_schedule_gmail_sequence(
                deal=deal, operator=operator,
                email_only_start_at=start_at + timedelta(minutes=position * interval),
                audience_key=key, expected_version_id=version.pk,
            )
            queued += int(task is not None and task.status in handoff._pending_or_running())
        self.stdout.write(f"{queued} Deals with pending or running first-step work. Existing send guards apply.")
