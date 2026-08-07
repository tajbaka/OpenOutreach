"""Focused tests for the read-only Slack feed post context workflow."""
from datetime import datetime, timezone
import json
from unittest.mock import MagicMock, patch
from urllib.parse import urlencode

from api import slack_feed_context as feed_context


def _context(post_text="Evidence quality matters more than evidence volume."):
    seen_at = datetime(2026, 8, 6, 21, 30, tzinfo=timezone.utc)
    return {
        "post": {
            "id": 91,
            "activity_urn": "urn:li:activity:91",
            "post_url": "https://www.linkedin.com/feed/update/urn:li:activity:91/",
            "author_name": "Ada Lovelace",
            "author_headline": "Security leader",
            "author_profile_url": "https://www.linkedin.com/in/ada/",
            "post_text": post_text,
            "posted_at": seen_at,
            "first_seen_at": seen_at,
            "last_seen_at": seen_at,
            "analyzed_at": seen_at,
            "intent": "high",
            "audience": "csp",
            "topics": ["FedRAMP", "evidence"],
            "relevance_reason": "A practical FedRAMP discussion.",
            "suggested_action": "Add a useful practitioner perspective.",
            "raw_analysis": {"is_relevant": True},
        },
        "observations": [{
            "operator": "Chuka",
            "account_username": "chuka@example.com",
            "first_seen_at": seen_at,
            "last_seen_at": seen_at,
            "seen_count": 2,
        }],
    }


def _action_body(action_id, *, in_view=False):
    payload = {
        "type": "block_actions",
        "trigger_id": "trigger-123",
        "actions": [{
            "action_id": action_id,
            "value": json.dumps({"post_id": 91}),
        }],
    }
    if in_view:
        payload["view"] = {
            "id": "V123",
            "hash": "h123",
            "private_metadata": json.dumps({"post_id": 91}),
        }
    return urlencode({"payload": json.dumps(payload)})


def test_parse_post_context_actions_extract_modal_coordinates():
    opened = feed_context.parse_post_context_action(
        _action_body(feed_context.POST_CONTEXT_ACTION_ID)
    )
    generated = feed_context.parse_post_context_action(
        _action_body(feed_context.POST_CONTEXT_AI_ACTION_ID, in_view=True)
    )

    assert opened["post_id"] == 91
    assert opened["trigger_id"] == "trigger-123"
    assert generated["view_id"] == "V123"
    assert generated["view_hash"] == "h123"


def test_fetch_feed_post_context_includes_full_analysis_and_observations():
    seen_at = datetime(2026, 8, 6, 21, 30, tzinfo=timezone.utc)
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value = (
        91,
        "urn:li:activity:91",
        "",
        "Ada Lovelace",
        "Security leader",
        "https://www.linkedin.com/in/ada/",
        "The complete post.",
        seen_at,
        seen_at,
        seen_at,
        seen_at,
        "high",
        "csp",
        ["FedRAMP"],
        "Relevant discussion.",
        "Add context.",
        {"is_relevant": True},
    )
    cur.fetchall.return_value = [
        ("Arian", "arian@example.com", seen_at, seen_at, 3),
    ]

    out = feed_context.fetch_feed_post_context(conn, 91)

    assert out["post"]["post_text"] == "The complete post."
    assert out["post"]["post_url"].endswith("urn:li:activity:91/")
    assert out["post"]["raw_analysis"] == {"is_relevant": True}
    assert out["observations"][0]["seen_count"] == 3


def test_render_context_preserves_entire_long_post_before_analysis():
    post_text = (("Evidence & controls < posture.\n" * 220) + "FINAL TAIL").strip()
    blocks = feed_context.render_post_context_blocks(_context(post_text))
    full_post_blocks = [
        block for block in blocks
        if block.get("block_id", "").startswith("feed_post_context_full_post_")
    ]
    rendered = "".join(
        block["text"]["text"].removeprefix("*Full post*\n")
        for block in full_post_blocks
    )

    assert len(full_post_blocks) > 1
    assert rendered == feed_context._slack_escape(post_text)
    assert blocks[0]["block_id"] == "feed_post_context_full_post_0"
    assert blocks.index(full_post_blocks[-1]) < next(
        index for index, block in enumerate(blocks)
        if block.get("block_id") == "feed_post_context_author"
    )
    assert "Eddy" in json.dumps(blocks)


def test_render_context_ai_action_and_loading_are_mutually_exclusive():
    normal = feed_context.render_post_context_blocks(_context())
    loading = feed_context.render_post_context_blocks(_context(), loading=True)

    assert normal[-1]["elements"][0]["action_id"] == feed_context.POST_CONTEXT_AI_ACTION_ID
    assert any(block.get("block_id") == "feed_post_context_loading" for block in loading)
    assert not any(block.get("block_id") == "feed_post_context_actions" for block in loading)


def test_handle_post_context_ai_updates_loading_then_summary():
    responder = MagicMock()
    slack_api = MagicMock()
    connect_factory = MagicMock()
    connect_factory.return_value.__enter__.return_value = MagicMock()

    with (
        patch.object(feed_context, "fetch_feed_post_context", return_value=_context()),
        patch.object(feed_context, "generate_ai_post_summary", return_value="Useful summary."),
    ):
        feed_context.handle_post_context_ai(
            responder,
            _action_body(feed_context.POST_CONTEXT_AI_ACTION_ID, in_view=True),
            connect_factory=connect_factory,
            slack_api=slack_api,
        )

    assert [call.args[0] for call in slack_api.call_args_list] == [
        "views.update",
        "views.update",
    ]
    assert "Generating post summary" in json.dumps(slack_api.call_args_list[0].args[1])
    assert "Useful summary" in json.dumps(slack_api.call_args_list[1].args[1])
    responder._respond_text.assert_called_once_with(200, "")
