"""Resolve a crm.Lead from a realtime event's conversation / sender URNs.

Strategy, in priority order:

1. Conversation URN → the most reliable match. Every thread persisted by
   `linkedin.db.messages.persist_thread` stamps `Message.thread_external_id`
   with the conversation URN. If we have ever seen this thread (via the
   sweep, follow-up, backfill, or an earlier realtime event), one query
   finds the Lead.
2. Sender member/profile URN → fallback for a thread we have never
   persisted. The enriched profile JSON on `Lead.description` carries the
   member URN; a substring match on that text column finds it (works on
   both SQLite dev and Postgres prod).

No match → None; the handler logs + skips.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def resolve_lead_for_realtime(*, conversation_urn: str, sender_member_urn: str):
    """Return the matching Lead, or None."""
    from crm.models import Lead, Message

    if conversation_urn:
        msg = (
            Message.objects.filter(
                source=Message.Source.LINKEDIN,
                thread_external_id=conversation_urn,
            )
            .select_related("lead")
            .first()
        )
        if msg is not None:
            return msg.lead

    if sender_member_urn:
        lead = Lead.objects.filter(description__contains=sender_member_urn).first()
        if lead is not None:
            return lead

    return None
