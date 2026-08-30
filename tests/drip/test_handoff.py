from datetime import timedelta

import pytest
from django.utils import timezone

from crm.models import Deal, Lead, Message, SalesOwner
from drip.exceptions import HandoffReviewError
from drip.manifest import validate_manifest
from drip.models import DripEnrollment, DripLane
from drip.services.handoff import (
    evaluate_gmail_handoff,
    evaluate_linkedin_handoff,
    review_handoff_not_applicable,
)
from drip.services.publication import publish_manifest
from gmail.client import scoped_gmail_id
from linkedin.enums import ProfileState
from linkedin.models import Campaign, Task
from tests.factories import UserFactory


pytestmark = pytest.mark.django_db


def _enrollment(valid_drip_payload):
    published = publish_manifest(validate_manifest(valid_drip_payload))
    lead = Lead.objects.create(
        first_name="Ada",
        last_name="Lovelace",
        company_name="Analytical Engines",
        linkedin_url="https://www.linkedin.com/in/ada-lovelace/",
        public_identifier="ada-lovelace",
        email="ada@example.com",
        icp="CSPs",
    )
    enrollment = DripEnrollment.objects.create(
        campaign=published.campaign,
        campaign_version=published.version,
        lead=lead,
        frozen_icp="CSPs",
        status=DripEnrollment.Status.WAITING,
        activated_at=timezone.now() - timedelta(days=5),
        enrolled_by="reviewer",
        plan_hash="a" * 64,
    )
    return lead, enrollment


def test_linkedin_handoff_uses_canonical_message_operator_as_primary_owner(
    valid_drip_payload,
    monkeypatch,
):
    lead, enrollment = _enrollment(valid_drip_payload)
    current_campaign = Campaign.objects.create(
        name="Current outbound",
        user=UserFactory(username="arian"),
    )
    deal = Deal.objects.create(
        lead=lead,
        campaign=current_campaign,
        state=ProfileState.CONNECTED,
        invitation_sender="Arian",
    )
    lane = DripLane.objects.create(
        enrollment=enrollment,
        channel=DripLane.Channel.LINKEDIN,
        operator="Arian",
        provider_account="arian",
        sender_identity="arian",
        recipient_identity=lead.linkedin_url,
    )
    owner = SalesOwner.objects.get(normalized_handle="arian")
    monkeypatch.setattr("linkedin.icp_outbound.channel_steps", lambda **kwargs: [1, 2])
    message = Message.objects.create(
        lead=lead,
        operator=owner,
        source=Message.Source.LINKEDIN,
        direction=Message.Direction.OUTBOUND,
        external_id=(
            f"daemon-send:Arian:{deal.pk}:linkedin_connect_followup:step-1:variant-0"
        ),
        sender="unmapped-surface-alias@example.com",
        body="Final current follow-up",
        sent_at=timezone.now(),
    )

    result = evaluate_linkedin_handoff(lane)

    assert result.eligible is True
    assert result.completed_at == message.sent_at
    assert result.evidence["message_id"] == message.pk


def test_linkedin_handoff_requires_final_evidence_and_no_live_current_task(
    valid_drip_payload,
    monkeypatch,
):
    lead, enrollment = _enrollment(valid_drip_payload)
    current_campaign = Campaign.objects.create(
        name="Current outbound",
        user=UserFactory(username="arian"),
    )
    Deal.objects.create(
        lead=lead,
        campaign=current_campaign,
        state=ProfileState.CONNECTED,
        invitation_sender="Arian",
    )
    lane = DripLane.objects.create(
        enrollment=enrollment,
        channel=DripLane.Channel.LINKEDIN,
        operator="Arian",
        provider_account="arian",
        sender_identity="arian",
        recipient_identity=lead.linkedin_url,
    )
    monkeypatch.setattr("linkedin.icp_outbound.channel_steps", lambda **kwargs: [1, 2])
    assert evaluate_linkedin_handoff(lane).reason == "current_linkedin_final_step_not_persisted"

    Task.objects.create(
        task_type=Task.TaskType.FOLLOW_UP,
        scheduled_at=timezone.now(),
        payload={
            "campaign_id": current_campaign.pk,
            "public_id": lead.public_identifier,
            "operator": "Arian",
        },
    )
    assert evaluate_linkedin_handoff(lane).reason == "current_linkedin_task_outstanding"


def test_gmail_handoff_freezes_exact_thread_and_rfc_continuation(
    valid_drip_payload,
    monkeypatch,
):
    lead, enrollment = _enrollment(valid_drip_payload)
    lane = DripLane.objects.create(
        enrollment=enrollment,
        channel=DripLane.Channel.GMAIL,
        operator="Arian",
        provider_account="arian_boundera",
        sender_identity="ariant@getboundera.com",
        recipient_identity=lead.email,
    )
    monkeypatch.setattr("gmail.templates.steps_for_icp", lambda **kwargs: [1, 2])
    thread_id = "current-thread-1"
    scoped_thread = scoped_gmail_id("arian_boundera", thread_id)
    first_rfc = "<current-step-0@getboundera.com>"
    final_rfc = "<current-step-1@getboundera.com>"
    common = {
        "gmail_account": "arian_boundera",
        "send_as": "ariant@getboundera.com",
        "gmail_thread_id": thread_id,
        "thread_subject": "Original subject",
    }
    Message.objects.create(
        lead=lead,
        source=Message.Source.GMAIL,
        direction=Message.Direction.OUTBOUND,
        external_id="arian_boundera:message-0",
        sender="ariant@getboundera.com",
        sent_at=timezone.now() - timedelta(days=2),
        thread_external_id=scoped_thread,
        raw={
            **common,
            "automation_key": f"gmail_follow_up:Arian:{lead.pk}:gmail_fallback:step-0",
            "rfc_message_id": first_rfc,
            "references": [],
        },
    )
    final = Message.objects.create(
        lead=lead,
        source=Message.Source.GMAIL,
        direction=Message.Direction.OUTBOUND,
        external_id="arian_boundera:message-1",
        sender="ariant@getboundera.com",
        sent_at=timezone.now() - timedelta(days=1),
        thread_external_id=scoped_thread,
        raw={
            **common,
            "automation_key": f"gmail_follow_up:Arian:{lead.pk}:gmail_fallback:step-1",
            "rfc_message_id": final_rfc,
            "references": [first_rfc],
        },
    )

    result = evaluate_gmail_handoff(lane)

    assert result.eligible is True
    assert result.completed_at == final.sent_at
    assert result.gmail_thread_id == thread_id
    assert result.gmail_thread_subject == "Original subject"
    assert result.evidence["last_rfc_message_id"] == final_rfc
    assert result.evidence["references"] == [first_rfc, final_rfc]


def test_not_applicable_handoff_requires_explicit_apply_and_reviewer(
    valid_drip_payload,
):
    lead, enrollment = _enrollment(valid_drip_payload)
    lane = DripLane.objects.create(
        enrollment=enrollment,
        channel=DripLane.Channel.GMAIL,
        operator="Arian",
        provider_account="arian_boundera",
        sender_identity="ariant@getboundera.com",
        recipient_identity=lead.email,
    )
    with pytest.raises(HandoffReviewError, match="reviewer"):
        review_handoff_not_applicable(lane_id=lane.pk, reviewed_by="", apply=True)

    preview = review_handoff_not_applicable(
        lane_id=lane.pk,
        reviewed_by="human-reviewer",
        apply=False,
    )
    lane.refresh_from_db()
    assert preview.applied is False
    assert lane.current_sequence_status == DripLane.CurrentSequenceStatus.PENDING

    applied = review_handoff_not_applicable(
        lane_id=lane.pk,
        reviewed_by="human-reviewer",
        apply=True,
    )
    lane.refresh_from_db()
    assert applied.applied is True
    assert lane.current_sequence_status == DripLane.CurrentSequenceStatus.NOT_APPLICABLE
    assert lane.current_sequence_reviewed_by == "human-reviewer"
    assert lane.current_sequence_reviewed_at is not None


def test_not_applicable_review_rejects_legacy_gmail_history_without_raw_binding(
    valid_drip_payload,
):
    lead, enrollment = _enrollment(valid_drip_payload)
    lane = DripLane.objects.create(
        enrollment=enrollment,
        channel=DripLane.Channel.GMAIL,
        operator="Arian",
        provider_account="arian_boundera",
        sender_identity="ariant@getboundera.com",
        recipient_identity=lead.email,
    )
    Message.objects.create(
        lead=lead,
        source=Message.Source.GMAIL,
        direction=Message.Direction.OUTBOUND,
        external_id=f"gmail-send:Arian:{lead.pk}:gmail_fallback:step-0:legacy",
        sender="ariant@getboundera.com",
        sent_at=timezone.now(),
        raw={},
    )

    with pytest.raises(HandoffReviewError, match="outbound evidence exists"):
        review_handoff_not_applicable(
            lane_id=lane.pk,
            reviewed_by="human-reviewer",
            apply=True,
        )
