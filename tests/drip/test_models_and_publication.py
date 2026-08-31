from copy import deepcopy

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from crm.models import Lead
from drip.manifest import validate_manifest
from drip.link_attribution import build_attributed_url
from drip.models import (
    DripCampaign,
    DripCampaignVersion,
    DripDelivery,
    DripEnrollment,
    DripLane,
    DripTrackedLink,
)
from drip.services.publication import publish_manifest


pytestmark = pytest.mark.django_db


def _enrollment(*, campaign, version, lead, status=DripEnrollment.Status.WAITING):
    return DripEnrollment.objects.create(
        campaign=campaign,
        campaign_version=version,
        lead=lead,
        frozen_icp="CSPs",
        status=status,
        activated_at=timezone.now(),
        enrolled_by="reviewer",
        plan_hash="a" * 64,
    )


def test_publication_is_immutable_versioned_and_idempotent(valid_drip_payload):
    first = publish_manifest(validate_manifest(valid_drip_payload))
    repeated = publish_manifest(validate_manifest(deepcopy(valid_drip_payload)))
    changed = deepcopy(valid_drip_payload)
    changed["audiences"]["CSPs"]["themes"][0]["intent"] = "A revised intent."
    second = publish_manifest(validate_manifest(changed))

    assert first.created is True
    assert repeated.created is False
    assert repeated.version.pk == first.version.pk
    assert second.created is True
    assert second.version.version == 2
    assert second.campaign.active_version == second.version
    assert second.campaign.status == DripCampaign.Status.ACTIVE

    first.version.content_hash = "0" * 64
    with pytest.raises(ValidationError, match="immutable"):
        first.version.save()
    with pytest.raises(ValidationError, match="immutable"):
        first.version.delete()


def test_one_nonterminal_enrollment_per_lead_across_campaigns(
    valid_drip_payload,
    second_drip_payload,
):
    first = publish_manifest(validate_manifest(valid_drip_payload))
    second = publish_manifest(validate_manifest(second_drip_payload))
    lead = Lead.objects.create(first_name="Ada", email="ada@example.com", icp="CSPs")
    existing = _enrollment(campaign=first.campaign, version=first.version, lead=lead)

    with pytest.raises(IntegrityError), transaction.atomic():
        _enrollment(campaign=second.campaign, version=second.version, lead=lead)

    existing.status = DripEnrollment.Status.STOPPED
    existing.stopped_at = timezone.now()
    existing.save(update_fields={"status", "stopped_at", "updated_at"})
    replacement = _enrollment(campaign=second.campaign, version=second.version, lead=lead)
    assert replacement.pk is not None


def test_active_recipient_owner_is_unique_across_duplicate_leads(
    valid_drip_payload,
    second_drip_payload,
):
    first = publish_manifest(validate_manifest(valid_drip_payload))
    second = publish_manifest(validate_manifest(second_drip_payload))
    lead_one = Lead.objects.create(first_name="Ada", email="same@example.com", icp="CSPs")
    lead_two = Lead.objects.create(first_name="A.", email="same@example.com", icp="CSPs")
    enrollment_one = _enrollment(
        campaign=first.campaign,
        version=first.version,
        lead=lead_one,
    )
    enrollment_two = _enrollment(
        campaign=second.campaign,
        version=second.version,
        lead=lead_two,
    )
    first_lane = DripLane.objects.create(
        enrollment=enrollment_one,
        channel=DripLane.Channel.GMAIL,
        operator="Arian",
        provider_account="ARIAN_BOUNDERA",
        sender_identity="ARIANT@GETBOUNDERA.COM",
        recipient_identity="SAME@EXAMPLE.COM",
    )
    assert first_lane.provider_account == "arian_boundera"
    assert first_lane.recipient_identity == "same@example.com"

    with pytest.raises(IntegrityError), transaction.atomic():
        DripLane.objects.create(
            enrollment=enrollment_two,
            channel=DripLane.Channel.GMAIL,
            operator="Arian",
            provider_account="arian_boundera",
            sender_identity="ariant@getboundera.com",
            recipient_identity="same@example.com",
        )

    first_lane.status = DripLane.Status.STOPPED
    first_lane.save(update_fields={"status", "updated_at"})
    replacement = DripLane.objects.create(
        enrollment=enrollment_two,
        channel=DripLane.Channel.GMAIL,
        operator="Arian",
        provider_account="arian_boundera",
        sender_identity="ariant@getboundera.com",
        recipient_identity="same@example.com",
    )
    assert replacement.pk is not None


def test_not_applicable_lane_requires_review_evidence(valid_drip_payload):
    published = publish_manifest(validate_manifest(valid_drip_payload))
    lead = Lead.objects.create(first_name="Ada", email="ada@example.com", icp="CSPs")
    enrollment = _enrollment(
        campaign=published.campaign,
        version=published.version,
        lead=lead,
    )
    lane = DripLane(
        enrollment=enrollment,
        channel=DripLane.Channel.GMAIL,
        operator="Arian",
        provider_account="arian_boundera",
        sender_identity="ariant@getboundera.com",
        recipient_identity="ada@example.com",
        current_sequence_status=DripLane.CurrentSequenceStatus.NOT_APPLICABLE,
    )

    with pytest.raises(ValidationError, match="requires reviewer"):
        lane.full_clean()


def test_nonterminal_linkedin_lane_requires_valid_unique_member_urn(
    valid_drip_payload,
    second_drip_payload,
):
    first = publish_manifest(validate_manifest(valid_drip_payload))
    second = publish_manifest(validate_manifest(second_drip_payload))
    lead_one = Lead.objects.create(first_name="Ada", icp="CSPs")
    lead_two = Lead.objects.create(first_name="Grace", icp="CSPs")
    enrollment_one = _enrollment(
        campaign=first.campaign,
        version=first.version,
        lead=lead_one,
    )
    enrollment_two = _enrollment(
        campaign=second.campaign,
        version=second.version,
        lead=lead_two,
    )
    missing = DripLane(
        enrollment=enrollment_one,
        channel=DripLane.Channel.LINKEDIN,
        operator="Arian",
        provider_account="arian",
        sender_identity="arian",
        recipient_identity="https://www.linkedin.com/in/ada/",
    )
    with pytest.raises(ValidationError, match="exact fsd_profile"):
        missing.full_clean()

    missing.status = DripLane.Status.STOPPED
    missing.full_clean()

    DripLane.objects.create(
        enrollment=enrollment_one,
        channel=DripLane.Channel.LINKEDIN,
        operator="Arian",
        provider_account="arian",
        sender_identity="arian",
        recipient_identity="https://www.linkedin.com/in/ada/",
        linkedin_member_urn="urn:li:fsd_profile:SAME",
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        DripLane.objects.create(
            enrollment=enrollment_two,
            channel=DripLane.Channel.LINKEDIN,
            operator="Chuka",
            provider_account="chuka",
            sender_identity="chuka",
            recipient_identity="https://www.linkedin.com/in/grace/",
            linkedin_member_urn="urn:li:fsd_profile:SAME",
        )


def test_campaign_version_constraint_rejects_cross_campaign_reference(
    valid_drip_payload,
    second_drip_payload,
):
    first = publish_manifest(validate_manifest(valid_drip_payload))
    second = publish_manifest(validate_manifest(second_drip_payload))
    lead = Lead.objects.create(first_name="Ada", email="ada@example.com", icp="CSPs")
    enrollment = DripEnrollment(
        campaign=first.campaign,
        campaign_version=second.version,
        lead=lead,
        frozen_icp="CSPs",
        enrolled_by="reviewer",
        plan_hash="a" * 64,
    )

    with pytest.raises(ValidationError, match="belongs to another campaign"):
        enrollment.full_clean()


def test_published_version_model_is_registered():
    assert DripCampaignVersion._meta.app_label == "drip"


def test_delivery_media_metadata_is_all_or_none_and_linkedin_only(
    valid_drip_payload,
):
    published = publish_manifest(validate_manifest(valid_drip_payload))
    lead = Lead.objects.create(first_name="Ada", email="ada@example.com", icp="CSPs")
    enrollment = _enrollment(
        campaign=published.campaign,
        version=published.version,
        lead=lead,
    )
    linkedin_lane = DripLane.objects.create(
        enrollment=enrollment,
        channel=DripLane.Channel.LINKEDIN,
        operator="Arian",
        provider_account="arian",
        sender_identity="arian",
        recipient_identity="https://www.linkedin.com/in/ada/",
        linkedin_member_urn="urn:li:fsd_profile:ada",
    )
    gmail_lane = DripLane.objects.create(
        enrollment=enrollment,
        channel=DripLane.Channel.GMAIL,
        operator="Arian",
        provider_account="arian_boundera",
        sender_identity="ariant@getboundera.com",
        recipient_identity="ada@example.com",
    )
    fields = {
        "theme_key": "visibility_gap",
        "theme_index": 0,
        "step_index": 0,
        "frozen_body": "Body",
        "scheduled_at": timezone.now(),
        "provider_account": linkedin_lane.provider_account,
    }
    partial = DripDelivery(
        lane=linkedin_lane,
        frozen_media_kind="gif",
        **fields,
    )
    with pytest.raises(ValidationError, match="entirely populated or entirely blank"):
        partial.full_clean()

    media = {
        "frozen_media_kind": "gif",
        "frozen_media_reference": "demo.gif",
        "frozen_media_mime_type": "image/gif",
        "frozen_media_size_bytes": 100,
        "frozen_media_sha256": "a" * 64,
    }
    linked_delivery = DripDelivery(lane=linkedin_lane, **fields, **media)
    linked_delivery.full_clean()

    gmail_fields = {
        **fields,
        "frozen_subject": "Subject",
        "provider_account": gmail_lane.provider_account,
    }
    gmail_delivery = DripDelivery(lane=gmail_lane, **gmail_fields, **media)
    with pytest.raises(ValidationError, match="only on LinkedIn"):
        gmail_delivery.full_clean()


def test_tracked_link_is_gmail_only_unique_and_immutable(valid_drip_payload):
    published = publish_manifest(validate_manifest(valid_drip_payload))
    lead = Lead.objects.create(first_name="Ada", email="ada@example.com", icp="CSPs")
    enrollment = _enrollment(
        campaign=published.campaign,
        version=published.version,
        lead=lead,
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
    link = DripTrackedLink(
        delivery=delivery,
        reference=reference,
        link_key="fedramp_automation",
        destination_url=destination,
        attributed_url=build_attributed_url(destination, reference),
    )
    link.full_clean()
    link.save()

    with pytest.raises(ValidationError, match="does not match"):
        DripTrackedLink.objects.create(
            delivery=DripDelivery.objects.create(
                lane=lane,
                theme_key="visibility_gap",
                theme_index=0,
                step_index=1,
                frozen_subject="Subject",
                frozen_body="Body",
                scheduled_at=timezone.now(),
                provider_account=lane.provider_account,
            ),
            reference="oo_000000000000000000000A",
            link_key="invalid",
            destination_url=destination,
            attributed_url="https://evil.example/path?ref=oo_000000000000000000000A",
        )

    link.link_key = "changed"
    with pytest.raises(ValidationError, match="immutable"):
        link.save()
    link.refresh_from_db()
    with pytest.raises(ValidationError, match="immutable"):
        link.delete()

    with pytest.raises(IntegrityError), transaction.atomic():
        DripTrackedLink.objects.create(
            delivery=delivery,
            reference="oo_000000000000000000000A",
            link_key="another",
            destination_url=destination,
            attributed_url=(
                "https://boundera.io/fedramp-automation?"
                "ref=oo_000000000000000000000A"
            ),
        )


def test_tracked_link_rejects_linkedin_delivery(valid_drip_payload):
    published = publish_manifest(validate_manifest(valid_drip_payload))
    lead = Lead.objects.create(first_name="Ada", icp="CSPs")
    enrollment = _enrollment(
        campaign=published.campaign,
        version=published.version,
        lead=lead,
    )
    lane = DripLane.objects.create(
        enrollment=enrollment,
        channel=DripLane.Channel.LINKEDIN,
        operator="Arian",
        provider_account="arian",
        sender_identity="arian",
        recipient_identity="https://www.linkedin.com/in/ada/",
        linkedin_member_urn="urn:li:fsd_profile:ada",
    )
    delivery = DripDelivery.objects.create(
        lane=lane,
        theme_key="visibility_gap",
        theme_index=0,
        step_index=0,
        frozen_body="Body",
        scheduled_at=timezone.now(),
        provider_account=lane.provider_account,
    )
    reference = "oo_EjRWeJCrze8SNFZ4kKvN7w"
    destination = "https://boundera.io/fedramp-automation"
    link = DripTrackedLink(
        delivery=delivery,
        reference=reference,
        link_key="fedramp_automation",
        destination_url=destination,
        attributed_url=build_attributed_url(destination, reference),
    )

    with pytest.raises(ValidationError, match="only on Gmail"):
        link.full_clean()
