from __future__ import annotations

from unittest.mock import patch

import pytest
from django.utils import timezone

from crm.models import Deal, Lead, Meeting, Message
from linkedin.db.messages import persist_thread
from linkedin.enums import ProfileState
from linkedin.models import OutreachSuppression, Task
from linkedin.tasks.connect import enqueue_follow_up
from linkedin.tasks.stop_checks import (
    automation_stop_reason,
    handle_inbound_linkedin_messages_persisted,
    lead_automation_stop_reason,
    retire_pending_linkedin_follow_ups,
)


@pytest.fixture
def lead():
    return Lead.objects.create(
        first_name="Alice",
        last_name="Smith",
        linkedin_url="https://www.linkedin.com/in/alice-stop/",
        public_identifier="alice-stop",
        email="alice@example.com",
    )


def _message(lead, *, source, direction, external_id):
    return Message.objects.create(
        lead=lead,
        source=source,
        external_id=external_id,
        direction=direction,
        sender="alice@example.com",
        body="Hello",
        sent_at=timezone.now(),
    )


@pytest.mark.django_db
def test_lead_stop_reason_is_clear_for_outbound_only(lead):
    _message(
        lead,
        source=Message.Source.LINKEDIN,
        direction=Message.Direction.OUTBOUND,
        external_id="outbound-only",
    )

    assert lead_automation_stop_reason(lead) == ""


@pytest.mark.django_db
@pytest.mark.parametrize("source", [Message.Source.LINKEDIN, Message.Source.GMAIL])
def test_lead_stop_reason_catches_cross_channel_inbound(lead, source):
    _message(
        lead,
        source=source,
        direction=Message.Direction.INBOUND,
        external_id=f"inbound-{source}",
    )

    assert lead_automation_stop_reason(lead) == "Lead replied; automation stopped"


@pytest.mark.django_db
def test_lead_stop_reason_catches_meeting(lead):
    Meeting.objects.create(
        lead=lead,
        source=Meeting.Source.GOOGLE_CALENDAR,
        external_id="alice-stop-meeting",
        start_at=timezone.now(),
    )
    assert lead_automation_stop_reason(lead) == "Meeting exists; automation stopped"


@pytest.mark.django_db
def test_lead_stop_reason_catches_meeting_participant(lead):
    primary = Lead.objects.create(
        first_name="Bob",
        linkedin_url="https://www.linkedin.com/in/bob-meeting-primary/",
    )
    meeting = Meeting.objects.create(
        lead=primary,
        source=Meeting.Source.GOOGLE_CALENDAR,
        external_id="participant-stop-meeting",
        start_at=timezone.now(),
    )
    meeting.participants.add(lead)

    assert lead_automation_stop_reason(lead) == "Meeting exists; automation stopped"


@pytest.mark.django_db
def test_lead_stop_reason_catches_suppression(lead):
    OutreachSuppression.objects.create(
        kind=OutreachSuppression.Kind.LEAD,
        value="Alice Smith",
        linkedin_url=lead.linkedin_url,
    )
    assert lead_automation_stop_reason(lead) == "Suppression: Alice Smith"


@pytest.mark.django_db
def test_lead_stop_reason_catches_disqualification(lead):
    Lead.objects.filter(pk=lead.pk).update(disqualified=True)
    assert lead_automation_stop_reason(lead) == "Lead disqualified; automation stopped"


@pytest.mark.django_db
def test_deal_wrapper_delegates_to_lead_policy(fake_session, lead):
    deal = Deal.objects.create(
        lead=lead,
        campaign=fake_session.campaign,
        state=ProfileState.CONNECTED,
    )
    _message(
        lead,
        source=Message.Source.GMAIL,
        direction=Message.Direction.INBOUND,
        external_id="deal-wrapper-inbound",
    )

    assert automation_stop_reason(deal) == "Lead replied; automation stopped"


@pytest.mark.django_db
def test_enqueue_follow_up_skips_known_stopped_lead(fake_session, lead, monkeypatch):
    monkeypatch.setattr("linkedin.conf.ENABLE_FOLLOW_UP", True)
    Deal.objects.create(
        lead=lead,
        campaign=fake_session.campaign,
        state=ProfileState.CONNECTED,
    )
    _message(
        lead,
        source=Message.Source.LINKEDIN,
        direction=Message.Direction.INBOUND,
        external_id="enqueue-stop-inbound",
    )

    enqueue_follow_up(
        fake_session.campaign.pk,
        "alice-stop",
        operator="Arian",
        delay_seconds=0,
    )

    assert not Task.objects.filter(task_type=Task.TaskType.FOLLOW_UP).exists()


@pytest.mark.django_db
def test_retire_pending_linkedin_followups_uses_both_lead_identifiers(lead):
    for public_id in ("alice-stop", "unrelated"):
        Task.objects.create(
            task_type=Task.TaskType.FOLLOW_UP,
            status=Task.Status.PENDING,
            scheduled_at=timezone.now(),
            payload={
                "campaign_id": 1,
                "public_id": public_id,
                "operator": "Arian",
            },
        )

    retired = retire_pending_linkedin_follow_ups(
        lead,
        reason="Lead replied; automation stopped",
    )

    assert retired == 1
    stopped = Task.objects.get(payload__public_id="alice-stop")
    untouched = Task.objects.get(payload__public_id="unrelated")
    assert stopped.status == Task.Status.COMPLETED
    assert stopped.completed_at is not None
    assert stopped.error == "Lead replied; automation stopped"
    assert untouched.status == Task.Status.PENDING


@pytest.mark.django_db
def test_persist_thread_schedules_stop_hook_only_for_new_inbound(lead):
    callbacks = []
    inbound = {
        "entity_urn": "urn:li:msg:inbound-stop-hook",
        "sender": "Alice Smith",
        "text": "Interested",
        "timestamp": "2026-08-29 10:00",
    }

    with patch(
        "linkedin.db.messages.transaction.on_commit",
        side_effect=callbacks.append,
    ):
        assert persist_thread(lead=lead, parsed=[inbound]) == 1
        assert persist_thread(lead=lead, parsed=[inbound]) == 0

    assert len(callbacks) == 1
    message = Message.objects.get(external_id=inbound["entity_urn"])
    with patch(
        "linkedin.tasks.stop_checks.handle_inbound_linkedin_messages_persisted",
    ) as stop_hook:
        callbacks[0]()

    stop_hook.assert_called_once_with((message.pk,))


@pytest.mark.django_db
@patch("drip.services.stops.stop_for_inbound_message")
def test_inbound_stop_hook_retires_current_task_and_delegates_to_drip(
    drip_stop, lead,
):
    task = Task.objects.create(
        task_type=Task.TaskType.FOLLOW_UP,
        status=Task.Status.PENDING,
        scheduled_at=timezone.now(),
        payload={
            "campaign_id": 1,
            "public_id": "alice-stop",
            "operator": "Arian",
        },
    )
    gmail_task = Task.objects.create(
        task_type=Task.TaskType.GMAIL_FOLLOW_UP,
        status=Task.Status.PENDING,
        scheduled_at=timezone.now(),
        payload={"lead_id": lead.pk, "operator": "Arian", "step_index": 1},
    )
    email_enrichment = Task.objects.create(
        task_type=Task.TaskType.ENRICH_EMAIL,
        status=Task.Status.PENDING,
        scheduled_at=timezone.now(),
        payload={"lead_id": lead.pk, "operator": "Arian"},
    )
    manual = Task.objects.create(
        task_type=Task.TaskType.MANUAL_REPLY,
        status=Task.Status.PENDING,
        scheduled_at=timezone.now(),
        payload={"lead_id": lead.pk, "operator": "Arian", "message": "Human reply"},
    )
    message = _message(
        lead,
        source=Message.Source.LINKEDIN,
        direction=Message.Direction.INBOUND,
        external_id="delegated-inbound-stop",
    )

    handle_inbound_linkedin_messages_persisted((message.pk,))

    task.refresh_from_db()
    gmail_task.refresh_from_db()
    email_enrichment.refresh_from_db()
    manual.refresh_from_db()
    assert task.status == Task.Status.COMPLETED
    assert task.error == "Lead replied; automation stopped"
    assert gmail_task.status == Task.Status.COMPLETED
    assert email_enrichment.status == Task.Status.COMPLETED
    assert manual.status == Task.Status.PENDING
    drip_stop.assert_called_once_with(message.pk)
