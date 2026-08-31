import json
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from crm.models import Lead
from drip.link_attribution import build_attributed_url
from drip.manifest import validate_manifest
from drip.models import DripDelivery, DripEnrollment, DripLane, DripTrackedLink
from drip.services.publication import publish_manifest


pytestmark = pytest.mark.django_db


def _tracked_link(valid_drip_payload):
    published = publish_manifest(validate_manifest(valid_drip_payload))
    lead = Lead.objects.create(first_name="Ada", email="ada@example.com", icp="CSPs")
    enrollment = DripEnrollment.objects.create(
        campaign=published.campaign,
        campaign_version=published.version,
        lead=lead,
        frozen_icp="CSPs",
        enrolled_by="reviewer",
        plan_hash="a" * 64,
    )
    lane = DripLane.objects.create(
        enrollment=enrollment,
        channel=DripLane.Channel.GMAIL,
        operator="Arian",
        provider_account="arian_boundera",
        sender_identity="ariant@getboundera.com",
        recipient_identity=lead.email,
    )
    delivery = DripDelivery.objects.create(
        lane=lane,
        theme_key="visibility_gap",
        theme_index=0,
        step_index=0,
        frozen_subject="Subject",
        frozen_body="Body",
        scheduled_at=timezone.now(),
        provider_account=lane.provider_account,
    )
    reference = "oo_EjRWeJCrze8SNFZ4kKvN7w"
    destination = "https://boundera.io/fedramp-automation"
    return DripTrackedLink.objects.create(
        delivery=delivery,
        reference=reference,
        link_key="fedramp_automation",
        destination_url=destination,
        attributed_url=build_attributed_url(destination, reference),
    )


def test_resolve_drip_reference_is_read_only_and_prints_full_mapping(valid_drip_payload):
    link = _tracked_link(valid_drip_payload)
    lead = link.delivery.lane.enrollment.lead
    lead.email = "changed@example.com"
    lead.save(update_fields={"email"})
    stdout = StringIO()

    call_command("resolve_drip_reference", link.reference, stdout=stdout)

    payload = json.loads(stdout.getvalue())
    assert payload["reference"] == link.reference
    assert payload["link_key"] == "fedramp_automation"
    assert payload["delivery"]["id"] == link.delivery_id
    assert payload["lane"]["channel"] == DripLane.Channel.GMAIL
    assert payload["lane"]["sender_identity"] == "ariant@getboundera.com"
    assert payload["lane"]["recipient_identity"] == "ada@example.com"
    assert payload["lead"]["current_email"] == "changed@example.com"
    assert payload["campaign"] == {"key": "fedramp_reengagement", "version": 1}
    assert DripTrackedLink.objects.get(pk=link.pk).reference == link.reference


def test_resolve_drip_reference_rejects_invalid_or_unknown_reference():
    with pytest.raises(CommandError, match="canonical 128-bit"):
        call_command("resolve_drip_reference", "oo_short")
    with pytest.raises(CommandError, match="No drip tracked link"):
        call_command(
            "resolve_drip_reference",
            "oo_000000000000000000000A",
        )
