"""Unit tests for the api/slack_enrich.py Vercel function.

The function lives outside any package (Vercel treats every file in api/ as a
serverless function), so it is loaded by path with importlib rather than a
normal import.
"""
import hashlib
import hmac
import importlib.util
import json
import pathlib
from unittest.mock import MagicMock, patch
from urllib.parse import urlencode

import pytest

_PATH = pathlib.Path(__file__).resolve().parent.parent / "api" / "slack_enrich.py"
_spec = importlib.util.spec_from_file_location("slack_enrich", _PATH)
slack_enrich = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(slack_enrich)


_SECRET = "test-signing-secret"


def _sign(body: str, timestamp: str, secret: str = _SECRET) -> str:
    basestring = f"v0:{timestamp}:{body}".encode("utf-8")
    return "v0=" + hmac.new(
        secret.encode("utf-8"), basestring, hashlib.sha256,
    ).hexdigest()


def test_verify_signature_accepts_a_valid_signature():
    now = 1_700_000_000
    body = "payload=%7B%7D"
    ts = str(now)
    sig = _sign(body, ts)
    assert slack_enrich.verify_signature(
        body, ts, sig, secret=_SECRET, now=now,
    ) is True


def test_verify_signature_rejects_a_bad_signature():
    now = 1_700_000_000
    assert slack_enrich.verify_signature(
        "payload=%7B%7D", str(now), "v0=deadbeef", secret=_SECRET, now=now,
    ) is False


def test_verify_signature_rejects_a_stale_timestamp():
    now = 1_700_000_000
    stale = now - 60 * 10  # 10 minutes old
    body = "payload=%7B%7D"
    sig = _sign(body, str(stale))
    assert slack_enrich.verify_signature(
        body, str(stale), sig, secret=_SECRET, now=now,
    ) is False


def test_verify_signature_rejects_missing_headers():
    assert slack_enrich.verify_signature(
        "body", "", "", secret=_SECRET, now=1_700_000_000,
    ) is False


def _interaction_body(value: str, message_blocks=None) -> str:
    payload = {"actions": [{"selected_option": {"value": value}}]}
    if message_blocks is not None:
        payload["message"] = {"blocks": message_blocks}
    return urlencode({"payload": json.dumps(payload)})


def _reply_button_body(value: str = "42:Chuka", message_blocks=None) -> str:
    payload = {
        "type": "block_actions",
        "trigger_id": "trigger-123",
        "response_url": "https://hooks.slack.com/actions/T/B/R",
        "channel": {"id": "C123"},
        "message": {"ts": "171234.567", "blocks": message_blocks or []},
        "actions": [{"action_id": "linkedin_reply_button", "value": value}],
    }
    return urlencode({"payload": json.dumps(payload)})


def _reply_modal_body(message: str = "Sounds good", metadata=None) -> str:
    payload = {
        "type": "view_submission",
        "user": {"id": "U123"},
        "view": {
            "callback_id": "linkedin_reply_modal",
            "private_metadata": json.dumps(metadata or {
                "lead_id": 42,
                "operator": "Chuka",
                "channel_id": "C123",
                "message_ts": "171234.567",
                "response_url": "https://hooks.slack.com/actions/T/B/R",
                "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "hi"}}],
            }),
            "state": {
                "values": {
                    "linkedin_reply_message": {
                        "linkedin_reply_body": {"value": message},
                    },
                },
            },
        },
    }
    return urlencode({"payload": json.dumps(payload)})


_MENU_BLOCK = {
    "type": "actions",
    "block_id": "enrich_phone_actions",
    "elements": [{
        "type": "static_select", "action_id": "enrich_phone_select",
        "options": [],
    }],
}


def test_parse_interaction_extracts_lead_provider_and_blocks():
    body = _interaction_body("42:leadmagic")
    lead_id, provider, blocks = slack_enrich.parse_interaction(body)
    assert (lead_id, provider) == (42, "leadmagic")
    assert blocks == []  # no message echoed → empty


def test_parse_interaction_handles_waterfall():
    body = _interaction_body("7:waterfall")
    lead_id, provider, _ = slack_enrich.parse_interaction(body)
    assert (lead_id, provider) == (7, "waterfall")


def test_parse_interaction_returns_echoed_message_blocks():
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "hi"}}]
    body = _interaction_body("5:prospeo", message_blocks=blocks)
    lead_id, provider, original = slack_enrich.parse_interaction(body)
    assert (lead_id, provider) == (5, "prospeo")
    assert original == blocks


def test_parse_interaction_rejects_missing_payload():
    with pytest.raises(ValueError):
        slack_enrich.parse_interaction("notpayload=x")


def test_parse_interaction_rejects_value_without_colon():
    with pytest.raises(ValueError):
        slack_enrich.parse_interaction(_interaction_body("nocolon"))


def _mock_conn(existing: bool):
    """A psycopg-shaped mock whose dedup SELECT returns a row iff `existing`."""
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value = (1,) if existing else None
    return conn, cur


def test_enqueue_task_inserts_when_none_exists():
    conn, cur = _mock_conn(existing=False)
    inserted = slack_enrich.enqueue_task(conn, 42, "leadmagic")
    assert inserted is True
    # Two execute calls: the dedup SELECT then the INSERT.
    assert cur.execute.call_count == 2
    insert_sql = cur.execute.call_args_list[1][0][0]
    assert "INSERT INTO linkedin_task" in insert_sql
    conn.commit.assert_called_once()


def test_enqueue_task_dedups_when_one_is_pending():
    conn, cur = _mock_conn(existing=True)
    inserted = slack_enrich.enqueue_task(conn, 42, "leadmagic")
    assert inserted is False
    # Only the dedup SELECT ran — no INSERT.
    assert cur.execute.call_count == 1
    conn.commit.assert_not_called()


def test_enqueue_task_dedup_select_keys_on_lead_and_provider():
    conn, cur = _mock_conn(existing=False)
    slack_enrich.enqueue_task(conn, 42, "bettercontact")
    select_sql, select_params = cur.execute.call_args_list[0][0]
    # Dedup is per (lead, provider) so two providers can queue at once.
    assert select_params == (42, "bettercontact")
    assert "payload->>'provider'" in select_sql


def test_render_response_keeps_menu_and_adds_status():
    original = [
        {"type": "section", "text": {"type": "mrkdwn", "text": "reply"}},
        _MENU_BLOCK,
    ]
    out = slack_enrich.render_response_blocks(original, "bettercontact")
    # the select menu survives the click — multi-use, not one-shot
    assert any(b.get("type") == "actions" for b in out)
    status = [b for b in out if b.get("block_id", "").startswith("enrich_status")]
    assert len(status) == 1
    assert "bettercontact" in status[0]["text"]["text"]
    # status sits directly above the still-live menu
    actions_idx = next(i for i, b in enumerate(out) if b.get("type") == "actions")
    assert out[actions_idx - 1]["block_id"].startswith("enrich_status")


def test_render_response_accumulates_providers_across_picks():
    original = [
        {"type": "section", "text": {"type": "mrkdwn", "text": "reply"}},
        _MENU_BLOCK,
    ]
    after_first = slack_enrich.render_response_blocks(original, "bettercontact")
    after_second = slack_enrich.render_response_blocks(after_first, "leadmagic")
    status = [
        b for b in after_second
        if b.get("block_id", "").startswith("enrich_status")
    ]
    assert len(status) == 1  # one status line, not stacked
    text = status[0]["text"]["text"]
    assert "bettercontact" in text and "leadmagic" in text
    # menu still present for a third pick
    assert any(b.get("type") == "actions" for b in after_second)


def test_render_response_dedups_repeated_provider():
    once = slack_enrich.render_response_blocks([_MENU_BLOCK], "leadmagic")
    twice = slack_enrich.render_response_blocks(once, "leadmagic")
    status = [b for b in twice if b.get("block_id", "").startswith("enrich_status")]
    assert status[0]["block_id"] == "enrich_status:leadmagic"


def test_parse_reply_button_extracts_modal_metadata():
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "reply"}}]
    out = slack_enrich.parse_reply_button(_reply_button_body(message_blocks=blocks))
    assert out["lead_id"] == 42
    assert out["operator"] == "Chuka"
    assert out["trigger_id"] == "trigger-123"
    assert out["response_url"] == "https://hooks.slack.com/actions/T/B/R"
    assert out["channel_id"] == "C123"
    assert out["message_ts"] == "171234.567"
    assert out["blocks"] == blocks


def test_parse_reply_modal_submission_extracts_task_payload():
    out = slack_enrich.parse_reply_modal_submission(_reply_modal_body("  Yes, happy to chat.  "))
    assert out["lead_id"] == 42
    assert out["operator"] == "Chuka"
    assert out["message"] == "Yes, happy to chat."
    assert out["slack_channel_id"] == "C123"
    assert out["slack_message_ts"] == "171234.567"
    assert out["slack_response_url"] == "https://hooks.slack.com/actions/T/B/R"
    assert out["slack_user_id"] == "U123"
    assert out["blocks"]


def test_parse_reply_modal_submission_rejects_empty_message():
    with pytest.raises(ValueError, match="empty reply body"):
        slack_enrich.parse_reply_modal_submission(_reply_modal_body("   "))


def test_render_reply_status_keeps_actions_and_replaces_old_status():
    original = [
        {"type": "section", "text": {"type": "mrkdwn", "text": "reply"}},
        {"type": "section", "block_id": "reply_status:queued", "text": {"type": "mrkdwn", "text": "old"}},
        _MENU_BLOCK,
    ]
    out = slack_enrich.render_reply_status_blocks(original, "new status")
    assert any(b.get("type") == "actions" for b in out)
    statuses = [b for b in out if b.get("block_id", "").startswith("reply_status")]
    assert len(statuses) == 1
    assert statuses[0]["text"]["text"] == "new status"


def test_enqueue_manual_reply_task_inserts_when_none_exists():
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "hi"}}]
    conn, cur = _mock_conn(existing=False)
    inserted = slack_enrich.enqueue_manual_reply_task(conn, {
        "lead_id": 42,
        "operator": "Chuka",
        "message": "Yes, happy to chat.",
        "slack_channel_id": "C123",
        "slack_message_ts": "171234.567",
        "slack_response_url": "https://hooks.slack.com/actions/T/B/R",
        "slack_user_id": "U123",
        "blocks": blocks,
    })
    assert inserted is True
    assert cur.execute.call_count == 2
    insert_sql = cur.execute.call_args_list[1][0][0]
    inserted_payload = cur.execute.call_args_list[1][0][1][0].obj
    assert "manual_reply" in insert_sql
    assert inserted_payload["slack_blocks"] == blocks
    conn.commit.assert_called_once()


def test_enqueue_manual_reply_task_dedups_pending_duplicate():
    conn, cur = _mock_conn(existing=True)
    inserted = slack_enrich.enqueue_manual_reply_task(conn, {
        "lead_id": 42,
        "operator": "Chuka",
        "message": "Yes, happy to chat.",
    })
    assert inserted is False
    assert cur.execute.call_count == 1
    conn.commit.assert_not_called()


def test_open_reply_modal_calls_slack_views_open(monkeypatch):
    monkeypatch.setattr(slack_enrich, "SLACK_BOT_TOKEN", "xoxb-test")
    thread_blocks = [{
        "type": "section",
        "block_id": "linkedin_thread_preview",
        "text": {"type": "mrkdwn", "text": "*Recent LinkedIn thread*"},
    }]
    with patch.object(slack_enrich.request, "urlopen") as mock_open:
        mock_open.return_value.__enter__.return_value.read.return_value = b'{"ok": true}'
        slack_enrich.open_reply_modal(
            trigger_id="trigger-123",
            lead_id=42,
            operator="Chuka",
            channel_id="C123",
            message_ts="171234.567",
            response_url="https://hooks.slack.com/actions/T/B/R",
            original_blocks=[],
            thread_blocks=thread_blocks,
        )
    req = mock_open.call_args[0][0]
    assert req.full_url.endswith("/views.open")
    sent = json.loads(req.data.decode("utf-8"))
    assert sent["trigger_id"] == "trigger-123"
    assert sent["view"]["callback_id"] == "linkedin_reply_modal"
    assert sent["view"]["blocks"][0]["block_id"] == "linkedin_thread_preview"
    assert sent["view"]["blocks"][-1]["type"] == "input"
    metadata = json.loads(sent["view"]["private_metadata"])
    assert metadata["response_url"] == "https://hooks.slack.com/actions/T/B/R"


def test_fetch_linkedin_thread_preview_returns_oldest_first():
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchall.return_value = [
        ("inbound", "Lead", "newer", "2026-06-15 12:02"),
        ("outbound", "Us", "older", "2026-06-15 12:01"),
    ]

    out = slack_enrich.fetch_linkedin_thread_preview(conn, 42, limit=2)

    cur.execute.assert_called_once()
    assert out[0]["body"] == "older"
    assert out[1]["body"] == "newer"


def test_render_thread_preview_blocks_escapes_and_labels_messages():
    blocks = slack_enrich.render_thread_preview_blocks([
        {
            "direction": "outbound",
            "sender": "Chuka",
            "body": "Hello <lead>",
            "sent_at": "2026-06-15 12:01",
        },
        {
            "direction": "inbound",
            "sender": "Dr. Jacquelyn Bell",
            "body": "Use A&B > C?",
            "sent_at": "2026-06-15 12:02",
        },
    ])

    assert blocks[0]["block_id"] == "linkedin_thread_preview"
    text = blocks[0]["text"]["text"]
    assert "*Us (Chuka):*" in text
    assert "Hello &lt;lead&gt;" in text
    assert "*Them (Dr. Jacquelyn Bell):*" in text
    assert "A&amp;B &gt; C" in text
    assert blocks[1]["type"] == "divider"


def test_post_response_url_updates_original_message():
    with patch.object(slack_enrich.request, "urlopen") as mock_open:
        mock_open.return_value.__enter__.return_value.status = 200
        slack_enrich._post_response_url(
            "https://hooks.slack.com/actions/T/B/R",
            {"replace_original": True, "text": "queued"},
        )
    req = mock_open.call_args[0][0]
    assert req.full_url == "https://hooks.slack.com/actions/T/B/R"
    sent = json.loads(req.data.decode("utf-8"))
    assert sent["replace_original"] is True
    assert sent["text"] == "queued"
