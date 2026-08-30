from datetime import timedelta

import pytest
from django.utils import timezone

from crm.models import Lead, Message
from linkedin.enrichment.base import EnrichmentResult, EnrichmentStatus
from linkedin.models import Task
from gmail.tasks.enrich_email import handle_enrich_email


def _lead(**overrides):
    data = {
        "first_name": "Ada",
        "last_name": "Lovelace",
        "company_name": "Analytical Engines",
        "linkedin_url": "https://www.linkedin.com/in/ada-lovelace/",
        "public_identifier": "ada-lovelace",
    }
    data.update(overrides)
    return Lead.objects.create(**data)


def _task(lead, **payload):
    base = {
        "lead_id": lead.id,
        "operator": "Arian",
        "sequence_name": "gmail_fallback",
        "step_index": 0,
        "bettercontact_email_request_id": "",
    }
    base.update(payload)
    return Task.objects.create(
        task_type=Task.TaskType.ENRICH_EMAIL,
        status=Task.Status.PENDING,
        scheduled_at=timezone.now() - timedelta(seconds=1),
        payload=base,
    )


@pytest.mark.django_db
def test_enrich_email_found_saves_email_and_queues_gmail(monkeypatch):
    monkeypatch.setattr("gmail.handoff.ENABLE_GMAIL_SEQUENCE", True)
    lead = _lead()

    monkeypatch.setattr(
        "gmail.tasks.enrich_email.BetterContactEmailProvider.enrich",
        lambda self, lead, task: EnrichmentResult(
            status=EnrichmentStatus.FOUND,
            provider="bettercontact",
            email="Ada@Example.com",
        ),
    )

    result = handle_enrich_email(_task(lead))

    lead.refresh_from_db()
    assert result.status == EnrichmentStatus.FOUND
    assert lead.email == "ada@example.com"
    assert lead.email_providers_tried == ["bettercontact"]
    gmail = Task.objects.get(task_type=Task.TaskType.GMAIL_FOLLOW_UP)
    assert gmail.payload["lead_id"] == lead.id
    assert gmail.payload["operator"] == "Arian"
    assert gmail.payload["step_index"] == 0


@pytest.mark.django_db
def test_enrich_email_not_found_records_tried_without_gmail(monkeypatch):
    monkeypatch.setattr("gmail.handoff.ENABLE_GMAIL_SEQUENCE", True)
    lead = _lead()
    monkeypatch.setattr(
        "gmail.tasks.enrich_email.BetterContactEmailProvider.enrich",
        lambda self, lead, task: EnrichmentResult(
            status=EnrichmentStatus.NOT_FOUND,
            provider="bettercontact",
        ),
    )

    result = handle_enrich_email(_task(lead))

    lead.refresh_from_db()
    assert result.status == EnrichmentStatus.NOT_FOUND
    assert lead.email == ""
    assert lead.email_providers_tried == ["bettercontact"]
    assert not Task.objects.filter(task_type=Task.TaskType.GMAIL_FOLLOW_UP).exists()


@pytest.mark.django_db
def test_enrich_email_api_failure_stays_retryable(monkeypatch):
    lead = _lead()
    monkeypatch.setattr(
        "gmail.tasks.enrich_email.BetterContactEmailProvider.enrich",
        lambda self, lead, task: EnrichmentResult(
            status=EnrichmentStatus.API_FAILURE,
            provider="bettercontact",
        ),
    )

    result = handle_enrich_email(_task(lead))

    lead.refresh_from_db()
    assert result.status == EnrichmentStatus.API_FAILURE
    assert lead.email_providers_tried == []


@pytest.mark.django_db
def test_enrich_email_existing_email_skips_provider_and_queues_gmail(monkeypatch):
    monkeypatch.setattr("gmail.handoff.ENABLE_GMAIL_SEQUENCE", True)
    lead = _lead(email="existing@example.com")

    called = False

    def _unexpected(self, lead, task):
        nonlocal called
        called = True
        return EnrichmentResult(status=EnrichmentStatus.FOUND, provider="bettercontact")

    monkeypatch.setattr(
        "gmail.tasks.enrich_email.BetterContactEmailProvider.enrich",
        _unexpected,
    )

    result = handle_enrich_email(_task(lead))

    assert result is None
    assert called is False
    assert Task.objects.filter(task_type=Task.TaskType.GMAIL_FOLLOW_UP).exists()


@pytest.mark.django_db
def test_enrich_email_persisted_reply_blocks_provider_without_deal(monkeypatch):
    monkeypatch.setattr("gmail.handoff.ENABLE_GMAIL_SEQUENCE", True)
    lead = _lead()
    Message.objects.create(
        lead=lead,
        source=Message.Source.GMAIL,
        direction=Message.Direction.INBOUND,
        external_id="arian_boundera:reply-before-enrichment",
        sender="ada@example.com",
        body="No thanks",
        sent_at=timezone.now(),
    )

    def _unexpected(self, lead, task):
        raise AssertionError("known stop should block email enrichment")

    monkeypatch.setattr(
        "gmail.tasks.enrich_email.BetterContactEmailProvider.enrich",
        _unexpected,
    )

    assert handle_enrich_email(_task(lead)) is None
    assert not Task.objects.filter(task_type=Task.TaskType.GMAIL_FOLLOW_UP).exists()


@pytest.mark.django_db
def test_enrich_email_reply_during_lookup_blocks_gmail_enqueue(monkeypatch):
    monkeypatch.setattr("gmail.handoff.ENABLE_GMAIL_SEQUENCE", True)
    lead = _lead()

    def _reply_then_find(self, lead, task):
        Message.objects.create(
            lead=lead,
            source=Message.Source.GMAIL,
            direction=Message.Direction.INBOUND,
            external_id="arian_boundera:reply-during-enrichment",
            sender="ada@example.com",
            body="I replied while lookup was running",
            sent_at=timezone.now(),
        )
        return EnrichmentResult(
            status=EnrichmentStatus.FOUND,
            provider="bettercontact",
            email="ada@example.com",
        )

    monkeypatch.setattr(
        "gmail.tasks.enrich_email.BetterContactEmailProvider.enrich",
        _reply_then_find,
    )

    result = handle_enrich_email(_task(lead))

    assert result.status == EnrichmentStatus.FOUND
    assert not Task.objects.filter(task_type=Task.TaskType.GMAIL_FOLLOW_UP).exists()
