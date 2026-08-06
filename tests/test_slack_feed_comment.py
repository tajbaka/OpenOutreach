"""Focused tests for the isolated Slack feed-comment workflow."""
import json
from unittest.mock import MagicMock
from urllib.parse import urlencode

from api import slack_feed_comment as feed_comment


def _context():
    return {
        "post": {
            "id": 91,
            "activity_urn": "urn:li:activity:7475978266084802560",
            "post_url": "https://www.linkedin.com/feed/update/urn:li:activity:7475978266084802560",
            "author_name": "Ada Lovelace",
            "author_headline": "Security leader",
            "author_profile_url": "https://www.linkedin.com/in/ada/",
            "post_text": "Evidence quality matters more than evidence volume.",
            "intent": "high",
            "audience": "csp",
            "topics": ["FedRAMP", "evidence"],
            "relevance_reason": "A practical FedRAMP discussion.",
            "suggested_action": "Add a useful practitioner perspective.",
        },
        "senders": [
            {
                "key": "0",
                "operator": "Arian",
                "account_username": "arian@example.com",
                "label": "Arian (arian@example.com)",
            },
            {
                "key": "1",
                "operator": "Chuka",
                "account_username": "chuka@example.com",
                "label": "Eddy (chuka@example.com)",
            },
        ],
    }


def _metadata():
    return {
        "post_id": 91,
        "channel_id": "C123",
        "message_ts": "171234.567",
        "response_url": "https://hooks.slack.com/actions/T/B/R",
        "senders": _context()["senders"],
        "default_sender_key": "0",
    }


def _button_body():
    return urlencode({"payload": json.dumps({
        "type": "block_actions",
        "trigger_id": "trigger-123",
        "response_url": "https://hooks.slack.com/actions/T/B/R",
        "channel": {"id": "C123"},
        "message": {
            "ts": "171234.567",
            "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "signal"}}],
        },
        "actions": [{
            "action_id": feed_comment.COMMENT_ACTION_ID,
            "value": json.dumps({"post_id": 91}),
        }],
    })})


def _draft_body(current_comment="My starting point", sender_key="1"):
    return urlencode({"payload": json.dumps({
        "type": "block_actions",
        "view": {
            "id": "V123",
            "hash": "h123",
            "callback_id": feed_comment.COMMENT_MODAL_CALLBACK_ID,
            "private_metadata": json.dumps(_metadata()),
            "state": {"values": {
                "feed_comment_message": {
                    feed_comment.COMMENT_BODY_ACTION_ID: {"value": current_comment},
                },
                "feed_comment_sender_input": {
                    feed_comment.COMMENT_SENDER_ACTION_ID: {
                        "selected_option": {"value": sender_key},
                    },
                },
            }},
        },
        "actions": [{
            "action_id": feed_comment.COMMENT_DRAFT_ACTION_ID,
            "value": json.dumps({"post_id": 91}),
        }],
    })})


def _submission_body(message="Useful point.", sender_key="1"):
    return urlencode({"payload": json.dumps({
        "type": "view_submission",
        "user": {"id": "U123"},
        "view": {
            "callback_id": feed_comment.COMMENT_MODAL_CALLBACK_ID,
            "private_metadata": json.dumps(_metadata()),
            "state": {"values": {
                "feed_comment_message": {
                    feed_comment.COMMENT_BODY_ACTION_ID: {"value": message},
                },
                "feed_comment_sender_input": {
                    feed_comment.COMMENT_SENDER_ACTION_ID: {
                        "selected_option": {"value": sender_key},
                    },
                },
            }},
        },
    })})


def test_parse_comment_button_extracts_source_message_context():
    out = feed_comment.parse_comment_button(_button_body())

    assert out["post_id"] == 91
    assert out["trigger_id"] == "trigger-123"
    assert out["channel_id"] == "C123"
    assert out["message_ts"] == "171234.567"
    assert "blocks" not in out


def test_handle_post_open_acknowledges_url_button_interaction():
    responder = MagicMock()

    feed_comment.handle_post_open(responder, "payload=ignored")

    responder._respond_text.assert_called_once_with(200, "")


def test_fetch_feed_comment_context_returns_observing_senders():
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value = (
        91,
        "urn:li:activity:7475978266084802560",
        "https://www.linkedin.com/feed/update/urn:li:activity:7475978266084802560",
        "Ada Lovelace",
        "Security leader",
        "https://www.linkedin.com/in/ada/",
        "Evidence quality matters.",
        "high",
        "csp",
        ["FedRAMP"],
        "Relevant discussion.",
        "Add context.",
    )
    cur.fetchall.return_value = [
        ("Arian", "arian@example.com"),
        ("Chuka", "chuka@example.com"),
    ]

    out = feed_comment.fetch_feed_comment_context(conn, 91)

    assert out["post"]["author_name"] == "Ada Lovelace"
    assert [sender["operator"] for sender in out["senders"]] == ["Arian", "Chuka"]
    assert out["senders"][1]["label"].startswith("Eddy")


def test_render_modal_has_sender_select_editor_and_ai_draft_action():
    blocks = feed_comment.render_feed_comment_modal_blocks(
        _context(),
        metadata=_metadata(),
        initial_comment="Typed draft",
        selected_sender_key="1",
    )

    sender = next(block for block in blocks if block.get("block_id") == "feed_comment_sender_input")
    assert sender["element"]["initial_option"]["value"] == "1"
    assert "Eddy" in json.dumps(sender)
    message = next(block for block in blocks if block.get("block_id") == "feed_comment_message")
    assert message["element"]["initial_value"] == "Typed draft"
    assert blocks[-1]["elements"][0]["action_id"] == feed_comment.COMMENT_DRAFT_ACTION_ID


def test_parse_draft_preserves_typed_comment_and_selected_sender():
    out = feed_comment.parse_comment_draft_button(_draft_body())

    assert out["current_comment"] == "My starting point"
    assert out["selected_sender_key"] == "1"
    assert out["view_id"] == "V123"


def test_parse_submission_maps_eddy_display_choice_to_chuka_operator():
    out = feed_comment.parse_comment_modal_submission(_submission_body("  Useful point.  "))

    assert out["post_id"] == 91
    assert out["operator"] == "Chuka"
    assert out["account_username"] == "chuka@example.com"
    assert out["message"] == "Useful point."
    assert out["slack_user_id"] == "U123"


def test_enqueue_feed_comment_creates_task_and_ledger():
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.side_effect = [None, (777,)]

    task_id = feed_comment.enqueue_feed_comment_task(conn, {
        "post_id": 91,
        "operator": "Chuka",
        "account_username": "chuka@example.com",
        "message": "Useful point.",
        "slack_channel_id": "C123",
        "slack_message_ts": "171234.567",
        "slack_user_id": "U123",
    })

    assert task_id == 777
    assert cur.execute.call_count == 4
    task_sql, task_params = cur.execute.call_args_list[2][0]
    ledger_sql = cur.execute.call_args_list[3][0][0]
    assert "'feed_comment'" in task_sql
    assert task_params[0].obj["operator"] == "Chuka"
    assert "slack_blocks" not in task_params[0].obj
    assert "INSERT INTO linkedin_linkedinfeedcomment" in ledger_sql
    conn.commit.assert_called_once()


def test_enqueue_feed_comment_dedups_pending_task_under_advisory_lock():
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value = (777,)

    task_id = feed_comment.enqueue_feed_comment_task(conn, {
        "post_id": 91,
        "operator": "Arian",
        "message": "Useful point.",
    })

    assert task_id == 777
    assert cur.execute.call_count == 2
    assert "pg_advisory_xact_lock" in cur.execute.call_args_list[0][0][0]
    conn.commit.assert_called_once()


def test_submission_queues_without_replacing_source_alert(monkeypatch):
    responder = MagicMock()
    conn = MagicMock()
    connect_factory = MagicMock(return_value=conn)
    enqueue = MagicMock(return_value=777)
    monkeypatch.setattr(feed_comment, "enqueue_feed_comment_task", enqueue)
    slack_api = MagicMock()
    post_response_url = MagicMock()

    feed_comment.handle_comment_submission(
        responder,
        _submission_body(),
        connect_factory=connect_factory,
        slack_api=slack_api,
        post_response_url=post_response_url,
    )

    enqueue.assert_called_once()
    responder._respond_json.assert_called_once_with({"response_action": "clear"})
    slack_api.assert_not_called()
    post_response_url.assert_not_called()


def test_handle_draft_keeps_typed_text_while_loading_and_on_failure(monkeypatch):
    conn = MagicMock()
    connect_factory = MagicMock(return_value=conn)
    responder = MagicMock()
    updates = []
    monkeypatch.setattr(feed_comment, "fetch_feed_comment_context", lambda _conn, _post_id: _context())
    monkeypatch.setattr(
        feed_comment,
        "update_feed_comment_modal",
        lambda **kwargs: updates.append(kwargs),
    )
    monkeypatch.setattr(
        feed_comment,
        "generate_ai_feed_comment",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("model unavailable")),
    )

    feed_comment.handle_comment_draft(
        responder,
        _draft_body(current_comment="Keep this text", sender_key="1"),
        connect_factory=connect_factory,
        slack_api=MagicMock(),
    )

    assert updates[0]["initial_comment"] == "Keep this text"
    assert updates[0]["loading"]
    assert updates[1]["initial_comment"] == "Keep this text"
    assert updates[1]["selected_sender_key"] == "1"
    assert "model unavailable" in updates[1]["draft_error"]
    responder._respond_text.assert_called_once_with(200, "")
