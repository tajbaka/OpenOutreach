"""Tests for the realtime event handler (parse → resolve → persist → Slack)."""
from __future__ import annotations

from unittest.mock import patch

from crm.models import Lead, Message

from linkedin.realtime.handler import handle_realtime_event
from linkedin.realtime.parser import ParsedRealtimeMessage

CONV = "urn:li:msg_conversation:(x,2-handler)"


def _inbound(lead):
    """A ParsedRealtimeMessage whose sender matches `lead` (→ INBOUND)."""
    return ParsedRealtimeMessage(
        entity_urn="urn:li:msg:rt1",
        conversation_urn=CONV,
        sender_name=f"{lead.first_name} {lead.last_name}".strip(),
        sender_member_urn="urn:li:fsd_profile:LEAD1",
        text="Hey, interested — let's talk",
        timestamp="2026-05-16 14:30",
    )


def _seed_lead(db):
    lead = Lead.objects.create(
        first_name="Waylon", last_name="Krush",
        linkedin_url="https://www.linkedin.com/in/waylonkrush/",
    )
    # An existing outbound message so conversation-URN resolution works.
    Message.objects.create(
        lead=lead, source=Message.Source.LINKEDIN, external_id="seed",
        direction=Message.Direction.OUTBOUND, sender="Arian", body="hi",
        sent_at=__import__("django.utils.timezone", fromlist=["now"]).now(),
        thread_external_id=CONV,
    )
    return lead


def test_inbound_message_persists_and_notifies(db):
    lead = _seed_lead(db)
    with patch("linkedin.realtime.handler.parse_realtime_event", return_value=_inbound(lead)), \
         patch("linkedin.realtime.handler.notify_message_received") as mock_notify:
        handle_realtime_event({"data": "ignored"}, operator="Arian")

    msg = Message.objects.get(external_id="urn:li:msg:rt1")
    assert msg.direction == Message.Direction.INBOUND
    assert msg.lead == lead
    mock_notify.assert_called_once()


def test_duplicate_event_is_idempotent(db):
    lead = _seed_lead(db)
    with patch("linkedin.realtime.handler.parse_realtime_event", return_value=_inbound(lead)), \
         patch("linkedin.realtime.handler.notify_message_received") as mock_notify:
        handle_realtime_event({"data": "x"}, operator="Arian")
        handle_realtime_event({"data": "x"}, operator="Arian")

    assert Message.objects.filter(external_id="urn:li:msg:rt1").count() == 1
    assert mock_notify.call_count == 1  # second event already persisted → no re-notify


def test_non_message_event_is_skipped(db):
    with patch("linkedin.realtime.handler.parse_realtime_event", return_value=None), \
         patch("linkedin.realtime.handler.notify_message_received") as mock_notify:
        handle_realtime_event({"data": "presence"}, operator="Arian")
    assert Message.objects.count() == 0
    mock_notify.assert_not_called()


def test_unresolved_sender_is_skipped_no_crash(db):
    unmatched = ParsedRealtimeMessage(
        entity_urn="urn:li:msg:rt9", conversation_urn="urn:li:msg_conversation:(x,2-none)",
        sender_name="Ghost", sender_member_urn="urn:li:fsd_profile:GHOST",
        text="hi", timestamp="2026-05-16 14:30",
    )
    with patch("linkedin.realtime.handler.parse_realtime_event", return_value=unmatched), \
         patch("linkedin.realtime.handler.notify_message_received") as mock_notify:
        handle_realtime_event({"data": "x"}, operator="Arian")  # must not raise
    assert Message.objects.count() == 0
    mock_notify.assert_not_called()


def test_outbound_echo_persisted_but_not_notified(db):
    """An echo of our own send: sender != lead → persist_thread marks it
    OUTBOUND → no Slack notification."""
    lead = _seed_lead(db)
    echo = ParsedRealtimeMessage(
        entity_urn="urn:li:msg:rtecho", conversation_urn=CONV,
        sender_name="Arian Tajbakhsh", sender_member_urn="urn:li:fsd_profile:US",
        text="our own message", timestamp="2026-05-16 14:31",
    )
    with patch("linkedin.realtime.handler.parse_realtime_event", return_value=echo), \
         patch("linkedin.realtime.handler.notify_message_received") as mock_notify:
        handle_realtime_event({"data": "x"}, operator="Arian")
    msg = Message.objects.get(external_id="urn:li:msg:rtecho")
    assert msg.direction == Message.Direction.OUTBOUND
    mock_notify.assert_not_called()


def test_handler_swallows_exceptions_and_notifies_error(db):
    with patch("linkedin.realtime.handler.parse_realtime_event", side_effect=RuntimeError("boom")), \
         patch("linkedin.realtime.handler.notify_error") as mock_err:
        handle_realtime_event({"data": "x"}, operator="Arian")  # must not raise
    mock_err.assert_called_once()


def test_inbound_enqueues_enrichment_when_enabled(db):
    from linkedin.models import Task

    lead = _seed_lead(db)
    with patch("linkedin.realtime.handler.parse_realtime_event", return_value=_inbound(lead)), \
         patch("linkedin.realtime.handler.notify_message_received"), \
         patch("linkedin.realtime.handler.ENABLE_AUTO_PHONE_ENRICHMENT", True):
        handle_realtime_event({"data": "x"}, operator="Arian")

    tasks = Task.objects.filter(task_type=Task.TaskType.ENRICH_PHONE)
    assert tasks.count() == 1
    assert tasks.first().payload["lead_id"] == lead.id


def test_inbound_does_not_enqueue_when_disabled(db):
    from linkedin.models import Task

    lead = _seed_lead(db)
    with patch("linkedin.realtime.handler.parse_realtime_event", return_value=_inbound(lead)), \
         patch("linkedin.realtime.handler.notify_message_received"), \
         patch("linkedin.realtime.handler.ENABLE_AUTO_PHONE_ENRICHMENT", False):
        handle_realtime_event({"data": "x"}, operator="Arian")

    assert Task.objects.filter(task_type=Task.TaskType.ENRICH_PHONE).count() == 0


def test_enrichment_not_enqueued_when_all_providers_tried(db):
    from linkedin.models import Task

    lead = _seed_lead(db)
    lead.phone_providers_tried = ["bettercontact", "leadmagic", "prospeo"]
    lead.save(update_fields=["phone_providers_tried"])
    with patch("linkedin.realtime.handler.parse_realtime_event", return_value=_inbound(lead)), \
         patch("linkedin.realtime.handler.notify_message_received"), \
         patch("linkedin.realtime.handler.ENABLE_AUTO_PHONE_ENRICHMENT", True):
        handle_realtime_event({"data": "x"}, operator="Arian")

    assert Task.objects.filter(task_type=Task.TaskType.ENRICH_PHONE).count() == 0


def test_enrichment_deduped_against_existing_task(db):
    """A second inbound message before the worker runs must not enqueue a
    duplicate task (which would double-bill the provider)."""
    from django.utils import timezone as _tz
    from linkedin.models import Task

    lead = _seed_lead(db)
    Task.objects.create(
        task_type=Task.TaskType.ENRICH_PHONE,
        status=Task.Status.PENDING,
        scheduled_at=_tz.now(),
        payload={"lead_id": lead.id, "bettercontact_request_id": "",
                 "provider": "waterfall"},
    )
    # A new inbound event with a fresh entity_urn so persist_thread accepts it.
    second = ParsedRealtimeMessage(
        entity_urn="urn:li:msg:rt2", conversation_urn=CONV,
        sender_name=f"{lead.first_name} {lead.last_name}".strip(),
        sender_member_urn="urn:li:fsd_profile:LEAD1",
        text="ping again", timestamp="2026-05-16 14:35",
    )
    with patch("linkedin.realtime.handler.parse_realtime_event", return_value=second), \
         patch("linkedin.realtime.handler.notify_message_received"), \
         patch("linkedin.realtime.handler.ENABLE_AUTO_PHONE_ENRICHMENT", True):
        handle_realtime_event({"data": "x"}, operator="Arian")

    assert Task.objects.filter(task_type=Task.TaskType.ENRICH_PHONE).count() == 1
