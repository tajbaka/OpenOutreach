"""Idempotent persistence of conversation threads into crm.Message + lookups."""
from __future__ import annotations

import logging
from datetime import datetime

from django.db import transaction
from django.utils import timezone

from crm.models import Message

logger = logging.getLogger(__name__)


def lead_outbound_operators(lead) -> set[str]:
    """Return canonical operator handles found in the lead's LinkedIn DM outbound senders.

    Used by the daemon's `follow_up` Task handler (and the
    `enqueue_no_reply_followups` backfill) to scope which leads a given
    daemon process is allowed to message. The originating account is
    whichever LinkedIn user sent the first outbound on the thread —
    that's the only account with an active DM channel to the lead, and
    is also the account whose connection request the lead accepted.

    Returns the set of canonical operator handles
    (`linkedin.operators.resolve_operator`) so the caller can do a
    plain set-membership check (`our_operator in owners`) regardless of
    whether the sender field stores `"chukwuka agu"` or
    `"Chuka Eddy Jack"`.

    Empty set means we have zero outbound LinkedIn Messages for this
    lead — typically a freshly-swept-into-CONNECTED lead that hasn't
    been messaged yet. Caller should treat that as "no constraint":
    whoever runs the daemon may proceed.
    """
    from linkedin.operators import resolve_operator
    lead_full = f"{lead.first_name or ''} {lead.last_name or ''}".strip().lower()
    lead_first = (lead.first_name or "").strip().lower()

    def _looks_like_lead_sender(sender: str) -> bool:
        sender = (sender or "").strip().lower()
        if not sender:
            return False
        return bool(
            (lead_full and sender == lead_full)
            or (lead_first and sender.startswith(lead_first + " "))
        )

    senders = (
        Message.objects.filter(
            lead=lead,
            source=Message.Source.LINKEDIN,
            direction=Message.Direction.OUTBOUND,
        )
        .exclude(sender="")
        .values_list("sender", flat=True)
        .distinct()
    )
    return {resolve_operator(s) for s in senders if s and not _looks_like_lead_sender(s)}


def persist_thread(
    *,
    lead,
    parsed: list[dict],
    thread_external_id: str = "",
    source: str = Message.Source.LINKEDIN,
) -> int:
    """Upsert each parsed message into crm.Message. Returns count newly created.

    Idempotent on (source, entity_urn): re-running with the same payload is
    a no-op. Direction is derived by comparing sender to the lead's name —
    1:1 LinkedIn DMs only have two participants, so anything not from the
    lead is from us. This avoids needing to know our own display name (which
    LinkedInProfile doesn't store reliably).
    """
    lead_full = f"{lead.first_name or ''} {lead.last_name or ''}".strip().lower()
    lead_first = (lead.first_name or "").strip().lower()
    known_connect_notes = {
        note.strip()
        for note in lead.deal_set.exclude(sent_note="").values_list("sent_note", flat=True)
        if note and note.strip()
    }

    created = 0
    with transaction.atomic():
        for m in parsed:
            entity_urn = (m.get("entity_urn") or "").strip()
            if not entity_urn:
                continue

            sender = (m.get("sender") or "").strip().lower()
            # Inbound iff sender matches the lead — try full name first, then
            # first-name+space prefix (handles leads imported from CSVs that
            # only carry first_name, plus Voyager senders with credentials
            # appended like "Bryan Guy, J.D.").
            is_inbound = bool(sender) and (
                (lead_full and sender == lead_full)
                or (lead_first and sender.startswith(lead_first + " "))
            )
            text = (m.get("text") or "").strip()
            # LinkedIn conversation payloads sometimes echo our own connection
            # note back with the lead's name as sender. When the body exactly
            # matches a Deal.sent_note we know it was our outbound invite note,
            # not a real reply from the lead.
            if text and text in known_connect_notes:
                is_inbound = False
            direction = (
                Message.Direction.INBOUND if is_inbound
                else Message.Direction.OUTBOUND
            )

            sent_at = _parse_timestamp(m.get("timestamp") or "")

            _, was_created = Message.objects.get_or_create(
                source=source,
                external_id=entity_urn,
                defaults={
                    "lead": lead,
                    "direction": direction,
                    "sender": sender,
                    "body": m.get("text") or "",
                    "sent_at": sent_at,
                    "thread_external_id": thread_external_id,
                    "raw": m,
                },
            )
            if was_created:
                created += 1
    return created


def _parse_timestamp(ts: str):
    """Parse 'YYYY-MM-DD HH:MM' into aware datetime; fall back to now() if empty/bad."""
    if not ts:
        return timezone.now()
    try:
        naive = datetime.strptime(ts, "%Y-%m-%d %H:%M")
        return timezone.make_aware(naive, timezone.get_current_timezone())
    except ValueError:
        logger.debug("persist_thread: malformed timestamp %r — falling back to now()", ts)
        return timezone.now()
