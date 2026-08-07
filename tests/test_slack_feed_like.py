"""Focused tests for the standalone Slack feed Like workflow."""
import json
from unittest.mock import MagicMock
from urllib.parse import urlencode

from api import slack_feed_like as feed_like


def _source_blocks():
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": "Feed post"}},
        {"type": "actions", "elements": []},
    ]


def _context():
    return {
        "post": {
            "id": 91,
            "author_name": "Ada Lovelace",
            "author_profile_url": "https://www.linkedin.com/in/ada/",
            "post_text": "Evidence quality matters.",
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


def _button_body():
    return urlencode({"payload": json.dumps({
        "type": "block_actions",
        "trigger_id": "trigger-123",
        "response_url": "https://hooks.slack.com/actions/T/B/R",
        "channel": {"id": "C123"},
        "message": {"ts": "171234.567", "blocks": _source_blocks()},
        "actions": [{
            "action_id": feed_like.LIKE_ACTION_ID,
            "value": json.dumps({"post_id": 91}),
        }],
    })})


def _submission_body(sender_key="1"):
    metadata = {
        "post_id": 91,
        "channel_id": "C123",
        "message_ts": "171234.567",
        "response_url": "https://hooks.slack.com/actions/T/B/R",
        "source_blocks": feed_like.encode_source_blocks(_source_blocks()),
        "senders": _context()["senders"],
        "default_sender_key": "0",
    }
    return urlencode({"payload": json.dumps({
        "type": "view_submission",
        "user": {"id": "U123"},
        "view": {
            "callback_id": feed_like.LIKE_MODAL_CALLBACK_ID,
            "private_metadata": json.dumps(metadata),
            "state": {"values": {
                "feed_like_sender_input": {
                    feed_like.LIKE_SENDER_ACTION_ID: {
                        "selected_option": {"value": sender_key},
                    },
                },
            }},
        },
    })})


def test_parse_like_button_preserves_source_alert_coordinates():
    out = feed_like.parse_like_button(_button_body())

    assert out["post_id"] == 91
    assert out["trigger_id"] == "trigger-123"
    assert out["blocks"] == _source_blocks()


def test_parse_like_submission_maps_eddy_to_chuka():
    out = feed_like.parse_like_submission(_submission_body())

    assert out["operator"] == "Chuka"
    assert out["account_username"] == "chuka@example.com"
    assert out["slack_blocks"] == _source_blocks()


def test_render_like_modal_offers_sender_choice_and_safety_note():
    blocks = feed_like.render_feed_like_modal_blocks(_context(), metadata={})
    sender = next(block for block in blocks if block.get("block_id") == "feed_like_sender_input")

    assert len(sender["element"]["options"]) == 2
    assert "Eddy" in json.dumps(sender)
    assert "will not be toggled off" in json.dumps(blocks)


def test_enqueue_feed_like_task_is_sender_post_deduped():
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.side_effect = [None, (321,)]
    payload = {
        "post_id": 91,
        "operator": "Arian",
        "account_username": "arian@example.com",
        "slack_blocks": _source_blocks(),
    }

    result = feed_like.enqueue_feed_like_task(conn, payload)

    assert result == feed_like.FeedLikeEnqueueResult(task_id=321, created=True)
    insert_sql = cur.execute.call_args_list[-1].args[0]
    assert "'feed_like'" in insert_sql
    conn.commit.assert_called_once()


def test_feed_like_status_replaces_only_its_own_status():
    original = [
        {"type": "section", "block_id": "feed_comment_status:sent", "text": {"text": "comment"}},
        {"type": "section", "block_id": "feed_like_status:queued", "text": {"text": "queued"}},
        {"type": "actions", "elements": []},
    ]

    rendered = feed_like.render_feed_like_status_blocks(
        original,
        "liked",
        suffix="liked",
    )

    assert any(block.get("block_id") == "feed_comment_status:sent" for block in rendered)
    assert not any(block.get("block_id") == "feed_like_status:queued" for block in rendered)
    assert any(block.get("block_id") == "feed_like_status:liked" for block in rendered)
