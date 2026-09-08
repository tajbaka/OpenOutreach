from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from django.utils import timezone

from crm.models import Deal, Lead, Message
from linkedin.db.leads import create_enriched_lead, promote_lead_to_deal
from linkedin.enums import ProfileState
from linkedin.general_icp_messages import content_hash
from linkedin.message_delivery import MessageDeliveryError
from linkedin.message_delivery_runtime import (
    delivery_audit_metadata,
    mark_delivery_status,
)
from linkedin.models import (
    CampaignMessageEnrollment,
    MessageProgram,
    MessageProgramVersion,
    OutboundDelivery,
    Task,
)
from linkedin.tasks.connect import ConnectStrategy, enqueue_follow_up, handle_connect
from linkedin.tasks.follow_up import handle_follow_up


ROLE_PERSONA = "CSP Small | Founder/CEO"
AUDIENCE_KEY = "csp-small-founder-ceo"


def _message(
    *,
    channel: str,
    step_key: str,
    step_index: int,
    delay_hours: int | float,
    body: str,
    subject: str = "",
):
    return {
        "audience_key": AUDIENCE_KEY,
        "role_persona": ROLE_PERSONA,
        "fedramp_segment": "",
        "channel": channel,
        "step_key": step_key,
        "step_index": step_index,
        "delay_hours": delay_hours,
        "variant_key": "primary",
        "subject": subject,
        "body": body,
        "media": [],
        "sender_override": "",
        "notes": "test",
    }


def _version(campaign, *, omit_followup_indexes=()):
    program = MessageProgram.objects.create(
        key="fedramp-marketplace-csp",
        name="FedRAMP Marketplace CSP Outreach",
    )
    payload = {
        "program_key": program.key,
        "program_name": program.name,
        "messages": [
            _message(
                channel="linkedin_connect",
                step_key="connect",
                step_index=0,
                delay_hours=0,
                body="Hi {first_name}, frozen connect for {company_name}.",
            ),
            _message(
                channel="linkedin_followup",
                step_key="followup-1",
                step_index=0,
                delay_hours=0,
                body="Frozen first for {first_name} at {company_name}.",
            ),
            _message(
                channel="linkedin_followup",
                step_key="followup-2",
                step_index=1,
                delay_hours=72,
                body="Frozen second for {first_name}.",
            ),
            _message(
                channel="gmail",
                step_key="email-1",
                step_index=0,
                delay_hours=0.33,
                subject="Frozen subject",
                body="Frozen email for {first_name}.",
            ),
        ],
    }
    payload['messages'] = [message for message in payload['messages']
                           if not (message['channel'] == 'linkedin_followup'
                                   and message['step_index'] in omit_followup_indexes)]
    version = MessageProgramVersion.objects.create(
        program=program,
        version=1,
        schema_version=1,
        payload=payload,
        content_hash=content_hash(payload),
        published_by="test",
    )
    campaign.active_message_version = version
    campaign.save(update_fields={"active_message_version"})
    return version


def _connected_deal(campaign, *, public_id="ada"):
    lead = Lead.objects.create(
        first_name="Ada",
        last_name="Lovelace",
        company_name="Analytical Engines",
        linkedin_url=f"https://www.linkedin.com/in/{public_id}/",
        public_identifier=public_id,
        icp=ROLE_PERSONA,
    )
    deal = Deal.objects.create(
        lead=lead,
        campaign=campaign,
        state=ProfileState.CONNECTED,
        connected_at=timezone.now() - timedelta(days=1),
    )
    Message.objects.create(
        lead=lead,
        source=Message.Source.LINKEDIN,
        external_id=f"connection-note:{public_id}",
        direction=Message.Direction.OUTBOUND,
        sender="Arian",
        body="Connection note",
        sent_at=timezone.now() - timedelta(days=1),
    )
    return deal


@pytest.mark.django_db
def test_bound_enqueue_freezes_role_persona_to_canonical_audience(
    fake_session,
    monkeypatch,
):
    monkeypatch.setattr("linkedin.conf.ENABLE_FOLLOW_UP", True)
    _version(fake_session.campaign)
    deal = _connected_deal(fake_session.campaign)

    task = enqueue_follow_up(
        fake_session.campaign.pk,
        deal.lead.public_identifier,
        operator="Arian",
        icp=ROLE_PERSONA,
        delay_seconds=999,
    )

    enrollment = CampaignMessageEnrollment.objects.get(deal=deal)
    delivery = OutboundDelivery.objects.get(
        enrollment=enrollment,
        channel=OutboundDelivery.Channel.LINKEDIN_FOLLOWUP,
        step_index=0,
    )
    assert enrollment.audience_key == AUDIENCE_KEY
    assert task.payload["delivery_id"] == delivery.pk
    assert task.payload["message_version_id"] == enrollment.message_version_id
    assert task.payload["message_enrollment_id"] == enrollment.pk
    assert task.payload["audience_key"] == AUDIENCE_KEY
    assert delivery.task == task
    assert delivery.frozen_body == "Frozen first for Ada at Analytical Engines."
    assert delivery.status == OutboundDelivery.Status.QUEUED


@pytest.mark.django_db
@pytest.mark.parametrize("omitted", [(0,), (0, 1)])
def test_bound_enqueue_never_skips_a_missing_first_followup(fake_session, monkeypatch, omitted):
    monkeypatch.setattr("linkedin.conf.ENABLE_FOLLOW_UP", True)
    _version(fake_session.campaign, omit_followup_indexes=omitted)
    deal = _connected_deal(fake_session.campaign)
    assert enqueue_follow_up(fake_session.campaign.pk, deal.lead.public_identifier,
                             operator="Arian", icp=ROLE_PERSONA) is None
    assert not Task.objects.filter(task_type=Task.TaskType.FOLLOW_UP).exists()
    assert not OutboundDelivery.objects.filter(channel="linkedin_followup").exists()


@pytest.mark.django_db
def test_bound_followup_uses_frozen_copy_and_keeps_next_step_on_same_version(
    fake_session,
    monkeypatch,
):
    monkeypatch.setattr("linkedin.conf.ENABLE_FOLLOW_UP", True)
    fake_session.linkedin_profile.linkedin_username = "ariant@tryfedrampgpt.com"
    version = _version(fake_session.campaign)
    deal = _connected_deal(fake_session.campaign)
    task = enqueue_follow_up(
        fake_session.campaign.pk,
        deal.lead.public_identifier,
        operator="Arian",
        icp=ROLE_PERSONA,
        delay_seconds=0,
    )
    first_delivery = OutboundDelivery.objects.get(pk=task.payload["delivery_id"])
    task.status = Task.Status.RUNNING
    task.started_at = timezone.now()
    task.save(update_fields={"status", "started_at"})

    deal.lead.first_name = "Changed"
    deal.lead.company_name = "Changed Company"
    deal.lead.save(update_fields={"first_name", "company_name"})
    monkeypatch.setattr(
        "linkedin.icp_outbound.load_icp_messages",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("bound delivery must not read sender JSON")
        ),
    )
    sent = MagicMock(return_value=True)
    monkeypatch.setattr("linkedin.actions.message.send_raw_message", sent)

    handle_follow_up(task, fake_session, {})

    assert sent.call_args.args[2] == first_delivery.frozen_body
    assert sent.call_args.kwargs["delivery_metadata"]["outbound_delivery_id"] == (
        first_delivery.pk
    )
    first_delivery.refresh_from_db()
    assert first_delivery.status == OutboundDelivery.Status.SENT
    assert first_delivery.sent_at is not None
    next_task = Task.objects.get(
        task_type=Task.TaskType.FOLLOW_UP,
        status=Task.Status.PENDING,
        payload__step_index=1,
    )
    second_delivery = OutboundDelivery.objects.get(pk=next_task.payload["delivery_id"])
    assert second_delivery.enrollment_id == first_delivery.enrollment_id
    assert second_delivery.enrollment.message_version_id == version.pk
    assert second_delivery.frozen_body == "Frozen second for Changed."


@pytest.mark.django_db
def test_bound_followup_task_without_delivery_fails_closed(
    fake_session,
    monkeypatch,
):
    fake_session.linkedin_profile.linkedin_username = "ariant@tryfedrampgpt.com"
    _version(fake_session.campaign)
    deal = _connected_deal(fake_session.campaign)
    task = Task.objects.create(
        task_type=Task.TaskType.FOLLOW_UP,
        status=Task.Status.RUNNING,
        scheduled_at=timezone.now(),
        started_at=timezone.now(),
        payload={
            "campaign_id": fake_session.campaign.pk,
            "public_id": deal.lead.public_identifier,
            "operator": "Arian",
            "step_index": 0,
        },
    )
    send = MagicMock(return_value=True)
    monkeypatch.setattr("linkedin.actions.message.send_raw_message", send)

    with pytest.raises(MessageDeliveryError, match="delivery_id"):
        handle_follow_up(task, fake_session, {})

    send.assert_not_called()
    assert not CampaignMessageEnrollment.objects.filter(deal=deal).exists()


@pytest.mark.django_db
def test_bound_followup_task_with_sender_mismatch_fails_closed(
    fake_session,
    monkeypatch,
):
    monkeypatch.setattr("linkedin.conf.ENABLE_FOLLOW_UP", True)
    fake_session.linkedin_profile.linkedin_username = "ariant@tryfedrampgpt.com"
    _version(fake_session.campaign)
    deal = _connected_deal(fake_session.campaign, public_id="sender-mismatch")
    task = enqueue_follow_up(
        fake_session.campaign.pk,
        deal.lead.public_identifier,
        operator="Arian",
        icp=ROLE_PERSONA,
        delay_seconds=0,
    )
    payload = dict(task.payload)
    payload["operator"] = "Chuka"
    task.payload = payload
    task.status = Task.Status.RUNNING
    task.started_at = timezone.now()
    task.save(update_fields={"payload", "status", "started_at"})
    send = MagicMock(return_value=True)
    monkeypatch.setattr("linkedin.actions.message.send_raw_message", send)

    with pytest.raises(MessageDeliveryError, match="operator"):
        handle_follow_up(task, fake_session, {})

    send.assert_not_called()


@pytest.mark.django_db
def test_bound_connect_uses_frozen_note_and_never_reads_sender_json(
    fake_session,
    monkeypatch,
):
    monkeypatch.setattr("linkedin.tasks.connect.ENABLE_CONNECT", True)
    _version(fake_session.campaign)
    profile = {
        "first_name": "Ada",
        "last_name": "Lovelace",
        "headline": "Founder",
        "positions": [{"company_name": "Analytical Engines"}],
    }
    create_enriched_lead(
        fake_session,
        "https://www.linkedin.com/in/ada-connect/",
        profile,
    )
    promote_lead_to_deal(fake_session, "ada-connect")
    deal = Deal.objects.select_related("lead").get(campaign=fake_session.campaign)
    deal.lead.icp = ROLE_PERSONA
    deal.lead.save(update_fields={"icp"})
    candidate = {
        "public_identifier": "ada-connect",
        "url": "https://www.linkedin.com/in/ada-connect/",
        "profile": profile,
        "lead_id": deal.lead_id,
    }
    strategy = ConnectStrategy(
        find_candidate=lambda _session: candidate,
        pre_connect=None,
        delay=10,
        action_fraction=1.0,
        qualifier=MagicMock(explain=lambda *_args, **_kwargs: ""),
    )
    monkeypatch.setattr("linkedin.tasks.connect.strategy_for", lambda *_args: strategy)
    monkeypatch.setattr(
        "linkedin.actions.status.get_connection_status",
        lambda *_args, **_kwargs: ProfileState.QUALIFIED,
    )
    send = MagicMock(return_value=ProfileState.PENDING)
    monkeypatch.setattr("linkedin.actions.connect.send_connection_request", send)
    monkeypatch.setattr(
        "linkedin.icp_outbound.load_icp_messages",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("bound connect must not read sender JSON")
        ),
    )
    task = Task.objects.create(
        task_type=Task.TaskType.CONNECT,
        status=Task.Status.RUNNING,
        scheduled_at=timezone.now(),
        started_at=timezone.now(),
        payload={"campaign_id": fake_session.campaign.pk},
    )

    handle_connect(task, fake_session, {fake_session.campaign.pk: strategy.qualifier})

    delivery = OutboundDelivery.objects.get(
        enrollment__deal=deal,
        channel=OutboundDelivery.Channel.LINKEDIN_CONNECT,
    )
    assert send.call_args.kwargs["note"] == delivery.frozen_body
    assert delivery.frozen_body == "Hi Ada, frozen connect for Analytical Engines."
    delivery.refresh_from_db()
    assert delivery.status == OutboundDelivery.Status.SENT
    deal.refresh_from_db()
    assert deal.sent_note == delivery.frozen_body


@pytest.mark.django_db
def test_linkedin_message_persists_delivery_audit_metadata(fake_session):
    from linkedin.db.chat import save_chat_message

    _version(fake_session.campaign)
    deal = _connected_deal(fake_session.campaign, public_id="metadata")
    task = enqueue_follow_up(
        fake_session.campaign.pk,
        deal.lead.public_identifier,
        operator="Arian",
        icp=ROLE_PERSONA,
        delay_seconds=0,
    )
    delivery = OutboundDelivery.objects.get(pk=task.payload["delivery_id"])

    save_chat_message(
        fake_session,
        deal.lead.public_identifier,
        delivery.frozen_body,
        deal_id=deal.pk,
        sequence_name="linkedin_connect_followup",
        step_index=delivery.step_index,
        operator="Arian",
        delivery_metadata=delivery_audit_metadata(delivery),
    )

    stored = Message.objects.filter(
        lead=deal.lead,
        external_id__startswith="daemon-send:Arian:",
    ).latest("pk")
    assert stored.raw["outbound_delivery_id"] == delivery.pk
    assert stored.raw["message_version_id"] == delivery.enrollment.message_version_id
    assert stored.raw["message_render_hash"] == delivery.render_hash


@pytest.mark.django_db
def test_stopped_delivery_cannot_be_resumed(fake_session, monkeypatch):
    monkeypatch.setattr("linkedin.conf.ENABLE_FOLLOW_UP", True)
    _version(fake_session.campaign)
    deal = _connected_deal(fake_session.campaign, public_id="terminal-delivery")
    task = enqueue_follow_up(
        fake_session.campaign.pk,
        deal.lead.public_identifier,
        operator="Arian",
        icp=ROLE_PERSONA,
        delay_seconds=0,
    )
    delivery = OutboundDelivery.objects.get(pk=task.payload["delivery_id"])

    mark_delivery_status(delivery, OutboundDelivery.Status.STOPPED)

    with pytest.raises(MessageDeliveryError, match="cannot be resumed"):
        mark_delivery_status(delivery, OutboundDelivery.Status.SENDING)
