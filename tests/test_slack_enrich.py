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
    payload = {
        "actions": [{
            "action_id": "enrich_phone_select",
            "selected_option": {"value": value},
        }],
    }
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


def _reply_cancel_body(task_id: int = 777, message_blocks=None) -> str:
    payload = {
        "type": "block_actions",
        "channel": {"id": "C123"},
        "message": {"ts": "171234.567", "blocks": message_blocks or []},
        "actions": [{
            "action_id": "linkedin_reply_cancel_button",
            "value": json.dumps({"task_id": task_id}),
        }],
    }
    return urlencode({"payload": json.dumps(payload)})


def _lead_context_body(
    action_id: str = "linkedin_lead_context_button",
    view_metadata=None,
) -> str:
    payload = {
        "type": "block_actions",
        "trigger_id": "trigger-ctx",
        "view": {
            "id": "V123",
            "hash": "h123",
            "private_metadata": json.dumps(view_metadata or {}),
        },
        "actions": [{
            "action_id": action_id,
            "value": json.dumps({
                "lead_id": 42,
                "operator": "Arian",
                "thread_external_id": "thread-arian",
            }),
        }],
    }
    return urlencode({"payload": json.dumps(payload)})


def _reply_draft_body(current_reply: str = "", metadata=None) -> str:
    metadata = metadata or {
        "lead_id": 42,
        "operator": "Chuka",
        "channel_id": "C123",
        "message_ts": "171234.567",
        "response_url": "https://hooks.slack.com/actions/T/B/R",
        "thread_external_id": "thread-chuka",
        "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "hi"}}],
        "thread_blocks": [{"type": "section", "block_id": "linkedin_thread_preview_header"}],
    }
    payload = {
        "type": "block_actions",
        "view": {
            "id": "V-reply",
            "hash": "h-reply",
            "callback_id": "linkedin_reply_modal",
            "private_metadata": json.dumps(metadata),
            "state": {
                "values": {
                    "linkedin_reply_message": {
                        "linkedin_reply_body": {"value": current_reply},
                    },
                },
            },
        },
        "actions": [{
            "action_id": "linkedin_reply_draft_button",
            "value": json.dumps({
                "lead_id": 42,
                "operator": "Chuka",
                "thread_external_id": "thread-chuka",
            }),
        }],
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


def test_interaction_intent_routes_known_actions():
    cases = [
        (_reply_button_body(), "reply_button"),
        (_reply_cancel_body(), "reply_cancel"),
        (_lead_context_body("linkedin_lead_context_button"), "lead_context"),
        (_lead_context_body("linkedin_lead_context_ai_button"), "lead_context_ai"),
        (_lead_context_body("linkedin_lead_context_draft_button"), "lead_context_draft"),
        (_reply_draft_body(), "reply_draft"),
        (_interaction_body("42:waterfall"), "enrich_phone"),
        (_reply_modal_body(), "reply_submission"),
    ]
    for body, expected in cases:
        payload = slack_enrich.decode_slack_payload(body)
        assert slack_enrich.interaction_intent(payload) == expected


def test_interaction_intent_rejects_unknown_select_action():
    payload = {
        "type": "block_actions",
        "actions": [{
            "action_id": "other_select",
            "selected_option": {"value": "42:waterfall"},
        }],
    }

    with pytest.raises(ValueError, match="unsupported Slack action"):
        slack_enrich.interaction_intent(payload)


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


def test_compact_metadata_preserves_source_blocks_before_thread_preview():
    source_blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": "source"}},
        {"type": "actions", "elements": []},
    ]
    thread_blocks = [
        {
            "type": "section",
            "block_id": f"linkedin_thread_preview:{idx}",
            "text": {"type": "mrkdwn", "text": "x" * 400},
        }
        for idx in range(10)
    ]

    out = json.loads(slack_enrich._compact_metadata({
        "lead_id": 42,
        "operator": "Chuka",
        "channel_id": "C123",
        "message_ts": "171234.567",
        "response_url": "https://hooks.slack.com/actions/T/B/R",
        "blocks": source_blocks,
        "thread_blocks": thread_blocks,
    }))

    assert out["blocks"] == source_blocks
    assert out["thread_blocks"] == []
    assert len(json.dumps(out, separators=(",", ":"))) <= 2800


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


def test_render_reply_status_can_include_cancel_button():
    out = slack_enrich.render_reply_status_blocks(
        [_MENU_BLOCK],
        "queued",
        cancel_task_id=777,
    )
    status = next(b for b in out if b.get("block_id") == "reply_status:queued")
    button = status["accessory"]
    assert button["action_id"] == "linkedin_reply_cancel_button"
    assert json.loads(button["value"]) == {"task_id": 777}


def test_parse_reply_cancel_button_extracts_task_and_blocks():
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "reply"}}]

    out = slack_enrich.parse_reply_cancel_button(
        _reply_cancel_body(task_id=777, message_blocks=blocks),
    )

    assert out["task_id"] == 777
    assert out["blocks"] == blocks


def test_enqueue_manual_reply_task_inserts_when_none_exists():
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "hi"}}]
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.side_effect = [None, (777,)]
    task_id = slack_enrich.enqueue_manual_reply_task(conn, {
        "lead_id": 42,
        "operator": "Chuka",
        "message": "Yes, happy to chat.",
        "slack_channel_id": "C123",
        "slack_message_ts": "171234.567",
        "slack_response_url": "https://hooks.slack.com/actions/T/B/R",
        "slack_user_id": "U123",
        "blocks": blocks,
    })
    assert task_id == 777
    assert cur.execute.call_count == 2
    insert_sql = cur.execute.call_args_list[1][0][0]
    inserted_payload = cur.execute.call_args_list[1][0][1][0].obj
    assert "manual_reply" in insert_sql
    assert "RETURNING id" in insert_sql
    assert inserted_payload["slack_blocks"] == blocks
    conn.commit.assert_called_once()


def test_enqueue_manual_reply_task_dedups_pending_duplicate():
    conn, cur = _mock_conn(existing=True)
    task_id = slack_enrich.enqueue_manual_reply_task(conn, {
        "lead_id": 42,
        "operator": "Chuka",
        "message": "Yes, happy to chat.",
    })
    assert task_id == 1
    assert cur.execute.call_count == 1
    conn.commit.assert_not_called()


def test_cancel_manual_reply_task_deletes_pending_task():
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value = (777,)

    cancelled = slack_enrich.cancel_manual_reply_task(conn, 777)

    assert cancelled is True
    sql, params = cur.execute.call_args[0]
    assert "DELETE FROM linkedin_task" in sql
    assert "status = 'pending'" in sql
    assert params == (777,)
    conn.commit.assert_called_once()


def test_cancel_manual_reply_task_returns_false_when_not_pending():
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value = None

    cancelled = slack_enrich.cancel_manual_reply_task(conn, 777)

    assert cancelled is False
    conn.commit.assert_called_once()


def test_open_reply_modal_calls_slack_views_open(monkeypatch):
    monkeypatch.setattr(slack_enrich, "SLACK_BOT_TOKEN", "xoxb-test")
    thread_blocks = [{
        "type": "section",
        "block_id": "linkedin_thread_preview_header",
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
            thread_external_id="thread-arian",
            thread_blocks=thread_blocks,
        )
    req = mock_open.call_args[0][0]
    assert req.full_url.endswith("/views.open")
    sent = json.loads(req.data.decode("utf-8"))
    assert sent["trigger_id"] == "trigger-123"
    assert sent["view"]["callback_id"] == "linkedin_reply_modal"
    assert sent["view"]["blocks"][0]["block_id"] == "linkedin_thread_preview_header"
    assert sent["view"]["blocks"][-2]["type"] == "input"
    assert sent["view"]["blocks"][-1]["block_id"] == "linkedin_reply_actions"
    assert sent["view"]["blocks"][-1]["elements"][0]["action_id"] == "linkedin_reply_draft_button"
    metadata = json.loads(sent["view"]["private_metadata"])
    assert metadata["response_url"] == "https://hooks.slack.com/actions/T/B/R"
    assert metadata["thread_external_id"] == "thread-arian"
    assert metadata["thread_blocks"][0]["block_id"] == "linkedin_thread_preview_header"


def test_update_slack_view_can_persist_lead_context_metadata(monkeypatch):
    monkeypatch.setattr(slack_enrich, "SLACK_BOT_TOKEN", "xoxb-test")

    with patch.object(slack_enrich.request, "urlopen") as mock_open:
        mock_open.return_value.__enter__.return_value.read.return_value = b'{"ok": true}'
        slack_enrich.update_slack_view(
            view_id="V123",
            blocks=[],
            private_metadata=json.dumps({
                "lead_id": 42,
                "operator": "Arian",
                "thread_external_id": "thread-arian",
                "ai_summary": "Summary",
                "draft_reply": "Draft\n\nReply",
            }),
        )

    req = mock_open.call_args[0][0]
    assert req.full_url.endswith("/views.update")
    sent = json.loads(req.data.decode("utf-8"))
    metadata = json.loads(sent["view"]["private_metadata"])
    assert metadata["ai_summary"] == "Summary"
    assert metadata["draft_reply"] == "Draft\n\nReply"


def test_parse_reply_button_accepts_thread_scoped_json_value():
    value = json.dumps({
        "lead_id": 42,
        "operator": "Arian",
        "thread_external_id": "thread-arian",
    })

    out = slack_enrich.parse_reply_button(_reply_button_body(value=value))

    assert out["lead_id"] == 42
    assert out["operator"] == "Arian"
    assert out["thread_external_id"] == "thread-arian"


def test_parse_reply_button_accepts_legacy_colon_value():
    out = slack_enrich.parse_reply_button(_reply_button_body(value="42:Chuka"))

    assert out["lead_id"] == 42
    assert out["operator"] == "Chuka"
    assert out["thread_external_id"] == ""


def test_parse_lead_context_button_extracts_metadata():
    out = slack_enrich.parse_lead_context_button(
        _lead_context_body("linkedin_lead_context_draft_button"),
    )

    assert out["lead_id"] == 42
    assert out["operator"] == "Arian"
    assert out["thread_external_id"] == "thread-arian"
    assert out["trigger_id"] == "trigger-ctx"
    assert out["view_id"] == "V123"
    assert out["view_hash"] == "h123"
    assert out["action_id"] == "linkedin_lead_context_draft_button"


def test_parse_lead_context_button_preserves_generated_modal_metadata():
    out = slack_enrich.parse_lead_context_button(
        _lead_context_body(
            "linkedin_lead_context_draft_button",
            view_metadata={
                "lead_id": 42,
                "operator": "Arian",
                "thread_external_id": "thread-arian",
                "ai_summary": "Existing summary",
                "draft_reply": "Existing draft\n\nWith spacing",
            },
        ),
    )

    assert out["ai_summary"] == "Existing summary"
    assert out["draft_reply"] == "Existing draft\n\nWith spacing"


def test_parse_reply_draft_button_extracts_reply_modal_metadata():
    out = slack_enrich.parse_reply_draft_button(
        _reply_draft_body("Existing typed reply"),
    )

    assert out["lead_id"] == 42
    assert out["operator"] == "Chuka"
    assert out["thread_external_id"] == "thread-chuka"
    assert out["view_id"] == "V-reply"
    assert out["view_hash"] == "h-reply"
    assert out["current_reply"] == "Existing typed reply"
    assert out["metadata"]["channel_id"] == "C123"
    assert out["metadata"]["thread_blocks"][0]["block_id"] == "linkedin_thread_preview_header"


def test_update_reply_modal_prefills_generated_draft(monkeypatch):
    monkeypatch.setattr(slack_enrich, "SLACK_BOT_TOKEN", "xoxb-test")
    metadata = {
        "lead_id": 42,
        "operator": "Chuka",
        "channel_id": "C123",
        "message_ts": "171234.567",
        "thread_external_id": "thread-chuka",
        "blocks": [],
        "thread_blocks": [{"type": "section", "block_id": "linkedin_thread_preview_header"}],
    }

    with patch.object(slack_enrich.request, "urlopen") as mock_open:
        mock_open.return_value.__enter__.return_value.read.return_value = b'{"ok": true}'
        slack_enrich.update_reply_modal(
            view_id="V-reply",
            metadata=metadata,
            initial_reply="Suggested reply",
        )

    req = mock_open.call_args[0][0]
    assert req.full_url.endswith("/views.update")
    sent = json.loads(req.data.decode("utf-8"))
    assert sent["view"]["callback_id"] == "linkedin_reply_modal"
    blocks = sent["view"]["blocks"]
    assert blocks[0]["block_id"] == "linkedin_thread_preview_header"
    reply_input = next(b for b in blocks if b.get("block_id") == "linkedin_reply_message")
    assert reply_input["element"]["initial_value"] == "Suggested reply"
    assert blocks[-1]["block_id"] == "linkedin_reply_actions"


def test_render_lead_context_blocks_includes_ai_action_only():
    context = {
        "lead": {
            "id": 42,
            "first_name": "Jacquelyn",
            "last_name": "Bell",
            "company_name": "JB Choices",
            "linkedin_url": "https://www.linkedin.com/in/jacquelyn-bell/",
            "public_identifier": "jacquelyn-bell",
            "description": json.dumps({
                "headline": "FedRAMP advisor",
                "summary": "Works with public sector compliance teams.",
            }),
            "icp": "Advisor",
        },
        "deals": [{
            "owner": "Arian",
            "campaign": "FedRampGPT",
            "state": "CONNECTED",
        }],
        "messages": [{
            "direction": "inbound",
            "sender": "Jacquelyn Bell",
            "body": "Tell me more.",
        }],
        "operator": "Arian",
        "thread_external_id": "thread-arian",
    }

    blocks = slack_enrich.render_lead_context_blocks(
        context,
        ai_summary="Advisor lead. Keep it light.",
        draft_reply="Happy to explain.",
    )

    body = json.dumps(blocks)
    assert "Jacquelyn Bell" in body
    assert "Advisor lead" in body
    assert "Happy to explain" in body
    actions = next(b for b in blocks if b.get("block_id") == "lead_context_actions")
    action_ids = {el["action_id"] for el in actions["elements"]}
    assert action_ids == {"linkedin_lead_context_ai_button"}


def test_render_lead_context_blocks_hides_actions_while_loading():
    context = {
        "lead": {
            "id": 42,
            "first_name": "Ada",
            "last_name": "Lovelace",
            "company_name": "Analytical Engines",
            "linkedin_url": "",
            "public_identifier": "ada",
            "description": "{}",
            "icp": "CSP",
        },
        "deals": [],
        "messages": [],
        "operator": "Arian",
        "thread_external_id": "thread-arian",
    }

    blocks = slack_enrich.render_lead_context_blocks(
        context,
        loading="Drafting reply...",
    )

    assert any(b.get("block_id") == "lead_context_loading" for b in blocks)
    assert not any(b.get("block_id") == "lead_context_actions" for b in blocks)
    assert blocks[-1]["block_id"] == "lead_context_loading"


def test_render_lead_context_blocks_puts_newest_artifact_at_bottom():
    context = {
        "lead": {
            "id": 42,
            "first_name": "Ada",
            "last_name": "Lovelace",
            "company_name": "Analytical Engines",
            "linkedin_url": "",
            "public_identifier": "ada",
            "description": "{}",
            "icp": "CSP",
        },
        "deals": [],
        "messages": [],
        "operator": "Arian",
        "thread_external_id": "thread-arian",
    }

    blocks = slack_enrich.render_lead_context_blocks(
        context,
        ai_summary="Newest summary",
        draft_reply="Existing draft",
        newest_artifact="ai_summary",
    )

    generated_ids = [
        b["block_id"]
        for b in blocks
        if b.get("block_id") in {
            "lead_context_ai_summary",
            "lead_context_draft_reply",
        }
    ]
    assert generated_ids == [
        "lead_context_draft_reply",
        "lead_context_ai_summary",
    ]


def test_render_lead_context_blocks_uses_saved_artifacts_by_default():
    context = {
        "lead": {
            "id": 42,
            "first_name": "Jacquelyn",
            "last_name": "Bell",
            "company_name": "JB Choices",
            "linkedin_url": "",
            "public_identifier": "jacquelyn-bell",
            "description": "{}",
            "icp": "Advisor",
        },
        "deals": [],
        "messages": [],
        "operator": "Arian",
        "thread_external_id": "thread-arian",
        "artifacts": {
            "ai_summary": "Saved summary",
            "draft_reply": "Saved draft",
        },
    }

    blocks = slack_enrich.render_lead_context_blocks(context)

    body = json.dumps(blocks)
    assert "Saved summary" in body
    assert "Saved draft" in body


def test_lead_context_metadata_falls_back_to_saved_artifacts():
    context = {
        "lead": {"id": 42},
        "operator": "Arian",
        "thread_external_id": "thread-arian",
        "artifacts": {
            "ai_summary": "Saved summary",
            "draft_reply": "Saved draft\n\nWith spacing",
        },
    }

    metadata = json.loads(slack_enrich._lead_context_metadata(context))

    assert metadata["ai_summary"] == "Saved summary"
    assert metadata["draft_reply"] == "Saved draft\n\nWith spacing"


def test_fetch_lead_context_artifacts_scopes_by_sender_and_thread():
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchall.return_value = [
        ("ai_summary", "Saved summary"),
        ("draft_reply", "Saved draft"),
    ]

    out = slack_enrich.fetch_lead_context_artifacts(
        conn,
        42,
        operator="Arian",
        thread_external_id="thread-arian",
    )

    sql, params = cur.execute.call_args[0]
    assert "linkedin_slackleadcontextartifact" in sql
    assert '"operator"' in sql
    assert params == (42, "Arian", "thread-arian", "ai_summary", "draft_reply")
    assert out == {"ai_summary": "Saved summary", "draft_reply": "Saved draft"}


def test_upsert_lead_context_artifact_uses_scope_conflict_key():
    conn = MagicMock()

    slack_enrich.upsert_lead_context_artifact(
        conn,
        lead_id=42,
        operator="Arian",
        thread_external_id="thread-arian",
        kind="draft_reply",
        content="Saved draft",
    )

    cur = conn.cursor.return_value.__enter__.return_value
    sql, params = cur.execute.call_args[0]
    assert "ON CONFLICT" in sql
    assert "(lead_id, \"operator\", thread_external_id, kind)" in sql
    assert params == (42, "Arian", "thread-arian", "draft_reply", "Saved draft")
    conn.commit.assert_called_once()


def test_upsert_lead_context_artifact_rejects_unknown_kind():
    conn = MagicMock()

    with pytest.raises(ValueError, match="unsupported lead context artifact kind"):
        slack_enrich.upsert_lead_context_artifact(
            conn,
            lead_id=42,
            kind="unknown",
            content="bad",
        )

    conn.cursor.assert_not_called()


def test_fetch_linkedin_thread_preview_returns_oldest_first():
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchall.return_value = [
        ("inbound", "Lead", "newer", "2026-06-15 12:02"),
        ("outbound", "Us", "older", "2026-06-15 12:01"),
    ]

    out = slack_enrich.fetch_linkedin_thread_preview(
        conn, 42, thread_external_id="thread-arian", limit=2,
    )

    cur.execute.assert_called_once()
    assert cur.execute.call_args[0][1] == (42, "thread-arian", 2)
    assert out[0]["body"] == "older"
    assert out[1]["body"] == "newer"


def test_fetch_linkedin_thread_preview_uses_latest_inbound_thread_fallback():
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value = ("thread-arian",)
    cur.fetchall.return_value = [
        ("inbound", "Paul", "newer", "2026-06-15 12:02"),
        ("outbound", "Arian", "older", "2026-06-15 12:01"),
    ]

    out = slack_enrich.fetch_linkedin_thread_preview(conn, 42, limit=2)

    assert cur.execute.call_count == 2
    assert cur.execute.call_args_list[1][0][1] == (42, "thread-arian", 2)
    assert [msg["body"] for msg in out] == ["older", "newer"]


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

    assert blocks[0]["block_id"] == "linkedin_thread_preview_header"
    assert blocks[1]["block_id"].startswith("linkedin_thread_preview:")
    assert "*Chuka*" in blocks[1]["text"]["text"]
    assert "Hello &lt;lead&gt;" in blocks[1]["text"]["text"]
    assert blocks[2]["block_id"].startswith("linkedin_thread_preview:")
    assert "*Dr. Jacquelyn Bell*" in blocks[2]["text"]["text"]
    assert "A&amp;B &gt; C" in blocks[2]["text"]["text"]
    assert blocks[3]["type"] == "divider"


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
