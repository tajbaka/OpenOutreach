"""Pure parser for one decoded LinkedIn realtime event.

Input is a single SSE event already JSON-decoded (see linkedin/realtime/sse.py).
Output is a ParsedRealtimeMessage for `messagesTopic` events, or None for
everything else (heartbeats, typing indicators, read receipts, badge
updates, conversation updates, unrecognised shapes).

Field paths confirmed against tests/fixtures/realtime/SHAPE.md.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone as dt_timezone

logger = logging.getLogger(__name__)

_DECORATED_EVENT_KEY = "com.linkedin.realtimefrontend.DecoratedEvent"
_MESSAGES_TOPIC = ":messagesTopic:"


@dataclass(frozen=True)
class ParsedRealtimeMessage:
    """A message extracted from one realtime event.

    `timestamp` is formatted "YYYY-MM-DD HH:MM" so it feeds straight into
    `linkedin.db.messages.persist_thread` (which expects that format).
    """

    entity_urn: str          # message URN (backendUrn) — idempotency key
    conversation_urn: str    # conversation.entityUrn — matches Message.thread_external_id
    sender_name: str
    sender_member_urn: str
    text: str
    timestamp: str


def _epoch_ms_to_str(value) -> str:
    """Format an epoch-millisecond timestamp as 'YYYY-MM-DD HH:MM'."""
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return ""
    return datetime.fromtimestamp(ms / 1000, tz=dt_timezone.utc).strftime("%Y-%m-%d %H:%M")


def _attributed_text(value) -> str:
    """Extract `.text` from a LinkedIn AttributedText dict, else ''."""
    if isinstance(value, dict):
        text = value.get("text")
        if isinstance(text, str):
            return text
    return ""


def parse_realtime_event(event) -> ParsedRealtimeMessage | None:
    """Parse one decoded realtime event.

    Returns a ParsedRealtimeMessage for `messagesTopic` events, None for
    everything else (heartbeats, typing, read receipts, badge updates,
    unrecognised or malformed shapes). Never raises — any exception while
    walking a malformed payload is caught and yields None.

    Note: `entity_urn` and `text` on a returned message are always non-empty;
    `conversation_urn`, `sender_member_urn`, and `sender_name` may be empty
    strings if the payload omits them (the handler tolerates that).
    """
    try:
        return _parse(event)
    except Exception as e:
        logger.debug("parse_realtime_event: undecodable event dropped: %s", e)
        return None


def _parse(event) -> ParsedRealtimeMessage | None:
    if not isinstance(event, dict):
        return None
    decorated = event.get(_DECORATED_EVENT_KEY)
    if not isinstance(decorated, dict):
        return None  # Heartbeat / ClientConnection / other envelope

    topic = decorated.get("topic")
    if not isinstance(topic, str) or _MESSAGES_TOPIC not in topic:
        return None  # typing / seen-receipt / badge / conversation / etc.

    decoration = (
        (decorated.get("payload") or {}).get("data") or {}
    ).get("doDecorateMessageMessengerRealtimeDecoration") or {}
    result = decoration.get("result")
    if not isinstance(result, dict):
        return None

    text = _attributed_text(result.get("body"))
    entity_urn = result.get("backendUrn")
    if not isinstance(entity_urn, str) or not entity_urn or not text:
        return None

    conversation = result.get("conversation")
    conversation_urn = ""
    if isinstance(conversation, dict):
        conversation_urn = conversation.get("entityUrn") or ""
    if not conversation_urn:
        conversation_urn = result.get("backendConversationUrn") or ""

    actor = result.get("actor")
    if not isinstance(actor, dict):
        actor = {}
    sender_member_urn = actor.get("hostIdentityUrn") or ""
    member = (actor.get("participantType") or {}).get("member") or {}
    sender_name = (
        f"{_attributed_text(member.get('firstName'))} "
        f"{_attributed_text(member.get('lastName'))}"
    ).strip()

    return ParsedRealtimeMessage(
        entity_urn=entity_urn,
        conversation_urn=conversation_urn,
        sender_name=sender_name,
        sender_member_urn=sender_member_urn,
        text=text,
        timestamp=_epoch_ms_to_str(result.get("deliveredAt")),
    )
