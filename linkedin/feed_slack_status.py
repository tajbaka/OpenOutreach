"""Pure Slack block rendering for the LinkedIn feed-comment workflow."""
from __future__ import annotations

import json

COMMENT_CANCEL_ACTION_ID = "linkedin_feed_comment_cancel_button"
STATUS_BLOCK_PREFIX = "feed_comment_status"


def render_feed_comment_status_blocks(
    original_blocks: list[dict],
    status_text: str,
    *,
    suffix: str,
    cancel_task_id: int | None = None,
) -> list[dict]:
    """Replace the feed-comment status while retaining every source block."""
    status: dict = {
        "type": "section",
        "block_id": f"{STATUS_BLOCK_PREFIX}:{suffix}",
        "text": {"type": "mrkdwn", "text": status_text},
    }
    if cancel_task_id is not None:
        status["accessory"] = {
            "type": "button",
            "action_id": COMMENT_CANCEL_ACTION_ID,
            "text": {"type": "plain_text", "text": "Cancel queued comment"},
            "style": "danger",
            "value": json.dumps(
                {"task_id": int(cancel_task_id)},
                separators=(",", ":"),
            ),
            "confirm": {
                "title": {"type": "plain_text", "text": "Cancel comment?"},
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "This removes the queued public comment if its sender "
                        "has not started."
                    ),
                },
                "confirm": {"type": "plain_text", "text": "Cancel comment"},
                "deny": {"type": "plain_text", "text": "Keep queued"},
            },
        }

    rendered: list[dict] = []
    inserted = False
    for block in original_blocks:
        if (block.get("block_id") or "").startswith(f"{STATUS_BLOCK_PREFIX}:"):
            continue
        if block.get("type") == "actions" and not inserted:
            rendered.append(status)
            inserted = True
        rendered.append(block)
    if not inserted:
        rendered.append(status)
    return rendered
