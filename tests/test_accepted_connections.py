"""Sender-specific reporting must never change outreach/history."""
from datetime import datetime, timedelta, timezone
from itertools import count

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from crm.models import Deal, Message, SalesOwner
from linkedin.accepted_connections import AcceptedConnection, build_accepted_connections
from linkedin.enums import ProfileState
from linkedin.models import Campaign
from tests.factories import LeadFactory, UserFactory


pytestmark = pytest.mark.django_db
NOW = datetime(2026, 9, 10, 16, 42, tzinfo=timezone.utc)
IDS = count()


def campaign(sender="ariantajbakh", **kwargs):
    from django.contrib.auth.models import User
    user = User.objects.filter(username=sender).first() or UserFactory(username=sender)
    return Campaign.objects.create(user=user, name=f"Report {next(IDS)}", **kwargs)


def accepted(sender="ariantajbakh", *, lead=None, recorded=NOW, **kwargs):
    return Deal.objects.create(
        lead=lead or LeadFactory(), campaign=campaign(sender),
        connected_at=recorded, state=ProfileState.CONNECTED, **kwargs,
    )


def message(lead, operator="Arian", **kwargs):
    owner = SalesOwner.objects.get_or_create(handle=operator)[0] if operator else None
    values = dict(lead=lead, operator=owner, source=Message.Source.LINKEDIN,
                  external_id=f"report-{next(IDS)}", direction=Message.Direction.INBOUND,
                  body="Yes, let's speak next week", sent_at=NOW)
    values.update(kwargs)
    return Message.objects.create(**values)


def pairs():
    rows, counts = build_accepted_connections()
    return {(r.lead_id, r.operator) for r in rows}, counts


def test_newest_first_unknown_last_and_earliest_repeat_observation():
    older = accepted(recorded=NOW - timedelta(days=5))
    recent = accepted("chukyjack")
    unknown = accepted(recorded=None)
    accepted(lead=older.lead, recorded=NOW + timedelta(days=1))
    rows, counts = build_accepted_connections()
    assert [r.lead_id for r in rows] == [recent.lead_id, older.lead_id, unknown.lead_id]
    assert rows[1].recorded_at == older.connected_at
    assert rows[-1].sheet_values()[0] == ""
    assert counts["duplicate_deals"] == 1
    assert counts["unknown_dates"] == 1


@pytest.mark.parametrize("source", [Message.Source.LINKEDIN, Message.Source.GMAIL])
@pytest.mark.parametrize("operator", ["Arian", "Chuka", "Eddy"])
def test_only_matching_sender_removed_for_human_reply(source, operator):
    arian = accepted()
    accepted("chukyjack", lead=arian.lead)
    # A reply can precede detection; do not require reply > connected_at.
    message(arian.lead, operator, source=source, sent_at=NOW - timedelta(hours=1))
    remaining, counts = pairs()
    other = "Chuka" if operator == "Arian" else "Arian"
    assert remaining == {(arian.lead_id, other)}
    assert counts["replied_pairs"] == 1


@pytest.mark.parametrize("operator,kwargs", [
    ("Arian", {"direction": Message.Direction.OUTBOUND}),
    (None, {}),
    ("unknown-person", {}),
    ("Athena", {}),
    ("Arian", {"source": Message.Source.CALENDAR}),
    ("Arian", {"source": Message.Source.GMAIL,
               "raw": {"headers": {"Auto-Submitted": "auto-replied"}}}),
    ("Arian", {"sender": "mailer-daemon@example.test"}),
])
def test_outbound_automated_other_or_unattributed_messages_do_not_remove(operator, kwargs):
    deal = accepted(last_reply_at=NOW)
    message(deal.lead, operator, **kwargs)
    remaining, counts = pairs()
    assert remaining == {(deal.lead_id, "Arian")}
    assert counts["unattributed_replies"] == (1 if operator in (None, "unknown-person") else 0)


def test_persisted_connection_survives_campaign_disable_and_later_deal_state():
    deal = accepted()
    deal.campaign.status = "disabled"
    deal.campaign.save(update_fields=["status"])
    Deal.objects.filter(pk=deal.pk).update(state=ProfileState.COMPLETED)
    assert pairs()[0] == {(deal.lead_id, "Arian")}


def test_unknown_and_conflicting_sender_held_non_report_senders_skipped():
    accepted("not-a-sender")
    accepted(invitation_sender="Chuka")
    accepted(invitation_sender="unresolved-id")
    accepted("athenaaghdami")
    valid = accepted(invitation_sender="Arian")
    remaining, counts = pairs()
    assert remaining == {(valid.lead_id, "Arian")}
    assert counts["held_sender_identity"] == 3


def test_projection_performs_selects_only():
    accepted()
    with CaptureQueriesContext(connection) as queries:
        build_accepted_connections()
    assert queries
    # PostgreSQL streams iterator() through a declared read-only SELECT cursor;
    # SQLite executes the SELECT directly.
    statements = [q["sql"].lstrip().upper() for q in queries]
    assert all(sql.startswith("SELECT ") or (
        sql.startswith("DECLARE ") and " CURSOR " in sql and " FOR SELECT " in sql
    ) for sql in statements)
    # Keep the historical remote read bounded to projection fields; never
    # transfer profile blobs, embeddings, or campaign models for this report.
    assert '"crm_lead"."raw"' not in queries[0]["sql"]
    assert '"linkedin_campaign"."model_blob"' not in queries[0]["sql"]
    assert len(queries) == 2


@pytest.mark.parametrize("sender,held", [("Arian", False), ("Chuka", True), ("unknown", True)])
def test_frozen_enrollment_sender_must_match_campaign(sender, held):
    from linkedin.models import CampaignMessageEnrollment, MessageProgram, MessageProgramVersion
    deal = accepted()
    program = MessageProgram.objects.create(key="report-test", name="Report test")
    version = MessageProgramVersion.objects.create(
        program=program, version=1, payload={}, content_hash="a" * 64, published_by="test",
    )
    CampaignMessageEnrollment.objects.create(
        deal=deal, message_version=version, audience_key="test", operator=sender,
    )
    remaining, counts = pairs()
    assert remaining == (set() if held else {(deal.lead_id, "Arian")})
    assert counts["held_sender_identity"] == int(held)


@pytest.mark.parametrize("source", [Message.Source.LINKEDIN, Message.Source.GMAIL])
@pytest.mark.parametrize("use_explicit_owner", [False, True])
def test_inbound_inherits_exact_conversation_owner_without_changing_messages(source, use_explicit_owner):
    deal = accepted()
    accepted("chukyjack", lead=deal.lead)
    message(deal.lead, "Arian" if use_explicit_owner else None, source=source,
            sender="Arian Taj", direction=Message.Direction.OUTBOUND, thread_external_id="thread-a")
    reply = message(deal.lead, None, source=source, thread_external_id="thread-a")
    remaining, counts = pairs()
    assert remaining == {(deal.lead_id, "Chuka")}
    assert counts["thread_attributed_replies"] == 1
    reply.refresh_from_db()
    assert reply.operator_id is None


@pytest.mark.parametrize("mismatch", ["lead", "source", "thread", "blank-thread", "ambiguous", "unknown", "conflicting"])
def test_never_borrow_owner_across_conversations_or_guess_ambiguous_threads(mismatch):
    deal = accepted()
    thread = "" if mismatch == "blank-thread" else "same-id"
    kwargs = dict(lead=deal.lead, operator="Arian", direction=Message.Direction.OUTBOUND,
                  thread_external_id=thread)
    if mismatch == "lead":
        kwargs["lead"] = LeadFactory()
    elif mismatch == "source":
        kwargs["source"] = Message.Source.GMAIL
    elif mismatch == "thread":
        kwargs["thread_external_id"] = "other-thread"
    elif mismatch == "unknown":
        kwargs["operator"] = None
    elif mismatch == "conflicting":
        kwargs["sender"] = "Chuka"
    message(**kwargs)
    if mismatch == "ambiguous":
        message(deal.lead, "Chuka", direction=Message.Direction.OUTBOUND, thread_external_id=thread)
    message(deal.lead, None, thread_external_id=thread)
    remaining, counts = pairs()
    assert remaining == {(deal.lead_id, "Arian")}
    assert counts["unattributed_replies"] == 1


@pytest.mark.parametrize("stamp,local,pattern", [
    (NOW, datetime(2026, 9, 10, 12, 42), "mmm d, yyyy h:mm AM/PM"),
    (datetime(2026, 1, 10, 16, tzinfo=timezone.utc), datetime(2026, 1, 10, 11), "mmm d, yyyy h:mm AM/PM"),
    (datetime(2026, 9, 10, tzinfo=timezone.utc), datetime(2026, 9, 10), "mmm d, yyyy"),
])
def test_toronto_timestamp_and_conservative_legacy_date_only(stamp, local, pattern):
    row = AcceptedConnection(1, "Chuka", stamp, "Test Person", "Example", "https://example.test")
    serial = (local - datetime(1899, 12, 30)).total_seconds() / 86400
    assert row.sheet_values() == [serial, "Eddy", "Test Person", "Example", "https://example.test"]
    assert row.date_format == pattern
