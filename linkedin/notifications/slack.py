"""Slack notifications via incoming webhook.

Triggered when a connection invite gets accepted (PENDING → CONNECTED via
the sweep). Sends a single Block Kit message with the lead's name, role,
and profile link. No-op when SLACK_WEBHOOK_URL is unset, so callers don't
need to guard.
"""
from __future__ import annotations

import json
import logging
from urllib import request
from urllib.error import URLError

from linkedin.conf import SLACK_WEBHOOK_URL

logger = logging.getLogger(__name__)


def notify_connection_accepted(
    *,
    full_name: str,
    title: str,
    company: str,
    profile_url: str,
    campaign_name: str,
    reply_text: str | None = None,
    operator: str = "",
) -> None:
    """Post a 'connection accepted' message to Slack. Silent no-op if disabled.

    `reply_text` (when truthy) signals the lead also replied to your note —
    the notification then highlights the reply rather than just the accept.
    `operator` is the display name of the account that owns this lead
    (Chuka / Arian) — derived by the caller from `session.linkedin_profile`.
    Rendered alongside Campaign so the team knows whose lead it is at a glance.
    """
    if not SLACK_WEBHOOK_URL:
        return

    headline = " · ".join(p for p in (title, company) if p)
    operator_clean = (operator or "").strip()

    def _context_elements() -> list[dict]:
        elements: list[dict] = []
        if operator_clean:
            elements.append({"type": "mrkdwn", "text": f"*Lead for:* {operator_clean}"})
        elements.append({"type": "mrkdwn", "text": f"*Campaign:* {campaign_name}"})
        if headline:
            elements.append({"type": "mrkdwn", "text": f"*Role:* {headline}"})
        return elements

    op_suffix = f" — {operator_clean}'s lead" if operator_clean else ""

    if reply_text:
        emoji = ":speech_balloon:"
        action_line = f"{emoji} *<{profile_url}|{full_name}>* accepted *and replied*{op_suffix}"
        fallback = f"{emoji} {full_name} accepted and replied ({campaign_name}){op_suffix}"
        snippet = reply_text.strip().replace("\n", " ")
        if len(snippet) > 280:
            snippet = snippet[:277] + "..."
        blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": action_line}},
            {"type": "section", "text": {"type": "mrkdwn", "text": f"> {snippet}"}},
            {"type": "context", "elements": _context_elements()},
        ]
    else:
        emoji = ":handshake:"
        action_line = (
            f"{emoji} *<{profile_url}|{full_name}>* accepted your invite (no reply yet){op_suffix}"
        )
        fallback = f"{emoji} {full_name} accepted your invite ({campaign_name}){op_suffix}"
        blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": action_line}},
            {"type": "context", "elements": _context_elements()},
        ]

    payload = {"text": fallback, "blocks": blocks}

    body = json.dumps(payload).encode("utf-8")
    req = request.Request(
        SLACK_WEBHOOK_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=10) as resp:
            if resp.status != 200:
                logger.warning(
                    "Slack webhook returned %d for %s", resp.status, full_name
                )
    except (URLError, TimeoutError) as e:
        logger.warning("Slack webhook failed for %s: %s", full_name, e)


def latest_reply_from_lead(messages: list[dict] | None, lead_full_name: str) -> dict | None:
    """Return the most recent message dict where the sender is the lead.

    `messages` is the list returned by `get_conversation()` —
    [{sender, text, timestamp}, ...] sorted oldest-first. None / empty means
    no conversation exists. Match is case-insensitive on the lead's name.

    Returns the raw message dict so callers can pull both `.text` and
    `.timestamp` (e.g. to update `Deal.last_reply_at`).
    """
    if not messages:
        return None
    target = lead_full_name.strip().lower()
    if not target:
        return None
    lead_messages = [
        m for m in messages
        if (m.get("sender") or "").strip().lower() == target and (m.get("text") or "").strip()
    ]
    if not lead_messages:
        return None
    return lead_messages[-1]
