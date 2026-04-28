"""Tests for crm.Message model and persist_thread helper.

Per the user's instructions during the autonomous run on 2026-04-27, these
are written but NOT executed in the orchestration session — the user wants
to verify the daemon-side conversation persistence flow manually before
running these. Run with: `.venv/bin/pytest tests/test_messages.py -v`.
"""
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from django.db import IntegrityError

from crm.models import Lead, Message
from linkedin.db.messages import persist_thread


# ---------------------------------------------------------------------------
# B.1 — Message model
# ---------------------------------------------------------------------------


def test_message_can_be_created_for_a_lead(db):
    lead = Lead.objects.create(
        first_name="Waylon",
        linkedin_url="https://www.linkedin.com/in/waylonkrush/",
    )
    msg = Message.objects.create(
        lead=lead,
        source=Message.Source.LINKEDIN,
        external_id="urn:li:message:abc123",
        direction=Message.Direction.OUTBOUND,
        sender="Arian Tajbakhsh",
        body="Hey Waylon, ...",
        sent_at=datetime(2026, 4, 1, 10, 0, tzinfo=timezone.utc),
    )
    assert msg.pk is not None
    assert lead.messages.count() == 1


def test_message_unique_together_source_and_external_id(db):
    lead = Lead.objects.create(
        first_name="A", linkedin_url="https://www.linkedin.com/in/a-1/",
    )
    Message.objects.create(
        lead=lead,
        source=Message.Source.LINKEDIN,
        external_id="urn:li:message:1",
        direction=Message.Direction.INBOUND,
        body="hi",
        sent_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
    )
    with pytest.raises(IntegrityError):
        Message.objects.create(
            lead=lead,
            source=Message.Source.LINKEDIN,
            external_id="urn:li:message:1",
            direction=Message.Direction.INBOUND,
            body="hi (dup)",
            sent_at=datetime(2026, 4, 2, tzinfo=timezone.utc),
        )


def test_message_same_external_id_allowed_across_sources(db):
    """Gmail and LinkedIn might coincidentally share an ID format."""
    lead = Lead.objects.create(
        first_name="A", linkedin_url="https://www.linkedin.com/in/a-2/",
    )
    Message.objects.create(
        lead=lead,
        source=Message.Source.LINKEDIN,
        external_id="x123",
        direction=Message.Direction.INBOUND,
        sent_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
    )
    Message.objects.create(
        lead=lead,
        source=Message.Source.GMAIL,
        external_id="x123",
        direction=Message.Direction.INBOUND,
        sent_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
    )


# ---------------------------------------------------------------------------
# B.3 — persist_thread helper
# ---------------------------------------------------------------------------


def test_persist_thread_creates_messages(db):
    lead = Lead.objects.create(
        first_name="Waylon", last_name="Krush",
        linkedin_url="https://www.linkedin.com/in/waylonkrush/",
    )
    parsed = [
        {
            "entity_urn": "urn:li:msg:m1",
            "sender": "Arian Tajbakhsh",
            "text": "Hey Waylon, ...",
            "timestamp": "2026-04-01 10:00",
        },
        {
            "entity_urn": "urn:li:msg:m2",
            "sender": "Waylon Krush",
            "text": "Sounds interesting",
            "timestamp": "2026-04-02 14:30",
        },
    ]
    persist_thread(
        lead=lead,
        parsed=parsed,
        thread_external_id="urn:li:conv:c1",
    )

    msgs = list(lead.messages.order_by("sent_at"))
    assert len(msgs) == 2
    assert msgs[0].direction == Message.Direction.OUTBOUND
    assert msgs[0].external_id == "urn:li:msg:m1"
    assert msgs[1].direction == Message.Direction.INBOUND
    assert msgs[1].thread_external_id == "urn:li:conv:c1"


def test_persist_thread_is_idempotent(db):
    lead = Lead.objects.create(
        first_name="Waylon", linkedin_url="https://www.linkedin.com/in/waylonkrush/",
    )
    parsed = [{
        "entity_urn": "urn:li:msg:dup",
        "sender": "Waylon Krush",
        "text": "hi",
        "timestamp": "2026-04-01 10:00",
    }]
    persist_thread(lead=lead, parsed=parsed)
    persist_thread(lead=lead, parsed=parsed)
    assert lead.messages.count() == 1


def test_persist_thread_handles_unparseable_timestamp(db):
    """If timestamp is empty or malformed, fall back to now() — never raise."""
    lead = Lead.objects.create(
        first_name="X", linkedin_url="https://www.linkedin.com/in/x-1/",
    )
    parsed = [{
        "entity_urn": "urn:li:msg:no_ts",
        "sender": "Waylon Krush",
        "text": "hi",
        "timestamp": "",
    }]
    persist_thread(lead=lead, parsed=parsed)
    msg = lead.messages.get()
    assert msg.sent_at is not None  # fell back to now()


# ---------------------------------------------------------------------------
# B.4 — get_conversation hook persists matched-Lead threads
# ---------------------------------------------------------------------------


@patch("linkedin.actions.conversations.fetch_messages")
@patch("linkedin.actions.conversations.find_conversation_urn")
@patch("linkedin.actions.conversations.find_conversation_urn_via_navigation")
@patch("linkedin.db.leads.resolve_urn")
def test_get_conversation_persists_messages_when_lead_exists(
    mock_resolve, mock_nav, mock_find, mock_fetch, fake_session,
):
    from linkedin.actions.conversations import get_conversation

    Lead.objects.create(
        first_name="Waylon",
        linkedin_url="https://www.linkedin.com/in/waylonkrush/",
        public_identifier="waylonkrush",
    )

    mock_resolve.return_value = "urn:li:fsd_profile:abc"
    mock_find.return_value = "urn:li:conv:c1"
    mock_fetch.return_value = {
        "data": {"messengerMessagesBySyncToken": {"elements": [
            {
                "entityUrn": "urn:li:msg:hook1",
                "body": {"text": "hi"},
                "deliveredAt": 1714560000000,
                "sender": {"participantType": {"member": {
                    "firstName": {"text": "Waylon"},
                    "lastName": {"text": "Krush"},
                }}},
            },
        ]}},
    }

    result = get_conversation(fake_session, "waylonkrush")
    assert result and len(result) == 1
    assert Message.objects.filter(external_id="urn:li:msg:hook1").exists()
