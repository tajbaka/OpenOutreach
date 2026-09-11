import hashlib
import json
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from crm.models import Deal, Lead, Message
from gmail.client import GmailSendResult
from gmail.handoff import maybe_schedule_gmail_sequence
from gmail.submission import (
    current_gmail_automation_key,
    recover_stale_current_gmail_task,
    stamp_submission_attempt,
)
from gmail.tasks.enrich_email import handle_enrich_email
from gmail.tasks.follow_up import handle_gmail_follow_up
from linkedin.enrichment.base import EnrichmentResult, EnrichmentStatus
from linkedin.enums import ProfileState
from linkedin.models import (
    Campaign,
    CampaignMessageEnrollment,
    MessageProgram,
    MessageProgramVersion,
    OutboundDelivery,
    Task,
)
from tests.factories import UserFactory


pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def versioned_gmail_enabled(monkeypatch):
    monkeypatch.setattr("gmail.handoff.ENABLE_GMAIL_SEQUENCE", True)
    monkeypatch.setattr("gmail.tasks.follow_up.ENABLE_GMAIL_SEQUENCE", True)
    monkeypatch.setattr("linkedin.suppression.lead_suppression_match", lambda lead: None)
    FrozenGmailClient.calls = []
    FrozenGmailClient.fail_after_callback = None


class FrozenGmailClient:
    account_key = "arian_boundera"
    send_as = "ariant@getboundera.com"
    reply_to = "ariant@boundera.io"
    calls = []
    fail_after_callback = None

    def __init__(self, *, operator):
        self.operator = operator

    def send_message(self, **kwargs):
        callback = kwargs.get("on_submit_attempt")
        if callback is not None:
            callback()
        if type(self).fail_after_callback is not None:
            raise type(self).fail_after_callback
        type(self).calls.append(kwargs)
        number = len(type(self).calls)
        return GmailSendResult(
            message_id=f"versioned-message-{number}",
            thread_id=kwargs.get("thread_id") or "versioned-thread",
            rfc_message_id=kwargs["rfc_message_id"],
        )


def _hash(payload) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _message(
    *,
    step_index: int,
    subject: str,
    body: str,
    delay_hours: int,
    media=None,
):
    return {
        "audience_key": "s-founder-ceo-rev5-maint",
        "role_persona": "S Founder-CEO | REV5-MAINT",
        "fedramp_segment": "REV5-MAINT",
        "channel": "gmail",
        "step_key": f"email-{step_index + 1}",
        "step_index": step_index,
        "delay_hours": delay_hours,
        "variant_key": "control",
        "subject": subject,
        "body": body,
        "media": list(media or []),
        "sender_override": "",
        "notes": "",
    }


def _version(
    program,
    *,
    version: int = 1,
    label: str = "frozen-v1",
    media=None,
    follow_up_media=None,
    starting_step_index: int = 0,
    based_on_version=None,
):
    payload = {
        "program_key": program.key,
        "program_name": program.name,
        "messages": [
            _message(
                step_index=starting_step_index,
                subject=f"{label} {{company_name}}",
                body=f"Hi {{first_name}}, {label} step zero.",
                delay_hours=0,
                media=media,
            ),
            _message(
                step_index=starting_step_index + 1,
                subject=f"{label} follow-up",
                body=f"Hi {{first_name}}, {label} step one.",
                delay_hours=192,
                media=follow_up_media,
            ),
        ],
    }
    return MessageProgramVersion.objects.create(
        program=program,
        version=version,
        schema_version=1,
        payload=payload,
        content_hash=_hash(payload),
        published_by="reviewer@example.com",
        based_on_version=based_on_version,
    )


def _deal(
    *,
    email="ada@example.com",
    media=None,
    follow_up_media=None,
    starting_step_index: int = 0,
):
    program = MessageProgram.objects.create(
        key="marketplace-csp",
        name="Marketplace CSP",
    )
    version = _version(
        program,
        media=media,
        follow_up_media=follow_up_media,
        starting_step_index=starting_step_index,
    )
    campaign = Campaign.objects.create(
        name="Versioned Gmail Campaign",
        user=UserFactory(),
        active_message_version=version,
    )
    lead = Lead.objects.create(
        first_name="Ada",
        last_name="Lovelace",
        company_name="Analytical Engines",
        linkedin_url="https://www.linkedin.com/in/ada-versioned/",
        public_identifier="ada-versioned",
        email=email,
        icp="S Founder-CEO | REV5-MAINT",
    )
    deal = Deal.objects.create(
        lead=lead,
        campaign=campaign,
        state=ProfileState.CONNECTED,
        connected_at=timezone.now(),
    )
    return deal, version


def _run_task(task):
    # Exercise sends at their actual persisted due time. Earlier versions of
    # this fixture bypassed the worker and executed future steps immediately.
    task.refresh_from_db()
    due_at = max(timezone.now(), task.scheduled_at)
    with patch("django.utils.timezone.now", return_value=due_at):
        task.status = Task.Status.RUNNING
        task.started_at = timezone.now()
        task.save(update_fields={"status", "started_at"})
        handle_gmail_follow_up(task)


def test_bound_gmail_freezes_copy_ignores_live_json_drift_and_keeps_version(monkeypatch):
    deal, first_version = _deal()
    monkeypatch.setattr("gmail.tasks.follow_up.GmailClient", FrozenGmailClient)

    first_task = maybe_schedule_gmail_sequence(deal=deal, operator="Arian")

    enrollment = CampaignMessageEnrollment.objects.get(deal=deal)
    first_delivery = OutboundDelivery.objects.get(enrollment=enrollment, step_index=0)
    assert enrollment.audience_key == "s-founder-ceo-rev5-maint"
    assert first_task.payload["delivery_id"] == first_delivery.pk
    assert first_task.payload["message_version_id"] == first_version.pk
    assert first_delivery.task == first_task
    assert first_delivery.status == OutboundDelivery.Status.QUEUED
    assert first_delivery.frozen_body == "Hi Ada, frozen-v1 step zero."

    monkeypatch.setattr(
        "gmail.tasks.follow_up.resolve_icp",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("bound Gmail must not reread Lead.icp")
        ),
    )
    monkeypatch.setattr(
        "gmail.tasks.follow_up.render_for_icp",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("bound Gmail must not reread sender JSON")
        ),
    )
    monkeypatch.setattr(
        "gmail.tasks.follow_up.steps_for_icp",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("bound Gmail must not reread cadence JSON")
        ),
    )

    _run_task(first_task)
    first_delivery.refresh_from_db()
    assert first_delivery.status == OutboundDelivery.Status.SENT

    second_task = Task.objects.get(
        task_type=Task.TaskType.GMAIL_FOLLOW_UP,
        payload__step_index=1,
    )
    second_delivery = OutboundDelivery.objects.get(enrollment=enrollment, step_index=1)
    assert second_task.payload["message_version_id"] == first_version.pk
    assert second_delivery.enrollment == enrollment
    assert second_delivery.frozen_body == "Hi Ada, frozen-v1 step one."

    second_version = _version(
        first_version.program,
        version=2,
        label="changed-v2",
        based_on_version=first_version,
    )
    deal.campaign.active_message_version = second_version
    deal.campaign.save(update_fields={"active_message_version"})
    deal.lead.first_name = "Changed"
    deal.lead.icp = "unrelated-live-route"
    deal.lead.save(update_fields={"first_name", "icp"})

    _run_task(second_task)

    assert [call["body"] for call in FrozenGmailClient.calls] == [
        "Hi Ada, frozen-v1 step zero.",
        "Hi Ada, frozen-v1 step one.",
    ]
    messages = list(Message.objects.filter(source=Message.Source.GMAIL).order_by("pk"))
    assert len(messages) == 2
    for message, delivery in zip(messages, (first_delivery, second_delivery), strict=True):
        assert message.raw["delivery_id"] == delivery.pk
        assert message.raw["message_version_id"] == first_version.pk
        assert message.raw["enrollment_id"] == enrollment.pk
        assert message.raw["audience_key"] == enrollment.audience_key
        assert message.raw["step_key"] == delivery.step_key
        assert message.raw["variant_key"] == delivery.variant_key
        assert message.raw["automation_key"] == (
            f"gmail_follow_up:delivery-{delivery.pk}:version-{first_version.pk}"
        )


def test_bound_gmail_queues_and_executes_the_versions_first_gmail_step(monkeypatch):
    deal, version = _deal(starting_step_index=3)
    monkeypatch.setattr("gmail.tasks.follow_up.GmailClient", FrozenGmailClient)

    task = maybe_schedule_gmail_sequence(deal=deal, operator="Arian")

    delivery = OutboundDelivery.objects.get()
    assert delivery.step_index == 3
    assert task.payload["step_index"] == 3
    assert task.payload["delivery_id"] == delivery.pk
    assert task.payload["message_version_id"] == version.pk

    _run_task(task)
    next_task = Task.objects.get(payload__step_index=4)
    _run_task(next_task)
    assert len(FrozenGmailClient.calls) == 2


def test_bound_gmail_task_without_or_with_mismatched_delivery_fails_closed(monkeypatch):
    deal, version = _deal()

    class NoClient:
        def __init__(self, *, operator):
            raise AssertionError("invalid bound Task must fail before Gmail auth")

    monkeypatch.setattr("gmail.tasks.follow_up.GmailClient", NoClient)
    missing = Task.objects.create(
        task_type=Task.TaskType.GMAIL_FOLLOW_UP,
        status=Task.Status.RUNNING,
        started_at=timezone.now(),
        scheduled_at=timezone.now(),
        payload={
            "lead_id": deal.lead_id,
            "deal_id": deal.pk,
            "operator": "Arian",
            "sequence_name": "gmail_fallback",
            "step_index": 0,
        },
    )
    with pytest.raises(ValueError, match="requires delivery_id"):
        handle_gmail_follow_up(missing)

    proper = maybe_schedule_gmail_sequence(deal=deal, operator="Arian")
    proper.status = Task.Status.RUNNING
    proper.started_at = timezone.now()
    proper.payload = {**proper.payload, "message_version_id": version.pk + 999}
    proper.save(update_fields={"status", "started_at", "payload"})
    with pytest.raises(ValueError, match="does not match enrollment"):
        handle_gmail_follow_up(proper)

    assert not Message.objects.filter(source=Message.Source.GMAIL).exists()


def test_bound_gmail_rejects_tampered_frozen_copy_before_client(monkeypatch):
    deal, _version_row = _deal()
    task = maybe_schedule_gmail_sequence(deal=deal, operator="Arian")
    delivery = OutboundDelivery.objects.get()
    OutboundDelivery.objects.filter(pk=delivery.pk).update(
        frozen_body="tampered after review",
    )

    class NoClient:
        def __init__(self, *, operator):
            raise AssertionError("tampered copy must fail before Gmail auth")

    monkeypatch.setattr("gmail.tasks.follow_up.GmailClient", NoClient)
    task.status = Task.Status.RUNNING
    task.started_at = timezone.now()
    task.save(update_fields={"status", "started_at"})

    with pytest.raises(ValueError, match="render_hash does not match"):
        handle_gmail_follow_up(task)

    assert not Message.objects.filter(source=Message.Source.GMAIL).exists()


def test_bound_email_enrichment_preserves_delivery_identity(monkeypatch):
    deal, version = _deal(email="")
    enrichment_task = maybe_schedule_gmail_sequence(deal=deal, operator="Arian")
    delivery = OutboundDelivery.objects.get()

    assert enrichment_task.task_type == Task.TaskType.ENRICH_EMAIL
    assert enrichment_task.payload["delivery_id"] == delivery.pk
    assert enrichment_task.payload["message_version_id"] == version.pk
    assert delivery.task_id is None

    monkeypatch.setattr(
        "gmail.tasks.enrich_email.BetterContactEmailProvider.enrich",
        lambda self, lead, task: EnrichmentResult(
            status=EnrichmentStatus.FOUND,
            provider="bettercontact",
            email="Ada@Example.com",
        ),
    )
    result = handle_enrich_email(enrichment_task)

    assert result.status == EnrichmentStatus.FOUND
    gmail_task = Task.objects.get(task_type=Task.TaskType.GMAIL_FOLLOW_UP)
    delivery.refresh_from_db()
    assert gmail_task.payload["delivery_id"] == delivery.pk
    assert gmail_task.payload["message_version_id"] == version.pk
    assert delivery.task == gmail_task
    assert delivery.status == OutboundDelivery.Status.QUEUED


def test_bound_email_enrichment_without_frozen_identity_fails_before_provider(monkeypatch):
    deal, _version_row = _deal(email="")
    enrichment_task = maybe_schedule_gmail_sequence(deal=deal, operator="Arian")
    enrichment_task.payload = {
        key: value
        for key, value in enrichment_task.payload.items()
        if key not in {"delivery_id", "message_version_id"}
    }
    enrichment_task.save(update_fields={"payload"})

    monkeypatch.setattr(
        "gmail.tasks.enrich_email.BetterContactEmailProvider.enrich",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("invalid bound enrichment must fail before provider")
        ),
    )
    with pytest.raises(ValueError, match="requires delivery_id"):
        handle_enrich_email(enrichment_task)


def test_bound_gmail_known_stop_marks_frozen_delivery_stopped(monkeypatch):
    deal, _version_row = _deal()
    task = maybe_schedule_gmail_sequence(deal=deal, operator="Arian")
    delivery = OutboundDelivery.objects.get()
    Message.objects.create(
        lead=deal.lead,
        source=Message.Source.GMAIL,
        direction=Message.Direction.INBOUND,
        external_id="arian_boundera:bound-reply",
        sender=deal.lead.email,
        body="No thanks",
        sent_at=timezone.now(),
    )

    class NoClient:
        def __init__(self, *, operator):
            raise AssertionError("known stop must block before Gmail auth")

    monkeypatch.setattr("gmail.tasks.follow_up.GmailClient", NoClient)
    _run_task(task)

    delivery.refresh_from_db()
    assert delivery.status == OutboundDelivery.Status.STOPPED
    assert not Message.objects.filter(
        source=Message.Source.GMAIL,
        direction=Message.Direction.OUTBOUND,
    ).exists()


@pytest.mark.parametrize(
    "terminal_status",
    [OutboundDelivery.Status.STOPPED, OutboundDelivery.Status.UNCLEAR],
)
def test_bound_gmail_terminal_delivery_never_executes(monkeypatch, terminal_status):
    deal, _version_row = _deal()
    task = maybe_schedule_gmail_sequence(deal=deal, operator="Arian")
    delivery = OutboundDelivery.objects.get()
    from linkedin.message_delivery_runtime import mark_delivery_status

    mark_delivery_status(delivery, terminal_status)

    class NoClient:
        def __init__(self, *, operator):
            raise AssertionError("terminal delivery must fail before Gmail auth")

    monkeypatch.setattr("gmail.tasks.follow_up.GmailClient", NoClient)
    with pytest.raises(ValueError, match="is not executable"):
        _run_task(task)

    delivery.refresh_from_db()
    assert delivery.status == terminal_status
    assert not Message.objects.filter(source=Message.Source.GMAIL).exists()


def test_bound_gmail_sent_without_message_evidence_never_resends(monkeypatch):
    deal, _version_row = _deal()
    task = maybe_schedule_gmail_sequence(deal=deal, operator="Arian")
    delivery = OutboundDelivery.objects.get()
    from linkedin.message_delivery_runtime import mark_delivery_status

    mark_delivery_status(delivery, OutboundDelivery.Status.SENT)

    class NoClient:
        def __init__(self, *, operator):
            raise AssertionError("sent delivery without evidence must never resend")

    monkeypatch.setattr("gmail.tasks.follow_up.GmailClient", NoClient)
    with pytest.raises(ValueError, match="marked sent without persisted Message evidence"):
        _run_task(task)

    delivery.refresh_from_db()
    assert delivery.status == OutboundDelivery.Status.SENT
    assert not Message.objects.filter(source=Message.Source.GMAIL).exists()


@pytest.mark.parametrize(
    ("delivery_status", "expected_status"),
    [
        (OutboundDelivery.Status.SENT, OutboundDelivery.Status.SENT),
        (OutboundDelivery.Status.SENDING, OutboundDelivery.Status.UNCLEAR),
    ],
)
def test_bound_gmail_recovery_never_requeues_submission_without_evidence(
    delivery_status,
    expected_status,
):
    deal, _version_row = _deal()
    task = maybe_schedule_gmail_sequence(deal=deal, operator="Arian")
    delivery = OutboundDelivery.objects.get()
    from linkedin.message_delivery_runtime import mark_delivery_status

    mark_delivery_status(delivery, delivery_status)
    task.status = Task.Status.RUNNING
    task.started_at = timezone.now() - timedelta(hours=1)
    task.save(update_fields={"status", "started_at"})

    assert recover_stale_current_gmail_task(task.pk) is True

    task.refresh_from_db()
    delivery.refresh_from_db()
    assert task.status == Task.Status.FAILED
    assert delivery.status == expected_status
    assert not Message.objects.filter(source=Message.Source.GMAIL).exists()


def test_bound_gmail_media_fails_closed_before_queueing():
    deal, _version_row = _deal(media=["brief.pdf"])

    assert maybe_schedule_gmail_sequence(deal=deal, operator="Arian") is None
    delivery = OutboundDelivery.objects.get()
    assert delivery.frozen_media == ["brief.pdf"]
    assert delivery.status == OutboundDelivery.Status.FAILED
    assert not Task.objects.exists()


def test_bound_gmail_follow_up_media_fails_closed_before_queueing(monkeypatch):
    deal, _version_row = _deal(follow_up_media=["follow-up.pdf"])
    task = maybe_schedule_gmail_sequence(deal=deal, operator="Arian")
    first_delivery = OutboundDelivery.objects.get(step_index=0)
    monkeypatch.setattr("gmail.tasks.follow_up.GmailClient", FrozenGmailClient)

    with pytest.raises(ValueError, match="attachments are not supported"):
        _run_task(task)

    first_delivery.refresh_from_db()
    second_delivery = OutboundDelivery.objects.get(step_index=1)
    assert first_delivery.status == OutboundDelivery.Status.SENT
    assert second_delivery.status == OutboundDelivery.Status.FAILED
    assert second_delivery.frozen_media == ["follow-up.pdf"]
    assert second_delivery.task_id is None
    assert Task.objects.filter(task_type=Task.TaskType.GMAIL_FOLLOW_UP).count() == 1


def test_bound_gmail_post_submission_failure_marks_delivery_unclear(monkeypatch):
    deal, _version_row = _deal()
    task = maybe_schedule_gmail_sequence(deal=deal, operator="Arian")
    delivery = OutboundDelivery.objects.get()
    FrozenGmailClient.fail_after_callback = RuntimeError("provider result unavailable")
    monkeypatch.setattr("gmail.tasks.follow_up.GmailClient", FrozenGmailClient)

    task.status = Task.Status.RUNNING
    task.started_at = timezone.now() - timedelta(seconds=1)
    task.save(update_fields={"status", "started_at"})
    with pytest.raises(RuntimeError, match="provider result unavailable"):
        handle_gmail_follow_up(task)

    delivery.refresh_from_db()
    assert delivery.status == OutboundDelivery.Status.UNCLEAR
    assert not Message.objects.filter(source=Message.Source.GMAIL).exists()


def test_bound_gmail_recovery_uses_delivery_identity_and_restores_sent_state():
    deal, version = _deal()
    task = maybe_schedule_gmail_sequence(deal=deal, operator="Arian")
    delivery = OutboundDelivery.objects.get()
    task.status = Task.Status.RUNNING
    task.started_at = timezone.now() - timedelta(hours=1)
    task.save(update_fields={"status", "started_at"})

    stamp_submission_attempt(task)
    delivery.refresh_from_db()
    assert delivery.status == OutboundDelivery.Status.SENDING
    automation_key = current_gmail_automation_key(task.payload)
    assert automation_key == (
        f"gmail_follow_up:delivery-{delivery.pk}:version-{version.pk}"
    )
    sent_at = timezone.now()
    Message.objects.create(
        lead=deal.lead,
        source=Message.Source.GMAIL,
        direction=Message.Direction.OUTBOUND,
        external_id="arian_boundera:recovered-versioned",
        sender="ariant@getboundera.com",
        sent_at=sent_at,
        raw={
            "automation_key": automation_key,
            "delivery_id": delivery.pk,
            "message_version_id": version.pk,
            "enrollment_id": delivery.enrollment_id,
            "audience_key": delivery.enrollment.audience_key,
            "step_key": delivery.step_key,
            "variant_key": delivery.variant_key,
        },
    )

    assert recover_stale_current_gmail_task(task.pk) is True
    task.refresh_from_db()
    delivery.refresh_from_db()
    assert task.status == Task.Status.PENDING
    assert delivery.status == OutboundDelivery.Status.SENT
    assert delivery.sent_at == sent_at


def test_bound_gmail_recovery_rejects_incomplete_message_identity():
    deal, _version_row = _deal()
    task = maybe_schedule_gmail_sequence(deal=deal, operator="Arian")
    delivery = OutboundDelivery.objects.get()
    task.status = Task.Status.RUNNING
    task.started_at = timezone.now() - timedelta(hours=1)
    task.save(update_fields={"status", "started_at"})
    stamp_submission_attempt(task)
    Message.objects.create(
        lead=deal.lead,
        source=Message.Source.GMAIL,
        direction=Message.Direction.OUTBOUND,
        external_id="arian_boundera:incomplete-versioned-evidence",
        sender="ariant@getboundera.com",
        sent_at=timezone.now(),
        raw={"automation_key": current_gmail_automation_key(task.payload)},
    )

    assert recover_stale_current_gmail_task(task.pk) is True

    task.refresh_from_db()
    delivery.refresh_from_db()
    assert task.status == Task.Status.FAILED
    assert delivery.status == OutboundDelivery.Status.UNCLEAR
