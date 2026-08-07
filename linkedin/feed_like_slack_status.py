"""Pure Slack status-block rendering for standalone feed Like tasks."""
from __future__ import annotations

STATUS_BLOCK_PREFIX = "feed_like_status"


def render_feed_like_status_blocks(
    original_blocks: list[dict],
    status_text: str,
    *,
    suffix: str,
) -> list[dict]:
    status = {
        "type": "section",
        "block_id": f"{STATUS_BLOCK_PREFIX}:{suffix}",
        "text": {"type": "mrkdwn", "text": status_text},
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
