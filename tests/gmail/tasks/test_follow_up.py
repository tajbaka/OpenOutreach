from datetime import timedelta

import pytest
from django.utils import timezone

from crm.models import Deal, Lead, Message
from gmail.client import GmailSendResult
from gmail.submission import (
    SUBMISSION_ATTEMPTED_AT_KEY,
    recover_stale_current_gmail_task,
)
from gmail.tasks.follow_up import handle_gmail_follow_up
from linkedin.enums import ProfileState
from linkedin.exceptions import SheetsError
from linkedin.models import Campaign, Task
from tests.factories import UserFactory


@pytest.fixture(autouse=True)
def no_suppression(monkeypatch):
    monkeypatch.setattr("linkedin.suppression.lead_suppression_match", lambda lead: None)
    monkeypatch.setattr("gmail.handoff.ENABLE_GMAIL_SEQUENCE", True)
    FakeGmailClient.calls = []
    FakeGmailClient.send_count = 0
    FakeGmailClient.provider_rfc_ids = []
    FakeGmailClient.fail_after_callback = None


class FakeGmailClient:
    account_key = "arian_boundera"
    send_as = "ariant@getboundera.com"
    reply_to = "ariant@boundera.io"
    calls = []
    send_count = 0
    provider_rfc_ids = []
    fail_after_callback = None

    def __init__(self, *, operator):
        self.operator = operator

    def send_message(self, **kwargs):
        callback = kwargs.get("on_submit_attempt")
        if callback is not None:
            callback()
        if type(self).fail_after_callback is not None:
            raise type(self).fail_after_callback
        type(self).send_count += 1
        type(self).calls.append(kwargs)
        message_id = f"gmail-id-{type(self).send_count}"
        thread_id = kwargs.get("thread_id") or "gmail-thread-1"
        provider_rfc_message_id = (
            type(self).provider_rfc_ids[type(self).send_count - 1]
            if len(type(self).provider_rfc_ids) >= type(self).send_count
            else kwargs["rfc_message_id"]
        )
        return GmailSendResult(
            message_id=message_id,
            thread_id=thread_id,
            rfc_message_id=provider_rfc_message_id,
        )


def _lead(**overrides):
    data = {
        "first_name": "Ada",
        "last_name": "Lovelace",
        "company_name": "Analytical Engines",
        "linkedin_url": "https://www.linkedin.com/in/ada-lovelace/",
        "public_identifier": "ada-lovelace",
        "email": "ada@example.com",
    }
    data.update(overrides)
    return Lead.objects.create(**data)


def _deal(lead):
    user = UserFactory(username="owner")
    campaign = Campaign.objects.create(name="Campaign", user=user)
    return Deal.objects.create(lead=lead, campaign=campaign, state=ProfileState.CONNECTED)


def _task(lead, deal=None, *, status=Task.Status.RUNNING, **payload):
    base = {
        "lead_id": lead.id,
        "operator": "Arian",
        "sequence_name": "gmail_fallback",
        "step_index": 0,
    }
    if deal is not None:
        base["deal_id"] = deal.id
    base.update(payload)
    return Task.objects.create(
        task_type=Task.TaskType.GMAIL_FOLLOW_UP,
        status=status,
        started_at=timezone.now() if status == Task.Status.RUNNING else None,
        scheduled_at=timezone.now() - timedelta(seconds=1),
        payload=base,
    )


@pytest.mark.django_db
def test_gmail_follow_up_sends_and_persists(monkeypatch):
    lead = _lead()
    deal = _deal(lead)
    monkeypatch.setattr("gmail.tasks.follow_up.ENABLE_GMAIL_SEQUENCE", True)
    monkeypatch.setattr("gmail.tasks.follow_up.GmailClient", FakeGmailClient)

    handle_gmail_follow_up(_task(lead, deal))

    msg = Message.objects.get(source=Message.Source.GMAIL, direction=Message.Direction.OUTBOUND)
    assert msg.external_id == "arian_boundera:gmail-id-1"
    assert msg.thread_external_id == "arian_boundera:gmail-thread-1"
    assert msg.sender == "ariant@getboundera.com"
    assert "Hi Ada" in msg.body
    assert "Analytical Engines" in msg.body
    assert msg.raw["gmail_message_id"] == "gmail-id-1"
    assert msg.raw["gmail_thread_id"] == "gmail-thread-1"
    assert msg.raw["gmail_account"] == "arian_boundera"
    assert msg.raw["reply_to"] == "ariant@boundera.io"
    assert msg.raw["rfc_message_id"].startswith("<openoutreach-")
    assert msg.raw["automation_key"] == (
        f"gmail_follow_up:Arian:{lead.id}:gmail_fallback:step-0"
    )


@pytest.mark.django_db
def test_gmail_follow_up_from_finished_campaign_never_builds_client(monkeypatch):
    lead = _lead()
    deal = _deal(lead)
    deal.campaign.status = Campaign.Status.FINISHED
    deal.campaign.save(update_fields=["status"])
    monkeypatch.setattr("gmail.tasks.follow_up.ENABLE_GMAIL_SEQUENCE", True)

    class NoClient:
        def __init__(self, *, operator):
            raise AssertionError("finished current Campaign must not send Gmail")

    monkeypatch.setattr("gmail.tasks.follow_up.GmailClient", NoClient)

    handle_gmail_follow_up(_task(lead, deal))

    assert not Message.objects.filter(source=Message.Source.GMAIL).exists()


@pytest.mark.django_db
def test_gmail_follow_up_stamps_durable_submission_boundary(monkeypatch):
    lead = _lead()
    deal = _deal(lead)
    task = _task(lead, deal)
    monkeypatch.setattr("gmail.tasks.follow_up.ENABLE_GMAIL_SEQUENCE", True)
    monkeypatch.setattr("gmail.tasks.follow_up.GmailClient", FakeGmailClient)

    handle_gmail_follow_up(task)

    task.refresh_from_db()
    assert task.payload[SUBMISSION_ATTEMPTED_AT_KEY]


@pytest.mark.django_db
def test_gmail_follow_up_refuses_retry_after_submission_boundary(monkeypatch):
    lead = _lead()
    deal = _deal(lead)
    task = _task(lead, deal)
    payload = dict(task.payload)
    payload[SUBMISSION_ATTEMPTED_AT_KEY] = timezone.now().isoformat()
    task.payload = payload
    task.save(update_fields={"payload"})
    monkeypatch.setattr("gmail.tasks.follow_up.ENABLE_GMAIL_SEQUENCE", True)

    class NoRetryClient:
        def __init__(self, *, operator):
            raise AssertionError("unclear submission must not build a Gmail client")

    monkeypatch.setattr("gmail.tasks.follow_up.GmailClient", NoRetryClient)

    with pytest.raises(ValueError, match="automatic retry is blocked"):
        handle_gmail_follow_up(task)


@pytest.mark.django_db
def test_gmail_follow_up_failure_after_boundary_stays_nonretryable(monkeypatch):
    lead = _lead()
    deal = _deal(lead)
    task = _task(lead, deal)
    FakeGmailClient.fail_after_callback = RuntimeError("metadata lookup failed")
    monkeypatch.setattr("gmail.tasks.follow_up.ENABLE_GMAIL_SEQUENCE", True)
    monkeypatch.setattr("gmail.tasks.follow_up.GmailClient", FakeGmailClient)

    with pytest.raises(RuntimeError, match="metadata lookup failed"):
        handle_gmail_follow_up(task)

    task.refresh_from_db()
    assert task.payload[SUBMISSION_ATTEMPTED_AT_KEY]
    assert not Message.objects.filter(source=Message.Source.GMAIL).exists()


@pytest.mark.django_db
def test_gmail_follow_up_next_step_delay_starts_after_send(monkeypatch):
    lead = _lead()
    deal = _deal(lead)
    deal.connected_at = timezone.now() - timedelta(days=30)
    deal.save(update_fields=["connected_at"])
    monkeypatch.setattr("gmail.tasks.follow_up.ENABLE_GMAIL_SEQUENCE", True)
    monkeypatch.setattr("gmail.tasks.follow_up.GmailClient", FakeGmailClient)

    seen = {}

    def fake_delay(delay_hours, *, reference_time=None):
        seen["delay_hours"] = delay_hours
        seen["reference_time"] = reference_time
        return 123

    monkeypatch.setattr("gmail.tasks.follow_up._delay_seconds_to_active_due", fake_delay)

    handle_gmail_follow_up(_task(lead, deal))

    next_task = Task.objects.get(task_type=Task.TaskType.GMAIL_FOLLOW_UP, payload__step_index=1)
    sent_message = Message.objects.get(
        source=Message.Source.GMAIL,
        direction=Message.Direction.OUTBOUND,
    )
    assert seen["delay_hours"] == 192
    assert seen["reference_time"] == sent_message.sent_at
    assert next_task.scheduled_at > timezone.now() + timedelta(seconds=100)


@pytest.mark.django_db
def test_gmail_follow_up_uses_persisted_icp(monkeypatch):
    lead = _lead(icp="Channel")
    deal = _deal(lead)
    monkeypatch.setattr("gmail.tasks.follow_up.ENABLE_GMAIL_SEQUENCE", True)
    monkeypatch.setattr("gmail.tasks.follow_up.GmailClient", FakeGmailClient)

    handle_gmail_follow_up(_task(lead, deal))

    msg = Message.objects.get(source=Message.Source.GMAIL, direction=Message.Direction.OUTBOUND)
    assert "vendors around FedRAMP 20x" in msg.body


@pytest.mark.django_db
def test_gmail_follow_up_stops_on_persisted_gmail_reply_without_deal(monkeypatch):
    lead = _lead()
    monkeypatch.setattr("gmail.tasks.follow_up.ENABLE_GMAIL_SEQUENCE", True)
    Message.objects.create(
        lead=lead,
        source=Message.Source.GMAIL,
        direction=Message.Direction.INBOUND,
        external_id="arian_boundera:reply-1",
        thread_external_id="arian_boundera:gmail-thread-1",
        sender=lead.email,
        body="reply",
        sent_at=timezone.now(),
    )

    class NoClient:
        def __init__(self, *, operator):
            raise AssertionError("persisted stop should block before Gmail auth")

    monkeypatch.setattr("gmail.tasks.follow_up.GmailClient", NoClient)

    handle_gmail_follow_up(_task(lead))

    assert not Message.objects.filter(source=Message.Source.GMAIL, direction=Message.Direction.OUTBOUND).exists()


@pytest.mark.django_db
def test_gmail_follow_up_dedup_skips_existing_step(monkeypatch):
    lead = _lead()
    deal = _deal(lead)
    monkeypatch.setattr("gmail.tasks.follow_up.ENABLE_GMAIL_SEQUENCE", True)
    Message.objects.create(
        lead=lead,
        source=Message.Source.GMAIL,
        direction=Message.Direction.OUTBOUND,
        external_id=f"gmail-send:Arian:{lead.id}:gmail_fallback:step-0:old",
        sender="ariant@getboundera.com",
        body="already sent",
        sent_at=timezone.now(),
    )

    class NoSendClient(FakeGmailClient):
        def send_message(self, **kwargs):
            raise AssertionError("should not send duplicate step")

    monkeypatch.setattr("gmail.tasks.follow_up.GmailClient", NoSendClient)

    handle_gmail_follow_up(_task(lead, deal))


@pytest.mark.django_db
def test_gmail_follow_up_continues_real_thread_and_retains_subject(monkeypatch):
    FakeGmailClient.provider_rfc_ids = [
        "<provider-first@gmail.com>",
        "<provider-second@gmail.com>",
    ]
    lead = _lead()
    deal = _deal(lead)
    monkeypatch.setattr("gmail.tasks.follow_up.ENABLE_GMAIL_SEQUENCE", True)
    monkeypatch.setattr("gmail.tasks.follow_up.GmailClient", FakeGmailClient)

    handle_gmail_follow_up(_task(lead, deal))
    step_one_task = Task.objects.get(
        task_type=Task.TaskType.GMAIL_FOLLOW_UP,
        payload__step_index=1,
    )
    step_one_task.status = Task.Status.RUNNING
    step_one_task.started_at = timezone.now()
    step_one_task.save(update_fields={"status", "started_at"})
    handle_gmail_follow_up(step_one_task)

    assert len(FakeGmailClient.calls) == 2
    first_call, second_call = FakeGmailClient.calls
    assert first_call["thread_id"] == ""
    assert first_call["in_reply_to"] == ""
    assert first_call["references"] == ()
    assert second_call["thread_id"] == "gmail-thread-1"
    assert second_call["in_reply_to"] == "<provider-first@gmail.com>"
    assert second_call["references"] == ("<provider-first@gmail.com>",)
    assert second_call["subject"] == first_call["subject"]

    messages = list(
        Message.objects.filter(
            source=Message.Source.GMAIL,
            direction=Message.Direction.OUTBOUND,
        ).order_by("sent_at", "pk")
    )
    assert [message.external_id for message in messages] == [
        "arian_boundera:gmail-id-1",
        "arian_boundera:gmail-id-2",
    ]
    assert {message.thread_external_id for message in messages} == {
        "arian_boundera:gmail-thread-1",
    }
    assert messages[1].raw["references"] == [messages[0].raw["rfc_message_id"]]
    assert messages[1].raw["thread_subject"] == messages[0].raw["thread_subject"]


@pytest.mark.django_db
def test_gmail_follow_up_continuation_fails_closed_without_exact_binding(monkeypatch):
    lead = _lead()
    deal = _deal(lead)
    monkeypatch.setattr("gmail.tasks.follow_up.ENABLE_GMAIL_SEQUENCE", True)
    monkeypatch.setattr("gmail.tasks.follow_up.GmailClient", FakeGmailClient)
    Message.objects.create(
        lead=lead,
        source=Message.Source.GMAIL,
        direction=Message.Direction.OUTBOUND,
        external_id=f"gmail-send:Arian:{lead.id}:gmail_fallback:step-0:legacy",
        sender="ariant@getboundera.com",
        body="legacy send with no provider thread binding",
        sent_at=timezone.now(),
    )

    with pytest.raises(ValueError, match="stored Gmail message"):
        handle_gmail_follow_up(_task(lead, deal, step_index=1))

    assert FakeGmailClient.calls == []


@pytest.mark.django_db
def test_gmail_follow_up_rechecks_persisted_stop_immediately_before_send(monkeypatch):
    lead = _lead()
    deal = _deal(lead)
    monkeypatch.setattr("gmail.tasks.follow_up.ENABLE_GMAIL_SEQUENCE", True)
    monkeypatch.setattr("gmail.tasks.follow_up.GmailClient", FakeGmailClient)
    reasons = iter(("", "Lead replied; automation stopped"))
    monkeypatch.setattr(
        "gmail.tasks.follow_up.lead_automation_stop_reason",
        lambda lead: next(reasons),
    )

    handle_gmail_follow_up(_task(lead, deal))

    assert FakeGmailClient.calls == []
    assert not Message.objects.filter(source=Message.Source.GMAIL).exists()


@pytest.mark.django_db
def test_gmail_follow_up_dedup_heals_missing_next_task(monkeypatch):
    lead = _lead()
    deal = _deal(lead)
    monkeypatch.setattr("gmail.tasks.follow_up.ENABLE_GMAIL_SEQUENCE", True)
    monkeypatch.setattr("gmail.tasks.follow_up.GmailClient", FakeGmailClient)
    original_task = _task(lead, deal)

    handle_gmail_follow_up(original_task)
    Task.objects.filter(payload__step_index=1).delete()
    handle_gmail_follow_up(original_task)

    assert len(FakeGmailClient.calls) == 1
    assert Task.objects.filter(
        task_type=Task.TaskType.GMAIL_FOLLOW_UP,
        payload__step_index=1,
    ).count() == 1


@pytest.mark.django_db
def test_stale_post_send_task_requeues_and_heals_successor_without_resend(
    monkeypatch,
):
    lead = _lead()
    deal = _deal(lead)
    monkeypatch.setattr("gmail.tasks.follow_up.ENABLE_GMAIL_SEQUENCE", True)
    monkeypatch.setattr("gmail.tasks.follow_up.GmailClient", FakeGmailClient)
    task = _task(lead, deal)

    handle_gmail_follow_up(task)
    Task.objects.filter(payload__step_index=1).delete()
    sent_message = Message.objects.get(
        source=Message.Source.GMAIL,
        direction=Message.Direction.OUTBOUND,
    )
    task.status = Task.Status.RUNNING
    task.started_at = timezone.now() - timedelta(hours=1)
    task.save(update_fields={"status", "started_at"})

    assert recover_stale_current_gmail_task(task.pk) is True
    task.refresh_from_db()
    assert task.status == Task.Status.PENDING

    seen = {}

    def fake_delay(delay_hours, *, reference_time=None):
        seen["delay_hours"] = delay_hours
        seen["reference_time"] = reference_time
        return 123

    monkeypatch.setattr("gmail.tasks.follow_up._delay_seconds_to_active_due", fake_delay)
    handle_gmail_follow_up(task)

    assert len(FakeGmailClient.calls) == 1
    assert seen["reference_time"] == sent_message.sent_at
    assert Task.objects.filter(
        task_type=Task.TaskType.GMAIL_FOLLOW_UP,
        payload__step_index=1,
    ).count() == 1


@pytest.mark.django_db
def test_gmail_follow_up_rejects_malformed_stored_references(monkeypatch):
    lead = _lead()
    deal = _deal(lead)
    monkeypatch.setattr("gmail.tasks.follow_up.ENABLE_GMAIL_SEQUENCE", True)
    monkeypatch.setattr("gmail.tasks.follow_up.GmailClient", FakeGmailClient)
    Message.objects.create(
        lead=lead,
        source=Message.Source.GMAIL,
        direction=Message.Direction.OUTBOUND,
        external_id="arian_boundera:provider-step-0",
        sender="ariant@getboundera.com",
        body="already sent",
        sent_at=timezone.now(),
        thread_external_id="arian_boundera:gmail-thread-1",
        raw={
            "automation_key": (
                f"gmail_follow_up:Arian:{lead.pk}:gmail_fallback:step-0"
            ),
            "gmail_account": "arian_boundera",
            "send_as": "ariant@getboundera.com",
            "gmail_thread_id": "gmail-thread-1",
            "rfc_message_id": "<provider-step-0@gmail.com>",
            "thread_subject": "Original subject",
            "references": ["not-an-rfc-message-id"],
        },
    )

    with pytest.raises(ValueError, match="References metadata is invalid"):
        handle_gmail_follow_up(_task(lead, deal, step_index=1))

    assert FakeGmailClient.calls == []


@pytest.mark.django_db
def test_gmail_follow_up_disabled_noops(monkeypatch):
    lead = _lead()
    deal = _deal(lead)
    monkeypatch.setattr("gmail.tasks.follow_up.ENABLE_GMAIL_SEQUENCE", False)

    class NoSendClient(FakeGmailClient):
        def __init__(self, *, operator):
            raise AssertionError("disabled handler should not build Gmail client")

    monkeypatch.setattr("gmail.tasks.follow_up.GmailClient", NoSendClient)

    handle_gmail_follow_up(_task(lead, deal))

    assert not Message.objects.filter(source=Message.Source.GMAIL).exists()


@pytest.mark.django_db
def test_gmail_follow_up_missing_sender_icp_skips_without_client(monkeypatch):
    lead = _lead(icp="CSPs")
    deal = _deal(lead)
    monkeypatch.setattr("gmail.tasks.follow_up.ENABLE_GMAIL_SEQUENCE", True)

    class NoClient:
        def __init__(self, *, operator):
            raise AssertionError("missing template should skip before Gmail client")

    monkeypatch.setattr("gmail.tasks.follow_up.GmailClient", NoClient)

    handle_gmail_follow_up(_task(lead, deal, operator="Missing"))

    assert not Message.objects.filter(source=Message.Source.GMAIL).exists()


@pytest.mark.django_db
def test_gmail_follow_up_blank_template_skips_without_client(monkeypatch):
    lead = _lead(icp="CSPs")
    deal = _deal(lead)
    monkeypatch.setattr("gmail.tasks.follow_up.ENABLE_GMAIL_SEQUENCE", True)
    monkeypatch.setattr(
        "gmail.tasks.follow_up.render_for_icp",
        lambda **kwargs: (_ for _ in ()).throw(SheetsError("gmail templates: step 0 needs body_variants")),
    )

    class NoClient:
        def __init__(self, *, operator):
            raise AssertionError("blank template should skip before Gmail client")

    monkeypatch.setattr("gmail.tasks.follow_up.GmailClient", NoClient)

    handle_gmail_follow_up(_task(lead, deal))

    assert not Message.objects.filter(source=Message.Source.GMAIL).exists()
