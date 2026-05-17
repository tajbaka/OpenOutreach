"""Parser tests, driven by the fixtures captured in Task 2.

The fixture files ARE the contract. If an assertion fails, the captured
payload at tests/fixtures/realtime/<name>.json is authoritative.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from linkedin.realtime.parser import ParsedRealtimeMessage, parse_realtime_event

FIXTURES = Path(__file__).parent.parent / "fixtures" / "realtime"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_inbound_message_parses():
    r = parse_realtime_event(_load("inbound_message.json"))
    assert isinstance(r, ParsedRealtimeMessage)
    assert r.entity_urn.startswith("urn:li:messagingMessage:")
    assert r.conversation_urn.startswith("urn:li:msg_conversation:")
    assert r.text == "hey im interested"
    assert r.sender_name == "Arian Taj"
    assert r.sender_member_urn.startswith("urn:li:fsd_profile:")
    # timestamp must be persist_thread-compatible: "YYYY-MM-DD HH:MM"
    assert len(r.timestamp) == 16 and r.timestamp[4] == "-"


def test_outbound_echo_parses():
    """Outbound echoes still parse — the handler decides direction from the
    persisted row. The parser returns None only for NON-message events."""
    r = parse_realtime_event(_load("outbound_echo.json"))
    assert isinstance(r, ParsedRealtimeMessage)
    assert r.sender_name == "Chuka Agu"
    assert r.text == "okay thanks"


@pytest.mark.parametrize("name", ["typing_indicator.json", "read_receipt.json", "presence.json"])
def test_non_message_events_return_none(name):
    assert parse_realtime_event(_load(name)) is None


def test_garbage_returns_none():
    assert parse_realtime_event(None) is None
    assert parse_realtime_event({}) is None
    assert parse_realtime_event({"com.linkedin.realtimefrontend.DecoratedEvent": {}}) is None
    assert parse_realtime_event(
        {"com.linkedin.realtimefrontend.DecoratedEvent":
         {"topic": "urn:li-realtime:messagesTopic:x", "payload": {}}}
    ) is None
