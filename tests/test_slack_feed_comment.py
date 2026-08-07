"""Focused tests for the isolated Slack feed-comment workflow."""
import json
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlencode

import pytest

from api import slack_feed_comment as feed_comment


def _source_blocks():
    return [
        {
            "type": "section",
            "block_id": "feed_post_date",
            "text": {"type": "mrkdwn", "text": "*Thursday August 6 2026*"},
        },
        {
            "type": "section",
            "block_id": "feed_post_author",
            "text": {"type": "mrkdwn", "text": "*Ada Lovelace*"},
        },
        {
            "type": "section",
            "block_id": "feed_post_body",
            "text": {"type": "mrkdwn", "text": "Evidence quality matters."},
        },
        {
            "type": "actions",
            "block_id": "feed_post_actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": feed_comment.COMMENT_ACTION_ID,
                    "text": {"type": "plain_text", "text": "Comment on LinkedIn"},
                    "value": json.dumps({"post_id": 91}),
                },
            ],
        },
    ]


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
        "source_blocks": feed_comment._encode_source_blocks(_source_blocks()),
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
            "blocks": _source_blocks(),
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


def _cancel_body(task_id=777):
    blocks = feed_comment.render_feed_comment_status_blocks(
        _source_blocks(),
        "queued",
        suffix="queued",
        cancel_task_id=task_id,
    )
    return urlencode({"payload": json.dumps({
        "type": "block_actions",
        "response_url": "https://hooks.slack.com/actions/T/B/CANCEL",
        "channel": {"id": "C123"},
        "message": {"ts": "171234.567", "blocks": blocks},
        "actions": [{
            "action_id": feed_comment.COMMENT_CANCEL_ACTION_ID,
            "value": json.dumps({"task_id": task_id}),
        }],
    })})


def test_parse_comment_button_extracts_source_message_context():
    out = feed_comment.parse_comment_button(_button_body())

    assert out["post_id"] == 91
    assert out["trigger_id"] == "trigger-123"
    assert out["channel_id"] == "C123"
    assert out["message_ts"] == "171234.567"
    assert out["blocks"] == _source_blocks()


def test_parse_comment_button_fails_closed_without_source_blocks():
    payload = json.loads(parse_qs(_button_body())["payload"][0])
    payload["message"]["blocks"] = []

    with pytest.raises(ValueError, match="source blocks"):
        body = urlencode({"payload": json.dumps(payload)})
        feed_comment.parse_comment_button(body)


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
    assert out["slack_blocks"] == _source_blocks()


def test_enqueue_feed_comment_creates_task_and_ledger():
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.side_effect = [None, (777,)]

    result = feed_comment.enqueue_feed_comment_task(conn, {
        "post_id": 91,
        "operator": "Chuka",
        "account_username": "chuka@example.com",
        "message": "Useful point.",
        "slack_channel_id": "C123",
        "slack_message_ts": "171234.567",
        "slack_user_id": "U123",
        "slack_blocks": _source_blocks(),
    })

    assert result == feed_comment.FeedCommentEnqueueResult(task_id=777, created=True)
    assert cur.execute.call_count == 4
    task_sql, task_params = cur.execute.call_args_list[2][0]
    ledger_sql = cur.execute.call_args_list[3][0][0]
    assert "'feed_comment'" in task_sql
    assert "interval '1 second'" in task_sql
    assert task_params[0] == 60
    assert task_params[1].obj["operator"] == "Chuka"
    assert task_params[1].obj["slack_blocks"] == _source_blocks()
    assert "INSERT INTO linkedin_linkedinfeedcomment" in ledger_sql
    conn.commit.assert_called_once()


def test_enqueue_feed_comment_dedups_pending_task_under_advisory_lock():
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value = (777,)

    result = feed_comment.enqueue_feed_comment_task(conn, {
        "post_id": 91,
        "operator": "Arian",
        "message": "Useful point.",
    })

    assert result == feed_comment.FeedCommentEnqueueResult(task_id=777, created=False)
    assert cur.execute.call_count == 2
    assert "pg_advisory_xact_lock" in cur.execute.call_args_list[0][0][0]
    conn.commit.assert_called_once()


def test_cancel_feed_comment_updates_ledger_before_deleting_pending_task():
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value = (777,)

    assert feed_comment.cancel_feed_comment_task(conn, 777) is True

    assert cur.execute.call_count == 3
    assert "status = 'pending'" in cur.execute.call_args_list[0][0][0]
    assert "UPDATE linkedin_linkedinfeedcomment" in cur.execute.call_args_list[1][0][0]
    assert "DELETE FROM linkedin_task" in cur.execute.call_args_list[2][0][0]
    conn.commit.assert_called_once()


def test_render_status_preserves_source_and_replaces_previous_status():
    queued = feed_comment.render_feed_comment_status_blocks(
        _source_blocks(),
        "queued",
        suffix="queued",
        cancel_task_id=777,
    )
    blocks = feed_comment.render_feed_comment_status_blocks(
        queued,
        "posted",
        suffix="sent",
    )

    assert queued[:3] == _source_blocks()[:3]
    assert queued[-1] == _source_blocks()[-1]
    assert queued[-2]["accessory"]["action_id"] == feed_comment.COMMENT_CANCEL_ACTION_ID
    assert json.loads(queued[-2]["accessory"]["value"]) == {"task_id": 777}
    assert blocks[:3] == _source_blocks()[:3]
    assert blocks[-1] == _source_blocks()[-1]
    assert blocks[-2]["block_id"] == "feed_comment_status:sent"
    assert "accessory" not in blocks[-2]
    assert len([b for b in blocks if b.get("block_id", "").startswith("feed_comment_status:")]) == 1


def test_submission_updates_source_alert_with_inline_cancel_status(monkeypatch):
    responder = MagicMock()
    conn = MagicMock()
    conn.__enter__.return_value = conn
    connect_factory = MagicMock(return_value=conn)
    enqueue = MagicMock(
        return_value=feed_comment.FeedCommentEnqueueResult(task_id=777, created=True),
    )
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
    response_url, slack_payload = post_response_url.call_args.args
    assert response_url == "https://hooks.slack.com/actions/T/B/R"
    assert slack_payload["replace_original"] is True
    assert slack_payload["blocks"][:3] == _source_blocks()[:3]
    assert slack_payload["blocks"][-1] == _source_blocks()[-1]
    assert slack_payload["blocks"][-2]["accessory"]["action_id"] == (
        feed_comment.COMMENT_CANCEL_ACTION_ID
    )
    slack_api.assert_not_called()


def test_duplicate_submission_does_not_update_source_status(monkeypatch):
    responder = MagicMock()
    conn = MagicMock()
    connect_factory = MagicMock(return_value=conn)
    monkeypatch.setattr(
        feed_comment,
        "enqueue_feed_comment_task",
        MagicMock(
            return_value=feed_comment.FeedCommentEnqueueResult(
                task_id=777,
                created=False,
            ),
        ),
    )
    slack_api = MagicMock()
    post_response_url = MagicMock()

    feed_comment.handle_comment_submission(
        responder,
        _submission_body(),
        connect_factory=connect_factory,
        slack_api=slack_api,
        post_response_url=post_response_url,
    )

    slack_api.assert_not_called()
    post_response_url.assert_not_called()
    responder._respond_json.assert_called_once_with({"response_action": "clear"})


def test_submission_falls_back_to_chat_update_when_response_url_fails(monkeypatch):
    responder = MagicMock()
    conn = MagicMock()
    connect_factory = MagicMock(return_value=conn)
    monkeypatch.setattr(
        feed_comment,
        "enqueue_feed_comment_task",
        MagicMock(
            return_value=feed_comment.FeedCommentEnqueueResult(
                task_id=777,
                created=True,
            ),
        ),
    )
    slack_api = MagicMock(return_value={"ok": True})
    post_response_url = MagicMock(side_effect=RuntimeError("expired response URL"))

    feed_comment.handle_comment_submission(
        responder,
        _submission_body(),
        connect_factory=connect_factory,
        slack_api=slack_api,
        post_response_url=post_response_url,
    )

    method, slack_payload = slack_api.call_args.args
    assert method == "chat.update"
    assert slack_payload["channel"] == "C123"
    assert slack_payload["ts"] == "171234.567"
    assert slack_payload["blocks"][-2]["block_id"] == "feed_comment_status:queued"
    responder._respond_json.assert_called_once_with({"response_action": "clear"})


def test_cancel_replaces_only_inline_source_status(monkeypatch):
    responder = MagicMock()
    conn = MagicMock()
    connect_factory = MagicMock(return_value=conn)
    monkeypatch.setattr(feed_comment, "cancel_feed_comment_task", lambda *_args: True)
    slack_api = MagicMock()
    post_response_url = MagicMock()

    feed_comment.handle_comment_cancel(
        responder,
        _cancel_body(),
        connect_factory=connect_factory,
        slack_api=slack_api,
        post_response_url=post_response_url,
    )

    response_url, payload = post_response_url.call_args.args
    blocks = payload["blocks"]
    assert response_url == "https://hooks.slack.com/actions/T/B/CANCEL"
    assert payload["replace_original"] is True
    assert blocks[:3] == _source_blocks()[:3]
    assert blocks[-1] == _source_blocks()[-1]
    assert "cancelled" in blocks[-2]["text"]["text"]
    assert "accessory" not in blocks[-2]
    slack_api.assert_not_called()
    responder._respond_text.assert_called_once_with(200, "")


def test_cancel_falls_back_to_original_chat_update(monkeypatch):
    responder = MagicMock()
    conn = MagicMock()
    connect_factory = MagicMock(return_value=conn)
    monkeypatch.setattr(feed_comment, "cancel_feed_comment_task", lambda *_args: True)
    slack_api = MagicMock(return_value={"ok": True})
    post_response_url = MagicMock(side_effect=RuntimeError("expired response URL"))

    feed_comment.handle_comment_cancel(
        responder,
        _cancel_body(),
        connect_factory=connect_factory,
        slack_api=slack_api,
        post_response_url=post_response_url,
    )

    method, payload = slack_api.call_args.args
    assert method == "chat.update"
    assert payload["channel"] == "C123"
    assert payload["ts"] == "171234.567"
    assert payload["blocks"][-2]["block_id"] == "feed_comment_status:cancelled"
    assert "accessory" not in payload["blocks"][-2]
    responder._respond_text.assert_called_once_with(200, "")


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
