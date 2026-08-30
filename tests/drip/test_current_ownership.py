from datetime import timedelta

import pytest
from django.utils import timezone

from crm.models import Deal, Lead
from drip.manifest import validate_manifest
from drip.models import DripEnrollment, DripLane
from drip.services.publication import publish_manifest
from gmail.handoff import enqueue_email_enrichment, enqueue_gmail_follow_up
from gmail.tasks.enrich_email import handle_enrich_email
from gmail.tasks.follow_up import handle_gmail_follow_up
from linkedin.enums import ProfileState
from linkedin.models import Task
from linkedin.tasks.connect import enqueue_follow_up
from linkedin.tasks.follow_up import handle_follow_up


pytestmark = pytest.mark.django_db


def _lead() -> Lead:
    return Lead.objects.create(
        first_name="Ada",
        last_name="Lovelace",
        company_name="Analytical Engines",
        linkedin_url="https://www.linkedin.com/in/ada-lovelace/",
        public_identifier="ada-lovelace",
        email="ada@example.com",
        icp="CSPs",
    )


def _handed_off_lane(valid_drip_payload, lead: Lead, *, channel: str) -> DripLane:
    published = publish_manifest(validate_manifest(valid_drip_payload))
    now = timezone.now()
    enrollment = DripEnrollment.objects.create(
        campaign=published.campaign,
        campaign_version=published.version,
        lead=lead,
        frozen_icp="CSPs",
        status=DripEnrollment.Status.ACTIVE,
        activated_at=now - timedelta(days=2),
        enrolled_by="reviewer",
        plan_hash="a" * 64,
    )
    is_gmail = channel == DripLane.Channel.GMAIL
    return DripLane.objects.create(
        enrollment=enrollment,
        channel=channel,
        operator="Arian",
        provider_account="arian_boundera" if is_gmail else "arian",
        sender_identity="ariant@getboundera.com" if is_gmail else "arian",
        recipient_identity=lead.email if is_gmail else lead.linkedin_url,
        status=DripLane.Status.ACTIVE,
        current_sequence_status=DripLane.CurrentSequenceStatus.NOT_APPLICABLE,
        current_sequence_reviewed_at=now - timedelta(days=2),
        current_sequence_reviewed_by="reviewer",
        handed_off_at=now - timedelta(days=1),
        current_theme_key="visibility_gap",
        theme_started_at=now - timedelta(days=1),
    )


def test_current_gmail_enqueue_and_enrichment_noop_after_handoff(
    valid_drip_payload,
    monkeypatch,
):
    lead = _lead()
    _handed_off_lane(valid_drip_payload, lead, channel=DripLane.Channel.GMAIL)
    monkeypatch.setattr("gmail.handoff.ENABLE_GMAIL_SEQUENCE", True)

    assert enqueue_gmail_follow_up(lead_id=lead.pk, operator="Arian") is None
    assert enqueue_email_enrichment(lead_id=lead.pk, operator="Arian") is None
    assert not Task.objects.filter(
        task_type__in=(Task.TaskType.GMAIL_FOLLOW_UP, Task.TaskType.ENRICH_EMAIL),
        payload__lead_id=lead.pk,
    ).exists()


def test_claimed_current_gmail_handlers_noop_after_handoff(
    valid_drip_payload,
    monkeypatch,
):
    lead = _lead()
    _handed_off_lane(valid_drip_payload, lead, channel=DripLane.Channel.GMAIL)
    monkeypatch.setattr("gmail.tasks.follow_up.ENABLE_GMAIL_SEQUENCE", True)

    class NoGmailClient:
        def __init__(self, *, operator):
            raise AssertionError("current Gmail must stop before provider auth")

    class NoEnrichmentProvider:
        def __init__(self):
            raise AssertionError("email enrichment must stop after Gmail handoff")

    monkeypatch.setattr("gmail.tasks.follow_up.GmailClient", NoGmailClient)
    monkeypatch.setattr(
        "gmail.tasks.enrich_email.BetterContactEmailProvider",
        NoEnrichmentProvider,
    )
    gmail_task = Task.objects.create(
        task_type=Task.TaskType.GMAIL_FOLLOW_UP,
        scheduled_at=timezone.now(),
        payload={"lead_id": lead.pk, "operator": "Arian", "step_index": 0},
    )
    enrichment_task = Task.objects.create(
        task_type=Task.TaskType.ENRICH_EMAIL,
        scheduled_at=timezone.now(),
        payload={
            "lead_id": lead.pk,
            "operator": "Arian",
            "bettercontact_email_request_id": "",
        },
    )

    handle_gmail_follow_up(gmail_task)
    assert handle_enrich_email(enrichment_task) is None


def test_current_linkedin_enqueue_and_handler_noop_after_handoff(
    valid_drip_payload,
    fake_session,
    monkeypatch,
):
    lead = _lead()
    _handed_off_lane(valid_drip_payload, lead, channel=DripLane.Channel.LINKEDIN)
    fake_session.linkedin_profile.linkedin_username = "ariantajbakh@gmail.com"
    fake_session.linkedin_profile.save(update_fields=["linkedin_username"])
    Deal.objects.create(
        lead=lead,
        campaign=fake_session.campaign,
        state=ProfileState.CONNECTED,
        invitation_sender="Arian",
        connected_at=timezone.now() - timedelta(days=5),
    )
    monkeypatch.setattr("linkedin.conf.ENABLE_FOLLOW_UP", True)
    monkeypatch.setattr("linkedin.tasks.follow_up.ENABLE_FOLLOW_UP", True)

    enqueue_follow_up(
        fake_session.campaign.pk,
        lead.public_identifier,
        operator="Arian",
    )
    assert not Task.objects.filter(
        task_type=Task.TaskType.FOLLOW_UP,
        payload__public_id=lead.public_identifier,
    ).exists()

    task = Task.objects.create(
        task_type=Task.TaskType.FOLLOW_UP,
        scheduled_at=timezone.now(),
        payload={
            "campaign_id": fake_session.campaign.pk,
            "public_id": lead.public_identifier,
            "operator": "Arian",
        },
    )
    monkeypatch.setattr(
        "linkedin.tasks.follow_up.get_profile_dict_for_public_id",
        lambda session, public_id: {"public_identifier": public_id},
    )
    monkeypatch.setattr(
        "linkedin.actions.message.send_raw_message",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("current LinkedIn must not send after handoff"),
        ),
    )

    handle_follow_up(task, fake_session, qualifiers=None)
