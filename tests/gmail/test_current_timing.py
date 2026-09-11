"""Real current-Gmail scheduling and submission guards with stub providers."""
from datetime import datetime, timedelta, timezone as dt_timezone

import pytest
from django.utils import timezone
from django.db import connection

from crm.models import Message
from gmail.handoff import maybe_schedule_gmail_sequence
from gmail.submission import (
    SUBMISSION_ATTEMPTED_AT_KEY, defer_early_current_gmail_task,
    recover_stale_current_gmail_task, reschedule_persisted_current_gmail_task,
    stamp_submission_attempt,
)
from gmail.tasks.follow_up import handle_gmail_follow_up
from gmail.worker import GmailWorker
from linkedin.models import Campaign, OutboundDelivery, Task
from tests.gmail.test_versioned_delivery import FrozenGmailClient, _deal


pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def configured_gmail(monkeypatch):
    monkeypatch.setattr("gmail.handoff.ENABLE_GMAIL_SEQUENCE", True)
    monkeypatch.setattr("gmail.tasks.follow_up.ENABLE_GMAIL_SEQUENCE", True)
    monkeypatch.setattr("linkedin.suppression.lead_suppression_match", lambda lead: None)
    monkeypatch.setattr("linkedin.tasks.follow_up.ENABLE_ACTIVE_HOURS", False)
    monkeypatch.setattr("gmail.tasks.follow_up.GmailClient", FrozenGmailClient)
    FrozenGmailClient.calls = []
    FrozenGmailClient.fail_after_callback = None


def _queued():
    deal, _version = _deal()
    task = maybe_schedule_gmail_sequence(deal=deal, operator="Arian")
    delivery = OutboundDelivery.objects.get(pk=task.payload["delivery_id"])
    task.mark_running()
    return deal, task, delivery


@pytest.mark.parametrize("later_field", ["task", "delivery"])
def test_early_claim_defers_same_task_without_changing_frozen_copy(later_field):
    _deal_row, task, delivery = _queued()
    future = timezone.now() + timedelta(days=2)
    frozen = (delivery.frozen_subject, delivery.frozen_body, delivery.render_hash)
    model, pk = (Task, task.pk) if later_field == "task" else (OutboundDelivery, delivery.pk)
    model.objects.filter(pk=pk).update(scheduled_at=future)
    # Deliberately leave the caller's object stale: guards must read the DB.
    handle_gmail_follow_up(task)
    task.refresh_from_db()
    delivery.refresh_from_db()
    assert task.status == Task.Status.PENDING
    assert task.scheduled_at == future
    assert delivery.task_id == task.pk
    assert delivery.status == OutboundDelivery.Status.QUEUED
    assert (delivery.frozen_subject, delivery.frozen_body, delivery.render_hash) == frozen
    assert SUBMISSION_ATTEMPTED_AT_KEY not in task.payload
    assert Task.objects.filter(task_type=Task.TaskType.GMAIL_FOLLOW_UP).count() == 1
    assert not FrozenGmailClient.calls


def test_final_callback_rechecks_future_task_and_does_not_mark_submission(monkeypatch):
    _deal_row, task, delivery = _queued()
    future = timezone.now() + timedelta(days=1)

    class DeferredClient(FrozenGmailClient):
        def send_message(self, **kwargs):
            Task.objects.filter(pk=task.pk).update(scheduled_at=future)
            return super().send_message(**kwargs)

    monkeypatch.setattr("gmail.tasks.follow_up.GmailClient", DeferredClient)
    handle_gmail_follow_up(task)
    task.refresh_from_db()
    delivery.refresh_from_db()
    assert task.status == Task.Status.PENDING
    assert task.scheduled_at == future
    assert delivery.status == OutboundDelivery.Status.QUEUED
    assert SUBMISSION_ATTEMPTED_AT_KEY not in task.payload
    assert not Message.objects.filter(source=Message.Source.GMAIL).exists()


def test_stamp_alone_rechecks_frozen_delivery_time():
    _deal_row, task, delivery = _queued()
    future = timezone.now() + timedelta(days=1)
    OutboundDelivery.objects.filter(pk=delivery.pk).update(scheduled_at=future)
    assert stamp_submission_attempt(task) is False
    task.refresh_from_db()
    delivery.refresh_from_db()
    assert task.status == Task.Status.PENDING
    assert task.scheduled_at == future
    assert delivery.status == OutboundDelivery.Status.QUEUED
    assert SUBMISSION_ATTEMPTED_AT_KEY not in task.payload


@pytest.mark.parametrize("mode", ["post_acceptance", "invitation_sent"])
def test_sender_window_only_applies_to_new_invitation_mode(monkeypatch, mode):
    # Thursday 19:00 Toronto. The default campaign must keep its old cadence.
    now = datetime(2026, 9, 10, 23, tzinfo=dt_timezone.utc)
    monkeypatch.setattr("django.utils.timezone.now", lambda: now)
    deal, task, delivery = _queued()
    Campaign.objects.filter(pk=deal.campaign_id).update(gmail_start_mode=mode)
    monkeypatch.setattr("linkedin.tasks.follow_up.ENABLE_ACTIVE_HOURS", True)
    monkeypatch.setattr("linkedin.tasks.follow_up.ACTIVE_TIMEZONE", "America/Toronto")
    monkeypatch.setattr("linkedin.tasks.follow_up.ACTIVE_START_HOUR", 9)
    monkeypatch.setattr("linkedin.tasks.follow_up.ACTIVE_END_HOUR", 17)
    monkeypatch.setattr("linkedin.tasks.follow_up.REST_DAYS", {5, 6})

    handle_gmail_follow_up(task)
    task.refresh_from_db()
    delivery.refresh_from_db()
    if mode == "invitation_sent":
        assert task.status == Task.Status.PENDING
        assert task.scheduled_at == datetime(2026, 9, 11, 13, tzinfo=dt_timezone.utc)
        assert delivery.status == OutboundDelivery.Status.QUEUED
        assert not FrozenGmailClient.calls
    else:
        assert delivery.status == OutboundDelivery.Status.SENT
        assert len(FrozenGmailClient.calls) == 1


def test_worker_preserves_same_task_deferral(monkeypatch):
    _deal_row, task, delivery = _queued()
    future = timezone.now() + timedelta(hours=1)
    Task.objects.filter(pk=task.pk).update(status=Task.Status.PENDING)
    OutboundDelivery.objects.filter(pk=delivery.pk).update(scheduled_at=future)
    assert GmailWorker(account_key="arian_boundera")._run_once() is True
    task.refresh_from_db()
    assert task.status == Task.Status.PENDING
    assert task.completed_at is None
    assert task.scheduled_at == future
    assert not FrozenGmailClient.calls


@pytest.mark.parametrize("stop", ["campaign", "reply", "disqualified"])
def test_final_callback_catches_stop_during_provider_preparation(monkeypatch, stop):
    deal, task, delivery = _queued()

    class StoppedClient(FrozenGmailClient):
        def send_message(self, **kwargs):
            if stop == "campaign":
                Campaign.objects.filter(pk=deal.campaign_id).update(status=Campaign.Status.DISABLED)
            elif stop == "reply":
                Message.objects.create(
                    lead=deal.lead, source=Message.Source.LINKEDIN,
                    direction=Message.Direction.INBOUND, external_id="new-reply",
                    sender="Ada", body="Let's talk", sent_at=timezone.now(),
                )
            else:
                type(deal.lead).objects.filter(pk=deal.lead_id).update(disqualified=True)
            return super().send_message(**kwargs)

    monkeypatch.setattr("gmail.tasks.follow_up.GmailClient", StoppedClient)
    with pytest.raises(ValueError, match="before submission"):
        handle_gmail_follow_up(task)
    task.refresh_from_db()
    delivery.refresh_from_db()
    assert SUBMISSION_ATTEMPTED_AT_KEY not in task.payload
    assert delivery.status == OutboundDelivery.Status.STOPPED
    assert not Message.objects.filter(source=Message.Source.GMAIL).exists()


def test_uncertain_send_is_held_even_when_task_is_early():
    _deal_row, task, delivery = _queued()
    OutboundDelivery.objects.filter(pk=delivery.pk).update(status=OutboundDelivery.Status.SENDING)
    Task.objects.filter(pk=task.pk).update(scheduled_at=timezone.now() + timedelta(days=2))
    with pytest.raises(ValueError, match="unresolved submission attempt"):
        handle_gmail_follow_up(task)
    delivery.refresh_from_db()
    assert delivery.status == OutboundDelivery.Status.UNCLEAR
    assert not FrozenGmailClient.calls


@pytest.mark.parametrize("action", ["early", "stamp", "recover", "post_send_recover"])
def test_current_submission_and_recovery_lock_lead_before_task(action):
    _deal_row, task, _delivery = _queued()
    statements = []

    def record(execute, sql, params, many, context):
        if "FOR UPDATE" in sql:
            statements.append(sql)
        return execute(sql, params, many, context)

    with connection.execute_wrapper(record):
        if action == "early":
            defer_early_current_gmail_task(task)
        elif action == "stamp":
            stamp_submission_attempt(task)
        elif action == "recover":
            recover_stale_current_gmail_task(task.pk)
        else:
            reschedule_persisted_current_gmail_task(task.pk)
    assert 'FROM "crm_lead"' in statements[0]
    assert 'FROM "linkedin_task"' in statements[1]
