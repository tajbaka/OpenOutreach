"""No-send regressions for the September 8 premature startup follow-up."""
from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from django.utils import timezone

from crm.models import Message
from linkedin.daemon import heal_tasks
from linkedin.message_delivery import MessageDeliveryError
from linkedin.message_delivery_runtime import mark_delivery_status
from linkedin.models import ActionLog, OutboundDelivery, Task
from linkedin.tasks.connect import enqueue_follow_up
from linkedin.tasks.follow_up import handle_follow_up
from tests.tasks.test_message_program_runtime import (
    ROLE_PERSONA,
    _connected_deal,
    _version,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def scheduled_session(fake_session, monkeypatch):
    now = timezone.now()
    monkeypatch.setattr("django.utils.timezone.now", lambda: now)
    monkeypatch.setattr("linkedin.conf.ENABLE_FOLLOW_UP", True)
    monkeypatch.setattr("linkedin.daemon.ENABLE_FOLLOW_UP", True)
    monkeypatch.setattr("linkedin.tasks.follow_up.ENABLE_FOLLOW_UP", True)
    fake_session.linkedin_profile.linkedin_username = "ariant@tryfedrampgpt.com"
    fake_session.linkedin_profile.save(update_fields=["linkedin_username"])
    return fake_session


@pytest.mark.parametrize("bound", [False, True])
@pytest.mark.parametrize("hours", [-2, 72])
@pytest.mark.parametrize("status", [Task.Status.PENDING, Task.Status.RUNNING])
def test_startup_preserves_existing_followup(scheduled_session, bound, hours, status):
    session = scheduled_session
    if bound:
        _version(session.campaign)
    deal = _connected_deal(session.campaign)
    task = enqueue_follow_up(session.campaign.pk, deal.lead.public_identifier,
                             operator="Arian", icp=ROLE_PERSONA, step_index=1)
    due = timezone.now() + timedelta(hours=hours)
    task.scheduled_at = due
    task.status = status
    task.started_at = timezone.now() if status == Task.Status.RUNNING else None
    task.save()
    payload = dict(task.payload)
    if bound:
        OutboundDelivery.objects.filter(pk=payload["delivery_id"]).update(scheduled_at=due)

    heal_tasks(session)
    heal_tasks(session)

    task.refresh_from_db()
    assert task.scheduled_at == due
    assert task.status == status
    assert task.payload == payload
    assert Task.objects.filter(task_type=Task.TaskType.FOLLOW_UP).count() == 1
    if bound:
        delivery = OutboundDelivery.objects.get(pk=payload["delivery_id"])
        assert delivery.scheduled_at == due
        assert delivery.task_id == task.pk


def test_startup_missing_first_task_uses_frozen_delivery(scheduled_session):
    _version(scheduled_session.campaign)
    _connected_deal(scheduled_session.campaign)
    heal_tasks(scheduled_session)
    task = Task.objects.get(task_type=Task.TaskType.FOLLOW_UP)
    assert task.payload["step_index"] == 0
    delivery = OutboundDelivery.objects.get(pk=task.payload["delivery_id"])
    assert delivery.task_id == task.pk
    assert delivery.operator == "Arian"
    assert delivery.frozen_body == "Frozen first for Ada at Analytical Engines."


@pytest.mark.parametrize("media", [False, True])
def test_early_claim_requeues_same_delivery_without_sending(scheduled_session, monkeypatch, media):
    session = scheduled_session
    _version(session.campaign, followup_media=["demo.gif"] if media else [])
    deal = _connected_deal(session.campaign)
    step = 0 if media else 1
    task = enqueue_follow_up(session.campaign.pk, deal.lead.public_identifier,
                             operator="Arian", icp=ROLE_PERSONA, step_index=step)
    delivery = OutboundDelivery.objects.get(pk=task.payload["delivery_id"])
    due = timezone.now() + timedelta(days=3)
    OutboundDelivery.objects.filter(pk=delivery.pk).update(scheduled_at=due)
    task.scheduled_at = timezone.now()
    task.mark_running()
    frozen = (delivery.frozen_body, delivery.frozen_media, delivery.render_hash)
    send = MagicMock(return_value=True)
    monkeypatch.setattr("linkedin.actions.message.send_raw_message", send)
    monkeypatch.setattr("linkedin.tasks.follow_up._send_media_follow_up", send)
    count = Message.objects.count()

    handle_follow_up(task, session, {})
    task.mark_completed()  # The real daemon completes a successfully deferred Task.

    send.assert_not_called()
    delivery.refresh_from_db()
    assert delivery.status == OutboundDelivery.Status.QUEUED
    assert delivery.scheduled_at == due
    assert delivery.sent_at is None
    assert (delivery.frozen_body, delivery.frozen_media, delivery.render_hash) == frozen
    next_task = Task.objects.get(pk=delivery.task_id)
    assert next_task.pk != task.pk
    assert next_task.status == Task.Status.PENDING
    assert next_task.scheduled_at == due
    assert next_task.payload["delivery_id"] == delivery.pk
    assert next_task.payload["step_index"] == step
    assert Message.objects.count() == count
    assert not ActionLog.objects.filter(action_type=ActionLog.ActionType.FOLLOW_UP).exists()


@pytest.mark.parametrize("reuse_pending", [False, True])
def test_enqueue_cannot_shorten_existing_delivery_delay(scheduled_session, reuse_pending):
    _version(scheduled_session.campaign)
    deal = _connected_deal(scheduled_session.campaign)
    task = enqueue_follow_up(scheduled_session.campaign.pk, deal.lead.public_identifier,
                             operator="Arian", icp=ROLE_PERSONA, step_index=1)
    delivery = OutboundDelivery.objects.get(pk=task.payload["delivery_id"])
    due = delivery.scheduled_at
    if reuse_pending:
        task.scheduled_at = timezone.now()
        task.save(update_fields=["scheduled_at"])
    else:
        task.mark_completed()
    retry = enqueue_follow_up(scheduled_session.campaign.pk, deal.lead.public_identifier,
                              operator="Arian", delivery_id=delivery.pk, delay_seconds=0)
    delivery.refresh_from_db()
    assert retry.scheduled_at >= due
    assert delivery.scheduled_at >= due
    assert (retry.pk == task.pk) is reuse_pending


def test_future_delivery_cannot_enter_sending(scheduled_session):
    _version(scheduled_session.campaign)
    deal = _connected_deal(scheduled_session.campaign)
    task = enqueue_follow_up(scheduled_session.campaign.pk, deal.lead.public_identifier,
                             operator="Arian", icp=ROLE_PERSONA, step_index=1)
    delivery = OutboundDelivery.objects.get(pk=task.payload["delivery_id"])
    with pytest.raises(MessageDeliveryError, match="before its scheduled time"):
        mark_delivery_status(delivery, OutboundDelivery.Status.SENDING)
    delivery.refresh_from_db()
    assert delivery.status == OutboundDelivery.Status.QUEUED


def test_due_second_followup_still_sends(scheduled_session, monkeypatch):
    _version(scheduled_session.campaign)
    deal = _connected_deal(scheduled_session.campaign)
    task = enqueue_follow_up(scheduled_session.campaign.pk, deal.lead.public_identifier,
                             operator="Arian", icp=ROLE_PERSONA, step_index=1)
    due = timezone.now() - timedelta(seconds=1)
    OutboundDelivery.objects.filter(pk=task.payload["delivery_id"]).update(scheduled_at=due)
    task.scheduled_at = due
    task.mark_running()
    send = MagicMock(return_value=True)
    monkeypatch.setattr("linkedin.actions.message.send_raw_message", send)
    handle_follow_up(task, scheduled_session, {})
    assert send.call_count == 1
    assert OutboundDelivery.objects.get(pk=task.payload["delivery_id"]).status == "sent"


@pytest.mark.parametrize("status", ["sending", "unclear", "stopped"])
def test_startup_does_not_rebind_held_delivery(scheduled_session, status):
    _version(scheduled_session.campaign)
    deal = _connected_deal(scheduled_session.campaign)
    task = enqueue_follow_up(scheduled_session.campaign.pk, deal.lead.public_identifier,
                             operator="Arian", icp=ROLE_PERSONA)
    task.mark_failed("Interrupted attempt held for review")
    OutboundDelivery.objects.filter(pk=task.payload["delivery_id"]).update(status=status)
    heal_tasks(scheduled_session)
    delivery = OutboundDelivery.objects.get(pk=task.payload["delivery_id"])
    assert delivery.status == status
    assert delivery.task_id == task.pk
    assert Task.objects.filter(task_type=Task.TaskType.FOLLOW_UP).count() == 1


def test_interrupted_sending_is_not_automatically_deferred_or_retried(scheduled_session, monkeypatch):
    _version(scheduled_session.campaign)
    deal = _connected_deal(scheduled_session.campaign)
    task = enqueue_follow_up(scheduled_session.campaign.pk, deal.lead.public_identifier,
                             operator="Arian", icp=ROLE_PERSONA, step_index=1)
    task.mark_running()
    OutboundDelivery.objects.filter(pk=task.payload["delivery_id"]).update(status="sending")
    send = MagicMock(return_value=True)
    monkeypatch.setattr("linkedin.actions.message.send_raw_message", send)
    with pytest.raises(MessageDeliveryError, match="manual reconciliation"):
        handle_follow_up(task, scheduled_session, {})
    send.assert_not_called()
    assert OutboundDelivery.objects.get(pk=task.payload["delivery_id"]).status == "sending"
    assert Task.objects.filter(task_type=Task.TaskType.FOLLOW_UP).count() == 1


def test_later_task_only_deferral_is_preserved(scheduled_session, monkeypatch):
    _version(scheduled_session.campaign)
    deal = _connected_deal(scheduled_session.campaign)
    task = enqueue_follow_up(scheduled_session.campaign.pk, deal.lead.public_identifier,
                             operator="Arian", icp=ROLE_PERSONA)
    due = timezone.now() + timedelta(hours=20)
    task.scheduled_at = due
    task.save(update_fields=["scheduled_at"])
    task.mark_running()
    send = MagicMock(return_value=True)
    monkeypatch.setattr("linkedin.actions.message.send_raw_message", send)
    handle_follow_up(task, scheduled_session, {})
    send.assert_not_called()
    delivery = OutboundDelivery.objects.get(pk=task.payload["delivery_id"])
    assert delivery.task.scheduled_at == due
    assert delivery.scheduled_at == due


def test_future_delivery_still_validates_identity_before_deferring(scheduled_session, monkeypatch):
    _version(scheduled_session.campaign)
    deal = _connected_deal(scheduled_session.campaign)
    task = enqueue_follow_up(scheduled_session.campaign.pk, deal.lead.public_identifier,
                             operator="Arian", icp=ROLE_PERSONA, step_index=1)
    task.payload["message_version_id"] += 123
    task.mark_running()
    send = MagicMock(return_value=True)
    monkeypatch.setattr("linkedin.actions.message.send_raw_message", send)
    with pytest.raises(MessageDeliveryError, match="binding mismatch"):
        handle_follow_up(task, scheduled_session, {})
    send.assert_not_called()
    assert Task.objects.filter(task_type=Task.TaskType.FOLLOW_UP).count() == 1
