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
from linkedin.models import Campaign
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


def test_save_chat_message_uses_step_aware_daemon_external_id(fake_session):
    from linkedin.db.chat import save_chat_message

    lead = Lead.objects.create(
        first_name="Alice",
        linkedin_url="https://www.linkedin.com/in/alice-sequence/",
    )
    fake_session.linkedin_profile.linkedin_username = "ariant@tryfedrampgpt.com"

    save_chat_message(
        fake_session,
        "alice-sequence",
        "follow-up body",
        deal_id=123,
        sequence_name="linkedin_connect_followup",
        step_index=0,
        operator="Arian",
    )

    msg = Message.objects.get(lead=lead, source=Message.Source.LINKEDIN)
    assert msg.external_id.startswith(
        "daemon-send:Arian:123:linkedin_connect_followup:step-0:"
    )
    assert msg.sender == "ariant@tryfedrampgpt.com"


def test_save_chat_message_keeps_legacy_external_id_without_sequence(fake_session):
    from linkedin.db.chat import save_chat_message

    lead = Lead.objects.create(
        first_name="Alice",
        linkedin_url="https://www.linkedin.com/in/alice-legacy/",
    )

    save_chat_message(fake_session, "alice-legacy", "manual body")

    msg = Message.objects.get(lead=lead, source=Message.Source.LINKEDIN)
    assert msg.external_id.startswith(f"daemon-send:{lead.pk}:")


def test_save_chat_message_uses_manual_reply_external_id(fake_session):
    from linkedin.db.chat import save_chat_message

    lead = Lead.objects.create(
        first_name="Alice",
        linkedin_url="https://www.linkedin.com/in/alice-manual/",
    )
    fake_session.linkedin_profile.linkedin_username = "chukyjack@gmail.com"

    save_chat_message(
        fake_session,
        "alice-manual",
        "manual reply body",
        operator="Chuka",
        external_id_kind="manual-reply",
    )

    msg = Message.objects.get(lead=lead, source=Message.Source.LINKEDIN)
    assert msg.external_id.startswith(f"manual-reply:Chuka:{lead.pk}:")
    assert msg.sender == "chukyjack@gmail.com"


def test_send_raw_message_can_disable_api_fallback(fake_session):
    from linkedin.actions import message as message_mod

    profile = {"public_identifier": "alice-no-api"}

    with patch.object(message_mod, "_send_message", return_value=False) as direct, \
            patch.object(message_mod, "_send_message_via_api") as api, \
            patch.object(message_mod, "_send_msg_pop_up") as popup:
        sent = message_mod.send_raw_message(
            fake_session,
            profile,
            "manual body",
            prefer_direct=True,
            allow_api_fallback=False,
        )

    assert sent is False
    direct.assert_called_once()
    popup.assert_not_called()
    api.assert_not_called()


def test_send_raw_message_can_raise_direct_failure(fake_session):
    from linkedin.actions import message as message_mod

    profile = {"public_identifier": "alice-no-urn"}

    with patch("linkedin.db.leads.resolve_urn", return_value=""):
        with pytest.raises(message_mod.MessageSendError, match="could not resolve URN"):
            message_mod.send_raw_message(
                fake_session,
                profile,
                "manual body",
                prefer_direct=True,
                allow_api_fallback=False,
                raise_on_failure=True,
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


def test_persist_thread_treats_matching_sent_note_as_outbound(db, django_user_model):
    lead = Lead.objects.create(
        first_name="Joseph W. Giusti",
        last_name="MBA, MS (Cybersecurity), Veteran, TS/SCI",
        linkedin_url="https://www.linkedin.com/in/josephwgiusti/",
    )
    user = django_user_model.objects.create(username="chukyjack")
    campaign = Campaign.objects.create(name="FedRampGPT test", user=user)
    campaign.deals.create(
        lead=lead,
        state="Pending",
        sent_note=(
            "Hi Joseph W. Giusti, we built FedrampGPT around FedRAMP "
            "authorization + 20x continuous monitoring. Would love to connect."
        ),
    )
    parsed = [{
        "entity_urn": "urn:li:msg:note-echo",
        "sender": "Joseph W. Giusti MBA, MS (Cybersecurity), Veteran, TS/SCI",
        "text": (
            "Hi Joseph W. Giusti, we built FedrampGPT around FedRAMP "
            "authorization + 20x continuous monitoring. Would love to connect."
        ),
        "timestamp": "2026-05-19 19:42",
    }]

    persist_thread(lead=lead, parsed=parsed)

    msg = lead.messages.get()
    assert msg.direction == Message.Direction.OUTBOUND


def test_persist_thread_treats_legacy_connect_note_echo_as_outbound(db):
    lead = Lead.objects.create(
        first_name="Walter",
        last_name="Maikish",
        company_name="Elisity",
        linkedin_url="https://www.linkedin.com/in/waltermaikish/",
    )
    parsed = [{
        "entity_urn": "urn:li:msg:walter-note-echo",
        "sender": "Walter Maikish",
        "text": (
            "Hey Walter — we are building FedrampGPT around FedRAMP auth "
            "+ continuous monitoring. Would love to connect."
        ),
        "timestamp": "2026-06-17 16:08",
    }]

    persist_thread(lead=lead, parsed=parsed)

    msg = lead.messages.get()
    assert msg.direction == Message.Direction.OUTBOUND


def test_persist_thread_treats_known_operator_sender_as_outbound(db):
    lead = Lead.objects.create(
        first_name="Arian",
        last_name="Taj",
        linkedin_url="https://www.linkedin.com/in/arian-taj/",
    )
    parsed = [{
        "entity_urn": "urn:li:msg:self-echo",
        "sender": "Arian Taj",
        "text": "outbound echo from our own account",
        "timestamp": "2026-05-16 14:31",
    }]

    persist_thread(lead=lead, parsed=parsed, outbound_senders={"Arian"})

    msg = lead.messages.get()
    assert msg.direction == Message.Direction.OUTBOUND


def test_persist_thread_treats_honorific_lead_sender_as_inbound(db):
    lead = Lead.objects.create(
        first_name="Jacquelyn",
        last_name="B.",
        linkedin_url="https://www.linkedin.com/in/jacquelyn-bell-solutions/",
    )
    parsed = [{
        "entity_urn": "urn:li:msg:dr-jacquelyn",
        "sender": "Dr. Jacquelyn Bell",
        "text": "How will you be delivering the solution across the agencies?",
        "timestamp": "2026-06-15 21:50",
    }]

    persist_thread(lead=lead, parsed=parsed)

    msg = lead.messages.get()
    assert msg.direction == Message.Direction.INBOUND


# ---------------------------------------------------------------------------
# B.4 — get_conversation hook persists matched-Lead threads
# ---------------------------------------------------------------------------


@patch("linkedin.actions.conversations.fetch_messages")
@patch("linkedin.actions.conversations.find_conversation_urn")
@patch("linkedin.actions.conversations.find_conversation_urn_via_navigation")
@patch("linkedin.actions.conversations.PlaywrightLinkedinAPI")
@patch("linkedin.db.leads.resolve_urn")
def test_get_conversation_persists_messages_when_lead_exists(
    mock_resolve, mock_api, mock_nav, mock_find, mock_fetch, fake_session,
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


# ---------------------------------------------------------------------------
# lead_outbound_operators — daemon owner-scoping helper
# ---------------------------------------------------------------------------


def test_lead_outbound_operators_canonicalizes_known_aliases(db):
    """Senders flow through `linkedin.operators.resolve_operator`, so the
    set returns canonical handles like 'Chuka' regardless of which
    surface-form of the name was on the Message row."""
    from linkedin.db.messages import lead_outbound_operators
    lead = Lead.objects.create(
        first_name="Travis",
        linkedin_url="https://www.linkedin.com/in/travis/",
    )
    for idx, sender in enumerate((
        "chukwuka agu",
        "Chuka Eddy Jack",
        "eddy agu",
        "eddy@tryfedrampgpt.com",
        "leili amirshahi",
        "leili.ash2011@yahoo.com",
        "athena aghdami",
        "athenaaghdami@gmail.com",
    )):
        Message.objects.create(
            lead=lead,
            source=Message.Source.LINKEDIN,
            external_id=f"urn:li:msg:{idx}",
            direction=Message.Direction.OUTBOUND,
            sender=sender,
            body="hi",
            sent_at=datetime(2026, 4, 1, 10, 0, tzinfo=timezone.utc),
        )
    assert lead_outbound_operators(lead) == {"Chuka", "Leili", "Athena"}


def test_lead_outbound_operators_skips_inbound_and_gmail(db):
    """Only OUTBOUND LINKEDIN messages count — inbound is the lead replying
    (so they're the sender there, which we never want to use as 'who owns
    the daemon thread'), and Gmail threads don't constrain LinkedIn DM
    sending ability."""
    from linkedin.db.messages import lead_outbound_operators
    lead = Lead.objects.create(
        first_name="Travis",
        linkedin_url="https://www.linkedin.com/in/travis/",
    )
    Message.objects.create(
        lead=lead, source=Message.Source.LINKEDIN, external_id="m1",
        direction=Message.Direction.INBOUND, sender="travis", body="hi",
        sent_at=datetime(2026, 4, 1, 10, 0, tzinfo=timezone.utc),
    )
    Message.objects.create(
        lead=lead, source=Message.Source.GMAIL, external_id="m2",
        direction=Message.Direction.OUTBOUND, sender="arian taj", body="hi",
        sent_at=datetime(2026, 4, 1, 10, 0, tzinfo=timezone.utc),
    )
    # No qualifying outbound LinkedIn → empty set (no constraint).
    assert lead_outbound_operators(lead) == set()


def test_lead_outbound_operators_ignores_echoed_connection_note_sender(db):
    """LinkedIn/backfill can echo our connection note as an outbound
    message while showing the lead's name as sender. That proves a thread
    exists, but it must not claim thread ownership away from the daemon
    account that is about to follow up."""
    from linkedin.db.messages import lead_outbound_operators
    lead = Lead.objects.create(
        first_name="Brian",
        last_name="Pennington",
        linkedin_url="https://www.linkedin.com/in/bfpennington/",
    )
    Message.objects.create(
        lead=lead, source=Message.Source.LINKEDIN, external_id="m1",
        direction=Message.Direction.OUTBOUND, sender="brian pennington",
        body="Hi Brian, saw you checking out Boundera...",
        sent_at=datetime(2026, 4, 1, 10, 0, tzinfo=timezone.utc),
    )

    assert lead_outbound_operators(lead) == set()


def test_lead_outbound_operators_returns_multiple_operators(db):
    """Cross-operator threads (rare, e.g. shared account or accidental
    cross-send) surface both operator handles. Caller decides what to
    do — the daemon's owner-scoping check uses `in` so as long as the
    current operator is in the set, the send proceeds."""
    from linkedin.db.messages import lead_outbound_operators
    lead = Lead.objects.create(
        first_name="Travis",
        linkedin_url="https://www.linkedin.com/in/travis/",
    )
    Message.objects.create(
        lead=lead, source=Message.Source.LINKEDIN, external_id="m1",
        direction=Message.Direction.OUTBOUND, sender="chukwuka agu", body="hi",
        sent_at=datetime(2026, 4, 1, 10, 0, tzinfo=timezone.utc),
    )
    Message.objects.create(
        lead=lead, source=Message.Source.LINKEDIN, external_id="m2",
        direction=Message.Direction.OUTBOUND, sender="Arian Taj", body="follow-up",
        sent_at=datetime(2026, 4, 2, 10, 0, tzinfo=timezone.utc),
    )
    assert lead_outbound_operators(lead) == {"Chuka", "Arian"}
