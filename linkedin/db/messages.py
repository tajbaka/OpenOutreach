"""Idempotent persistence of conversation threads into crm.Message."""
from __future__ import annotations

import logging
from datetime import datetime

from django.db import transaction
from django.utils import timezone

from crm.models import Message

logger = logging.getLogger(__name__)


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
