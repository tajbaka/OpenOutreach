"""Shared transport helpers for isolated Slack feed workflows."""
from __future__ import annotations

import base64
import binascii
import json
import logging
import zlib

logger = logging.getLogger(__name__)


def encode_source_blocks(blocks: list) -> str:
    raw = json.dumps(blocks or [], separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return base64.urlsafe_b64encode(zlib.compress(raw, level=9)).decode("ascii")


def decode_source_blocks(value: str) -> list[dict]:
    if not value:
        raise ValueError("feed source blocks are missing")
    try:
        raw = zlib.decompress(base64.urlsafe_b64decode(value)).decode("utf-8")
        decoded = json.loads(raw)
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, zlib.error) as exc:
        raise ValueError("feed source blocks are malformed") from exc
    if (
        not isinstance(decoded, list)
        or not decoded
        or not all(isinstance(block, dict) for block in decoded)
    ):
        raise ValueError("feed source blocks are malformed")
    return decoded


def best_effort_source_update(
    *,
    payload: dict,
    blocks: list,
    text: str,
    slack_api,
    post_response_url,
    workflow: str,
) -> None:
    response_url = payload.get("slack_response_url") or ""
    if response_url:
        try:
            post_response_url(response_url, {
                "replace_original": True,
                "text": text,
                "blocks": blocks,
            })
            return
        except Exception:
            logger.exception("Failed to update %s through Slack response_url", workflow)

    channel_id = payload.get("slack_channel_id") or ""
    message_ts = payload.get("slack_message_ts") or ""
    if channel_id and message_ts:
        try:
            slack_api("chat.update", {
                "channel": channel_id,
                "ts": message_ts,
                "text": text,
                "blocks": blocks,
            })
            return
        except Exception:
            logger.exception("Failed to update %s through Slack chat.update", workflow)

    logger.warning("No Slack source-message update path succeeded for %s", workflow)
