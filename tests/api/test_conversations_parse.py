"""Tests for parse_messages entityUrn capture (B.2).

Per the user's instructions during the autonomous run on 2026-04-27, these
are written but NOT executed in the orchestration session — the user wants
to verify the daemon-side conversation persistence flow manually before
running these. Run with: `.venv/bin/pytest tests/api/test_conversations_parse.py -v`.
"""
from linkedin.actions.conversations import parse_messages


def test_parse_messages_includes_entity_urn():
    raw = {
        "data": {
            "messengerMessagesBySyncToken": {
                "elements": [
                    {
                        "entityUrn": "urn:li:msg:m1",
                        "body": {"text": "hello there"},
                        "deliveredAt": 1714560000000,
                        "sender": {
                            "participantType": {
                                "member": {
                                    "firstName": {"text": "Waylon"},
                                    "lastName": {"text": "Krush"},
                                },
                            },
                        },
                    },
                ],
            },
        },
    }
    parsed = parse_messages(raw)
    assert len(parsed) == 1
    assert parsed[0]["entity_urn"] == "urn:li:msg:m1"
    assert parsed[0]["text"] == "hello there"
    assert parsed[0]["sender"] == "Waylon Krush"


def test_parse_messages_skips_message_without_entity_urn():
    """Defensive — if Voyager ever omits entityUrn we still return the others."""
    raw = {
        "data": {
            "messengerMessagesBySyncToken": {
                "elements": [
                    {
                        "body": {"text": "no urn here"},
                        "deliveredAt": 1714560000000,
                        "sender": {"participantType": {"member": {
                            "firstName": {"text": "X"}, "lastName": {"text": "Y"},
                        }}},
                    },
                    {
                        "entityUrn": "urn:li:msg:m2",
                        "body": {"text": "with urn"},
                        "deliveredAt": 1714560000000,
                        "sender": {"participantType": {"member": {
                            "firstName": {"text": "X"}, "lastName": {"text": "Y"},
                        }}},
                    },
                ],
            },
        },
    }
    parsed = parse_messages(raw)
    assert len(parsed) == 1
    assert parsed[0]["entity_urn"] == "urn:li:msg:m2"
