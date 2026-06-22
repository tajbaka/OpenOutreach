import pytest
from django.utils import timezone

from crm.models import Deal, Lead, Message
from linkedin.enums import ProfileState
from gmail.handoff import maybe_schedule_gmail_sequence
from linkedin.models import Campaign, Task
from tests.factories import UserFactory


def _deal(*, email="", icp=""):
    user = UserFactory(username="owner")
    campaign = Campaign.objects.create(name="Campaign", user=user)
    lead = Lead.objects.create(
        first_name="Ada",
        last_name="Lovelace",
        company_name="Analytical Engines",
        linkedin_url="https://www.linkedin.com/in/ada-lovelace/",
        public_identifier="ada-lovelace",
        email=email,
        icp=icp,
    )
    return Deal.objects.create(
        lead=lead,
        campaign=campaign,
        state=ProfileState.CONNECTED,
    )


@pytest.mark.django_db
def test_handoff_existing_email_enqueues_gmail_follow_up(monkeypatch):
    monkeypatch.setattr("gmail.handoff.ENABLE_GMAIL_SEQUENCE", True)
    monkeypatch.setattr("linkedin.suppression.lead_suppression_match", lambda lead: None)
    deal = _deal(email="ada@example.com")

    task = maybe_schedule_gmail_sequence(deal=deal, operator="Arian")

    assert task.task_type == Task.TaskType.GMAIL_FOLLOW_UP
    assert task.payload["lead_id"] == deal.lead_id
    assert task.payload["operator"] == "Arian"
    assert task.payload["step_index"] == 0


@pytest.mark.django_db
def test_handoff_missing_email_enqueues_email_enrichment(monkeypatch):
    monkeypatch.setattr("gmail.handoff.ENABLE_GMAIL_SEQUENCE", True)
    monkeypatch.setattr("linkedin.suppression.lead_suppression_match", lambda lead: None)
    deal = _deal(email="")

    task = maybe_schedule_gmail_sequence(deal=deal, operator="Arian")

    assert task.task_type == Task.TaskType.ENRICH_EMAIL
    assert task.payload["lead_id"] == deal.lead_id
    assert task.payload["operator"] == "Arian"
    assert task.payload["step_index"] == 0


@pytest.mark.django_db
def test_handoff_uses_persisted_icp_for_gmail_templates(monkeypatch):
    monkeypatch.setattr("gmail.handoff.ENABLE_GMAIL_SEQUENCE", True)
    monkeypatch.setattr("linkedin.suppression.lead_suppression_match", lambda lead: None)
    deal = _deal(email="ada@example.com", icp="Channel")

    task = maybe_schedule_gmail_sequence(deal=deal, operator="Arian")

    assert task.task_type == Task.TaskType.GMAIL_FOLLOW_UP
    assert task.payload["lead_id"] == deal.lead_id


@pytest.mark.django_db
def test_handoff_stops_when_lead_replied_anywhere(monkeypatch):
    monkeypatch.setattr("gmail.handoff.ENABLE_GMAIL_SEQUENCE", True)
    deal = _deal(email="ada@example.com")
    Message.objects.create(
        lead=deal.lead,
        source=Message.Source.GMAIL,
        direction=Message.Direction.INBOUND,
        external_id="gmail-reply",
        body="reply",
        sent_at=timezone.now(),
    )

    assert maybe_schedule_gmail_sequence(deal=deal, operator="Arian") is None
    assert not Task.objects.exists()


@pytest.mark.django_db
def test_handoff_disabled_noops(monkeypatch):
    monkeypatch.setattr("gmail.handoff.ENABLE_GMAIL_SEQUENCE", False)
    deal = _deal(email="ada@example.com")

    assert maybe_schedule_gmail_sequence(deal=deal, operator="Arian") is None
    assert not Task.objects.exists()


@pytest.mark.django_db
def test_handoff_skips_unmapped_gmail_operator(monkeypatch):
    monkeypatch.setattr("gmail.handoff.ENABLE_GMAIL_SEQUENCE", True)
    deal = _deal(email="ada@example.com")

    assert maybe_schedule_gmail_sequence(deal=deal, operator="Chuka") is None
    assert not Task.objects.exists()


@pytest.mark.django_db
def test_handoff_queue_error_does_not_escape_to_linkedin(monkeypatch):
    monkeypatch.setattr("gmail.handoff.ENABLE_GMAIL_SEQUENCE", True)
    monkeypatch.setattr("linkedin.suppression.lead_suppression_match", lambda lead: None)
    deal = _deal(email="ada@example.com")

    def fail_enqueue(**kwargs):
        raise RuntimeError("task queue unavailable")

    monkeypatch.setattr("gmail.handoff.enqueue_gmail_follow_up", fail_enqueue)

    assert maybe_schedule_gmail_sequence(deal=deal, operator="Arian") is None
    assert not Task.objects.exists()
