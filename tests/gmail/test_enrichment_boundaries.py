"""Email lookup is independent of the frozen send clock and shared stops."""
from datetime import timedelta

import pytest
from django.utils import timezone

from crm.models import Message
from gmail.addresses import usable_email
from gmail.handoff import maybe_schedule_gmail_sequence
from gmail.tasks.enrich_email import handle_enrich_email
from linkedin.enrichment.base import EnrichmentResult, EnrichmentStatus
from linkedin.models import Campaign, OutboundDelivery, Task
from tests.gmail.test_versioned_delivery import _deal


pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def configured_gmail(monkeypatch):
    monkeypatch.setattr("gmail.handoff.ENABLE_GMAIL_SEQUENCE", True)
    monkeypatch.setattr("linkedin.suppression.lead_suppression_match", lambda lead: None)
    monkeypatch.setattr("linkedin.tasks.follow_up.ENABLE_ACTIVE_HOURS", False)


@pytest.mark.parametrize("lookup_offset", [timedelta(days=-1), timedelta(days=1)])
def test_lookup_keeps_original_delivery_clock_when_result_is_early_or_late(monkeypatch, lookup_offset):
    deal, _version = _deal(email="")
    task = maybe_schedule_gmail_sequence(deal=deal, operator="Arian")
    delivery = OutboundDelivery.objects.get()
    due = timezone.now() + timedelta(days=2)
    OutboundDelivery.objects.filter(pk=delivery.pk).update(scheduled_at=due)
    frozen = (delivery.frozen_subject, delivery.frozen_body, delivery.render_hash)
    monkeypatch.setattr("django.utils.timezone.now", lambda: due + lookup_offset)
    monkeypatch.setattr(
        "gmail.tasks.enrich_email.BetterContactEmailProvider.enrich",
        lambda *_args: EnrichmentResult(status=EnrichmentStatus.FOUND, provider="bettercontact", email="Ada@example.com"),
    )

    handle_enrich_email(task)
    delivery.refresh_from_db()
    send_task = Task.objects.get(task_type=Task.TaskType.GMAIL_FOLLOW_UP)
    assert delivery.scheduled_at == due
    assert send_task.scheduled_at == due
    assert send_task.payload["delivery_id"] == delivery.pk
    assert delivery.task_id == send_task.pk
    assert (delivery.frozen_subject, delivery.frozen_body, delivery.render_hash) == frozen
    assert not Message.objects.filter(source=Message.Source.GMAIL).exists()


@pytest.mark.parametrize("stop", ["linkedin_reply", "gmail_reply", "campaign", "disqualified"])
def test_stop_recorded_during_lookup_wins_before_saving_or_enqueue(monkeypatch, stop):
    deal, _version = _deal(email="")
    task = maybe_schedule_gmail_sequence(deal=deal, operator="Arian")

    def lookup(_provider, lead, _task):
        if stop in {"linkedin_reply", "gmail_reply"}:
            Message.objects.create(
                lead=lead,
                source=Message.Source.LINKEDIN if stop == "linkedin_reply" else Message.Source.GMAIL,
                direction=Message.Direction.INBOUND,
                external_id="received-during-lookup", sender="Ada", body="Let's talk",
                sent_at=timezone.now(),
            )
        elif stop == "campaign":
            Campaign.objects.filter(pk=deal.campaign_id).update(status=Campaign.Status.DISABLED)
        else:
            type(lead).objects.filter(pk=lead.pk).update(disqualified=True)
        return EnrichmentResult(status=EnrichmentStatus.FOUND, provider="bettercontact", email="ada@example.com")

    monkeypatch.setattr("gmail.tasks.enrich_email.BetterContactEmailProvider.enrich", lookup)
    assert handle_enrich_email(task) is None
    deal.lead.refresh_from_db()
    task.refresh_from_db()
    assert deal.lead.email == ""
    assert deal.lead.email_providers_tried == []
    assert not Task.objects.filter(task_type=Task.TaskType.GMAIL_FOLLOW_UP).exists()
    assert OutboundDelivery.objects.get().status == OutboundDelivery.Status.STOPPED
    assert "held" in task.error


@pytest.mark.parametrize("email", ["", "not-an-address", "ada@", "@example.com"])
def test_unusable_lookup_result_holds_only_email_lane(monkeypatch, email):
    deal, _version = _deal(email="")
    state = deal.state
    task = maybe_schedule_gmail_sequence(deal=deal, operator="Arian")
    monkeypatch.setattr(
        "gmail.tasks.enrich_email.BetterContactEmailProvider.enrich",
        lambda *_args: EnrichmentResult(status=EnrichmentStatus.FOUND, provider="bettercontact", email=email),
    )
    handle_enrich_email(task)
    deal.refresh_from_db()
    deal.lead.refresh_from_db()
    task.refresh_from_db()
    assert deal.state == state
    assert not deal.lead.disqualified
    assert not deal.lead.email
    assert "no usable address" in task.error
    assert OutboundDelivery.objects.get().status == OutboundDelivery.Status.PLANNED
    assert not Task.objects.filter(task_type=Task.TaskType.GMAIL_FOLLOW_UP).exists()


@pytest.mark.parametrize("value, expected", [
    (None, ""), ("", ""), ("not email", ""), ("ada@", ""),
    ("Ada <ADA@example.com>", ""),
    ("ada@example.com, other@example.com", ""),
    ("ada@example.com\r\nBcc: other@example.com", ""),
    (" ADA@example.com ", "ada@example.com"),
    ("ada+test@example.co.uk", "ada+test@example.co.uk"),
])
def test_address_check_is_syntactic_only(value, expected):
    assert usable_email(value) == expected


def test_later_reviewed_address_resumes_same_planned_delivery_without_lookup(monkeypatch):
    deal, _version = _deal(email="")
    lookup_task = maybe_schedule_gmail_sequence(deal=deal, operator="Arian")
    delivery = OutboundDelivery.objects.get()
    original = (delivery.scheduled_at, delivery.frozen_body, delivery.render_hash)
    lookups = []

    def not_found(*_args):
        lookups.append(True)
        return EnrichmentResult(status=EnrichmentStatus.NOT_FOUND, provider="bettercontact")

    monkeypatch.setattr("gmail.tasks.enrich_email.BetterContactEmailProvider.enrich", not_found)
    handle_enrich_email(lookup_task)
    lookup_task.mark_completed()
    delivery.refresh_from_db()
    assert delivery.status == OutboundDelivery.Status.PLANNED
    deal.lead.email = "reviewed@example.com"
    deal.lead.save(update_fields={"email"})

    send_task = maybe_schedule_gmail_sequence(deal=deal, operator="Arian")
    delivery.refresh_from_db()
    assert send_task.task_type == Task.TaskType.GMAIL_FOLLOW_UP
    assert send_task.payload["delivery_id"] == delivery.pk
    assert (delivery.scheduled_at, delivery.frozen_body, delivery.render_hash) == original
    assert len(lookups) == 1
