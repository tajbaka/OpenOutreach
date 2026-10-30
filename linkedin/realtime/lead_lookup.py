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

One exception: if the conversation match points at one of our operator/self
profiles, prefer a different sender-URN lead. That repairs threads that were
historically persisted under the logged-in account's own profile.

No match → None; the handler logs + skips.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _is_operator_profile(lead) -> bool:
    from linkedin.operators import resolve_operator

    candidates = (
        f"{lead.first_name or ''} {lead.last_name or ''}".strip(),
        lead.public_identifier or "",
    )
    for value in candidates:
        if value and resolve_operator(value) != value:
            return True
    return False


def _lead_for_sender_member_urn(sender_member_urn: str):
    from crm.models import Lead

    if not sender_member_urn:
        return None
    return Lead.objects.filter(description__contains=sender_member_urn).first()


def resolve_lead_for_realtime(*, conversation_urn: str, sender_member_urn: str, operator: str = ""):
    """Return the matching Lead, or None."""
    from crm.models import Message
    from django.db.models import Q
    from linkedin.models import RB2BVisitor
    from linkedin.operators import resolve_operator

    # Exact identity and sender scope; never name matching for new visitor threads.
    q = Q(member_urn=sender_member_urn) if sender_member_urn else Q(pk__in=[])
    if conversation_urn:
        q |= Q(lead__messages__thread_external_id=conversation_urn, lead__messages__source="linkedin")
    rows = list(RB2BVisitor.objects.filter(q, lead__isnull=False).select_related("lead", "linkedin_profile").distinct())
    if rows:
        if len(rows) != 1 or resolve_operator(operator) != resolve_operator(rows[0].linkedin_profile.linkedin_username):
            return None
        row = rows[0]
        if conversation_urn and Message.objects.filter(source="linkedin", thread_external_id=conversation_urn).exclude(lead=row.lead).exists():
            return None
        return row.lead

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
            sender_lead = _lead_for_sender_member_urn(sender_member_urn)
            if (
                sender_lead is not None
                and sender_lead.id != msg.lead_id
                and _is_operator_profile(msg.lead)
            ):
                logger.warning(
                    "Realtime conversation %s was mapped to operator profile lead %s; "
                    "using sender lead %s instead",
                    conversation_urn,
                    msg.lead_id,
                    sender_lead.id,
                )
                return sender_lead
            return msg.lead

    lead = _lead_for_sender_member_urn(sender_member_urn)
    if lead is not None:
        return lead

    return None
