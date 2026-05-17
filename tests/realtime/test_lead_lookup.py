"""Tests for resolving a Lead from a realtime event's URNs."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from crm.models import Lead, Message

from linkedin.realtime.lead_lookup import resolve_lead_for_realtime


def test_resolves_by_conversation_urn(db):
    lead = Lead.objects.create(
        first_name="Waylon", last_name="Krush",
        linkedin_url="https://www.linkedin.com/in/waylonkrush/",
    )
    Message.objects.create(
        lead=lead, source=Message.Source.LINKEDIN, external_id="urn:li:msg:1",
        direction=Message.Direction.OUTBOUND, sender="Arian", body="hi",
        sent_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        thread_external_id="urn:li:msg_conversation:(x,2-abc)",
    )
    got = resolve_lead_for_realtime(
        conversation_urn="urn:li:msg_conversation:(x,2-abc)",
        sender_member_urn="",
    )
    assert got == lead


def test_resolves_by_sender_member_urn_from_profile_json(db):
    lead = Lead.objects.create(
        first_name="Dana", linkedin_url="https://www.linkedin.com/in/dana/",
        description=json.dumps({"urn": "urn:li:fsd_profile:DANA123"}),
    )
    got = resolve_lead_for_realtime(
        conversation_urn="urn:li:msg_conversation:(x,2-unknown)",
        sender_member_urn="urn:li:fsd_profile:DANA123",
    )
    assert got == lead


def test_returns_none_when_no_match(db):
    assert resolve_lead_for_realtime(
        conversation_urn="urn:li:msg_conversation:(x,2-nope)",
        sender_member_urn="urn:li:fsd_profile:NOBODY",
    ) is None


def test_conversation_urn_match_wins_over_sender(db):
    by_conv = Lead.objects.create(
        first_name="ByConv", linkedin_url="https://www.linkedin.com/in/byconv/",
    )
    Message.objects.create(
        lead=by_conv, source=Message.Source.LINKEDIN, external_id="m1",
        direction=Message.Direction.OUTBOUND, sender="us", body="hi",
        sent_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        thread_external_id="urn:li:msg_conversation:(x,2-shared)",
    )
    Lead.objects.create(
        first_name="BySender", linkedin_url="https://www.linkedin.com/in/bysender/",
        description=json.dumps({"urn": "urn:li:fsd_profile:S1"}),
    )
    got = resolve_lead_for_realtime(
        conversation_urn="urn:li:msg_conversation:(x,2-shared)",
        sender_member_urn="urn:li:fsd_profile:S1",
    )
    assert got == by_conv
