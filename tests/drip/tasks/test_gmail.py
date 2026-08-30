from datetime import timedelta

import pytest
from django.utils import timezone

from crm.models import Lead, Message
from drip.manifest import validate_manifest
from drip.models import (
    DripDelivery,
    DripDeliveryAttempt,
    DripEnrollment,
    DripLane,
)
from drip.services.publication import publish_manifest
from drip.tasks.gmail import handle_drip_gmail, recover_stale_drip_gmail_task
from gmail.client import GmailSendResult
from linkedin.models import Task


pytestmark = pytest.mark.django_db


class FakeGmailClient:
    account_key = "arian_boundera"
    send_as = "ariant@getboundera.com"
    reply_to = "ariant@boundera.io"
    calls = []
    send_count = 0
    before_callback = None
    fail_after_callback = None
    provider_rfc_ids = []

    def __init__(self, *, operator):
        self.operator = operator

    def send_message(self, **kwargs):
        if type(self).before_callback is not None:
            type(self).before_callback()
        kwargs["on_submit_attempt"]()
        if type(self).fail_after_callback is not None:
            raise type(self).fail_after_callback
        type(self).send_count += 1
        type(self).calls.append(kwargs)
        provider_rfc_message_id = (
            type(self).provider_rfc_ids[type(self).send_count - 1]
            if len(type(self).provider_rfc_ids) >= type(self).send_count
            else kwargs["rfc_message_id"]
        )
        return GmailSendResult(
            message_id=f"drip-message-{type(self).send_count}",
            thread_id=kwargs["thread_id"] or "drip-thread-1",
            rfc_message_id=provider_rfc_message_id,
        )


@pytest.fixture(autouse=True)
def _reset_fake(monkeypatch):
    FakeGmailClient.calls = []
    FakeGmailClient.send_count = 0
    FakeGmailClient.before_callback = None
    FakeGmailClient.fail_after_callback = None
    FakeGmailClient.provider_rfc_ids = []
    monkeypatch.setattr("drip.tasks.gmail.GmailClient", FakeGmailClient)
    monkeypatch.setattr("linkedin.suppression.lead_suppression_match", lambda lead: None)


def _domain(valid_drip_payload, *, inherited_thread=False):
    published = publish_manifest(validate_manifest(valid_drip_payload))
    lead = Lead.objects.create(
        first_name="Ada",
        last_name="Lovelace",
        company_name="Analytical Engines",
        email="ada@example.com",
        icp="CSPs",
    )
    enrollment = DripEnrollment.objects.create(
        campaign=published.campaign,
        campaign_version=published.version,
        lead=lead,
        frozen_icp="CSPs",
        status=DripEnrollment.Status.ACTIVE,
        activated_at=timezone.now(),
        enrolled_by="reviewer",
        plan_hash="a" * 64,
    )
    current_sequence_status = (
        DripLane.CurrentSequenceStatus.COMPLETED
        if inherited_thread
        else DripLane.CurrentSequenceStatus.NOT_APPLICABLE
    )
    handoff_evidence = {"mode": "not_applicable"}
    if inherited_thread:
        handoff_evidence = {
            "mode": "current_sequence_completed",
            "gmail_account": "arian_boundera",
            "send_as": "ariant@getboundera.com",
            "gmail_thread_id": "current-thread-1",
            "last_rfc_message_id": "<current-final@getboundera.com>",
            "references": [
                "<current-first@getboundera.com>",
                "<current-final@getboundera.com>",
            ],
        }
    now = timezone.now() - timedelta(days=2)
    lane = DripLane.objects.create(
        enrollment=enrollment,
        channel=DripLane.Channel.GMAIL,
        operator="Arian",
        provider_account="arian_boundera",
        sender_identity="ariant@getboundera.com",
        recipient_identity="ada@example.com",
        status=DripLane.Status.ACTIVE,
        current_sequence_status=current_sequence_status,
        current_sequence_reviewed_at=now,
        current_sequence_reviewed_by="reviewer",
        handoff_evidence=handoff_evidence,
        handed_off_at=now,
        current_theme_index=0,
        current_theme_key="visibility_gap",
        theme_started_at=now,
        gmail_thread_id="current-thread-1" if inherited_thread else "",
        gmail_thread_subject="Original current subject" if inherited_thread else "",
    )
    return lead, enrollment, lane


def _queued_delivery(
    lane,
    *,
    step_index=0,
    subject="A new drip subject",
    body="A frozen drip body",
):
    delivery = DripDelivery.objects.create(
        lane=lane,
        theme_key=lane.current_theme_key,
        theme_index=lane.current_theme_index,
        step_index=step_index,
        frozen_subject=subject,
        frozen_body=body,
        scheduled_at=timezone.now() - timedelta(seconds=1),
        status=DripDelivery.Status.PLANNED,
        provider_account=lane.provider_account,
    )
    task = Task.objects.create(
        task_type=Task.TaskType.DRIP_GMAIL,
        status=Task.Status.RUNNING,
        started_at=timezone.now(),
        scheduled_at=delivery.scheduled_at,
        payload={"delivery_id": delivery.pk, "operator": lane.operator},
    )
    delivery.status = DripDelivery.Status.QUEUED
    delivery.current_task = task
    delivery.save(update_fields={"status", "current_task", "updated_at"})
    return delivery, task


def test_drip_gmail_opens_thread_and_persists_real_provider_ids(valid_drip_payload):
    lead, _enrollment, lane = _domain(valid_drip_payload)
    delivery, task = _queued_delivery(lane)

    handle_drip_gmail(task)

    delivery.refresh_from_db()
    lane.refresh_from_db()
    attempt = delivery.attempts.get()
    message = delivery.outbound_message
    assert delivery.status == DripDelivery.Status.SENT
    assert delivery.current_task == task
    assert delivery.provider_message_id == "drip-message-1"
    assert delivery.provider_thread_id == "drip-thread-1"
    assert delivery.rfc_message_id.startswith("<openoutreach-drip-")
    assert delivery.rfc_references == ""
    assert attempt.outcome == DripDeliveryAttempt.Outcome.SENT
    assert attempt.submission_attempted_at is not None
    assert attempt.finished_at is not None
    assert lane.gmail_thread_id == "drip-thread-1"
    assert lane.gmail_thread_subject == "A new drip subject"
    assert message.lead == lead
    assert message.external_id == "arian_boundera:drip-message-1"
    assert message.thread_external_id == "arian_boundera:drip-thread-1"
    assert message.raw["gmail_message_id"] == "drip-message-1"
    assert message.raw["gmail_thread_id"] == "drip-thread-1"
    assert message.raw["reply_to"] == "ariant@boundera.io"
    assert message.raw["delivery_id"] == delivery.pk
    assert FakeGmailClient.calls[0]["thread_id"] == ""
    assert FakeGmailClient.calls[0]["in_reply_to"] == ""


def test_drip_gmail_inherits_current_thread_and_original_subject(valid_drip_payload):
    _lead, _enrollment, lane = _domain(valid_drip_payload, inherited_thread=True)
    delivery, task = _queued_delivery(lane, subject="Manifest drip subject")

    handle_drip_gmail(task)

    delivery.refresh_from_db()
    call = FakeGmailClient.calls[0]
    assert call["thread_id"] == "current-thread-1"
    assert call["subject"] == "Original current subject"
    assert call["in_reply_to"] == "<current-final@getboundera.com>"
    assert call["references"] == (
        "<current-first@getboundera.com>",
        "<current-final@getboundera.com>",
    )
    assert delivery.provider_thread_id == "current-thread-1"


def test_drip_gmail_later_step_replies_to_previous_success(valid_drip_payload):
    FakeGmailClient.provider_rfc_ids = [
        "<provider-first@gmail.com>",
        "<provider-second@gmail.com>",
    ]
    _lead, _enrollment, lane = _domain(valid_drip_payload)
    first, first_task = _queued_delivery(lane)
    handle_drip_gmail(first_task)
    first.refresh_from_db()
    first.sent_at = timezone.now() - timedelta(days=4)
    first.save(update_fields={"sent_at", "updated_at"})

    second, second_task = _queued_delivery(
        lane,
        step_index=1,
        subject="A different later subject is ignored",
        body="Second frozen body",
    )
    second.scheduled_at = first.sent_at
    second.save(update_fields={"scheduled_at", "updated_at"})
    handle_drip_gmail(second_task)

    second.refresh_from_db()
    call = FakeGmailClient.calls[1]
    assert first.rfc_message_id == "<provider-first@gmail.com>"
    assert call["thread_id"] == "drip-thread-1"
    assert call["subject"] == "A new drip subject"
    assert call["in_reply_to"] == "<provider-first@gmail.com>"
    assert call["references"] == ("<provider-first@gmail.com>",)
    assert second.rfc_message_id == "<provider-second@gmail.com>"
    assert second.rfc_references == "<provider-first@gmail.com>"


def test_drip_gmail_does_not_send_before_manifest_delay(valid_drip_payload):
    _lead, _enrollment, lane = _domain(valid_drip_payload)
    first, first_task = _queued_delivery(lane)
    handle_drip_gmail(first_task)
    first.refresh_from_db()

    second, second_task = _queued_delivery(lane, step_index=1)
    second.scheduled_at = timezone.now() - timedelta(seconds=1)
    second.save(update_fields={"scheduled_at", "updated_at"})

    handle_drip_gmail(second_task)

    second.refresh_from_db()
    assert second.status == DripDelivery.Status.PLANNED
    assert second.current_task is None
    assert second.scheduled_at >= first.sent_at + timedelta(days=3)
    assert len(FakeGmailClient.calls) == 1


def test_drip_gmail_known_stop_globally_stops_without_attempt(valid_drip_payload):
    lead, enrollment, lane = _domain(valid_drip_payload)
    delivery, task = _queued_delivery(lane)
    Message.objects.create(
        lead=lead,
        source=Message.Source.GMAIL,
        direction=Message.Direction.INBOUND,
        external_id="arian_boundera:known-reply",
        sender=lead.email,
        body="Stop",
        sent_at=timezone.now(),
    )

    handle_drip_gmail(task)

    delivery.refresh_from_db()
    enrollment.refresh_from_db()
    lane.refresh_from_db()
    assert delivery.status == DripDelivery.Status.STOPPED
    assert enrollment.status == DripEnrollment.Status.STOPPED
    assert lane.status == DripLane.Status.STOPPED
    assert not delivery.attempts.exists()
    assert FakeGmailClient.calls == []


def test_drip_gmail_pause_releases_delivery_for_resume(valid_drip_payload):
    _lead, enrollment, lane = _domain(valid_drip_payload)
    delivery, task = _queued_delivery(lane)
    enrollment.status = DripEnrollment.Status.PAUSED
    enrollment.save(update_fields={"status", "updated_at"})

    handle_drip_gmail(task)

    delivery.refresh_from_db()
    assert delivery.status == DripDelivery.Status.PLANNED
    assert delivery.current_task is None
    assert not delivery.attempts.exists()


def test_drip_gmail_stop_race_before_submission_is_not_submitted(valid_drip_payload):
    lead, _enrollment, lane = _domain(valid_drip_payload)
    delivery, task = _queued_delivery(lane)

    def persist_reply():
        Message.objects.create(
            lead=lead,
            source=Message.Source.GMAIL,
            direction=Message.Direction.INBOUND,
            external_id="arian_boundera:racing-reply",
            sender=lead.email,
            body="Stop before send",
            sent_at=timezone.now(),
        )

    FakeGmailClient.before_callback = persist_reply

    with pytest.raises(ValueError, match="stopped before submission"):
        handle_drip_gmail(task)

    delivery.refresh_from_db()
    lane.refresh_from_db()
    attempt = delivery.attempts.get()
    assert delivery.status == DripDelivery.Status.STOPPED
    assert delivery.current_task is None
    assert lane.status == DripLane.Status.STOPPED
    assert attempt.outcome == DripDeliveryAttempt.Outcome.NOT_SUBMITTED
    assert attempt.submission_attempted_at is None
    assert not Message.objects.filter(
        source=Message.Source.GMAIL,
        direction=Message.Direction.OUTBOUND,
    ).exists()


def test_drip_gmail_provider_failure_after_boundary_is_unclear(valid_drip_payload):
    _lead, _enrollment, lane = _domain(valid_drip_payload)
    delivery, task = _queued_delivery(lane)
    FakeGmailClient.fail_after_callback = RuntimeError("provider connection dropped")

    with pytest.raises(RuntimeError, match="provider connection dropped"):
        handle_drip_gmail(task)

    delivery.refresh_from_db()
    lane.refresh_from_db()
    attempt = delivery.attempts.get()
    assert delivery.status == DripDelivery.Status.UNCLEAR
    assert delivery.current_task == task
    assert lane.status == DripLane.Status.PAUSED
    assert attempt.outcome == DripDeliveryAttempt.Outcome.UNCLEAR
    assert attempt.submission_attempted_at is not None


def test_drip_gmail_malformed_provider_message_id_is_unclear(valid_drip_payload):
    _lead, _enrollment, lane = _domain(valid_drip_payload)
    delivery, task = _queued_delivery(lane)
    FakeGmailClient.provider_rfc_ids = ["<bad id>"]

    with pytest.raises(ValueError, match="invalid RFC Message-ID"):
        handle_drip_gmail(task)

    delivery.refresh_from_db()
    lane.refresh_from_db()
    attempt = delivery.attempts.get()
    assert delivery.status == DripDelivery.Status.UNCLEAR
    assert lane.status == DripLane.Status.PAUSED
    assert attempt.outcome == DripDeliveryAttempt.Outcome.UNCLEAR


def test_drip_gmail_client_failure_before_boundary_is_retryable(
    valid_drip_payload,
    monkeypatch,
):
    _lead, _enrollment, lane = _domain(valid_drip_payload)
    delivery, task = _queued_delivery(lane)

    class BrokenClient:
        def __init__(self, *, operator):
            raise RuntimeError("OAuth unavailable")

    monkeypatch.setattr("drip.tasks.gmail.GmailClient", BrokenClient)

    with pytest.raises(RuntimeError, match="OAuth unavailable"):
        handle_drip_gmail(task)

    delivery.refresh_from_db()
    attempt = delivery.attempts.get()
    assert delivery.status == DripDelivery.Status.PLANNED
    assert delivery.current_task is None
    assert attempt.outcome == DripDeliveryAttempt.Outcome.NOT_SUBMITTED
    assert attempt.submission_attempted_at is None


def test_stale_drip_gmail_before_boundary_requeues_same_task(valid_drip_payload):
    _lead, _enrollment, lane = _domain(valid_drip_payload)
    delivery, task = _queued_delivery(lane)
    attempt = DripDeliveryAttempt.objects.create(
        delivery=delivery,
        attempt_number=1,
    )
    delivery.status = DripDelivery.Status.SENDING
    delivery.save(update_fields={"status", "updated_at"})

    assert recover_stale_drip_gmail_task(task.pk) is True

    task.refresh_from_db()
    delivery.refresh_from_db()
    attempt.refresh_from_db()
    assert task.status == Task.Status.PENDING
    assert task.started_at is None
    assert delivery.status == DripDelivery.Status.QUEUED
    assert delivery.current_task == task
    assert attempt.outcome == DripDeliveryAttempt.Outcome.NOT_SUBMITTED


def test_stale_drip_gmail_after_boundary_becomes_unclear(valid_drip_payload):
    _lead, _enrollment, lane = _domain(valid_drip_payload)
    delivery, task = _queued_delivery(lane)
    attempt = DripDeliveryAttempt.objects.create(
        delivery=delivery,
        attempt_number=1,
        submission_attempted_at=timezone.now() - timedelta(minutes=45),
    )
    delivery.status = DripDelivery.Status.SENDING
    delivery.save(update_fields={"status", "updated_at"})

    assert recover_stale_drip_gmail_task(task.pk) is True

    task.refresh_from_db()
    delivery.refresh_from_db()
    lane.refresh_from_db()
    attempt.refresh_from_db()
    assert task.status == Task.Status.FAILED
    assert delivery.status == DripDelivery.Status.UNCLEAR
    assert lane.status == DripLane.Status.PAUSED
    assert attempt.outcome == DripDeliveryAttempt.Outcome.UNCLEAR


def test_stale_drip_gmail_preserves_already_finalized_send(valid_drip_payload):
    _lead, _enrollment, lane = _domain(valid_drip_payload)
    delivery, task = _queued_delivery(lane)
    now = timezone.now()
    attempt = DripDeliveryAttempt.objects.create(
        delivery=delivery,
        attempt_number=1,
        outcome=DripDeliveryAttempt.Outcome.SENT,
        submission_attempted_at=now,
        finished_at=now,
    )
    delivery.status = DripDelivery.Status.SENT
    delivery.sent_at = now
    delivery.save(update_fields={"status", "sent_at", "updated_at"})

    assert recover_stale_drip_gmail_task(task.pk) is True

    delivery.refresh_from_db()
    attempt.refresh_from_db()
    task.refresh_from_db()
    assert delivery.status == DripDelivery.Status.SENT
    assert attempt.outcome == DripDeliveryAttempt.Outcome.SENT
    assert task.status == Task.Status.COMPLETED


def test_stale_drip_gmail_does_not_requeue_while_lane_paused(valid_drip_payload):
    _lead, _enrollment, lane = _domain(valid_drip_payload)
    delivery, task = _queued_delivery(lane)
    attempt = DripDeliveryAttempt.objects.create(delivery=delivery, attempt_number=1)
    lane.status = DripLane.Status.PAUSED
    lane.save(update_fields={"status", "updated_at"})
    delivery.status = DripDelivery.Status.SENDING
    delivery.save(update_fields={"status", "updated_at"})

    assert recover_stale_drip_gmail_task(task.pk) is True

    delivery.refresh_from_db()
    attempt.refresh_from_db()
    task.refresh_from_db()
    assert delivery.status == DripDelivery.Status.PLANNED
    assert delivery.current_task_id is None
    assert attempt.outcome == DripDeliveryAttempt.Outcome.NOT_SUBMITTED
    assert task.status == Task.Status.COMPLETED
