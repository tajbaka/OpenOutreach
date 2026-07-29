"""Idempotent persistence of conversation threads into crm.Message + lookups."""
from __future__ import annotations

import logging
import re
from datetime import datetime

from django.db import transaction
from django.utils import timezone

from crm.models import Message

logger = logging.getLogger(__name__)

_HONORIFIC_PREFIXES = {"dr", "mr", "mrs", "ms", "miss", "prof", "professor"}


def _normalized_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def _without_honorific_prefix(value: str) -> str:
    parts = _normalized_name(value).split()
    while parts and parts[0] in _HONORIFIC_PREFIXES:
        parts.pop(0)
    return " ".join(parts)


def _lead_given_name(lead) -> str:
    """Return the lead's normalized greeting name.

    Imported ``first_name`` values can include middle initials or additional
    given names (for example, ``"Douglas M."``). Outbound copy addresses those
    leads by the first token, so echo detection must use that same token.
    """
    parts = _without_honorific_prefix(lead.first_name or "").split()
    return parts[0] if parts else ""


def _looks_like_lead_sender(sender: str, lead) -> bool:
    sender_name = _without_honorific_prefix(sender)
    if not sender_name:
        return False

    lead_full = _normalized_name(f"{lead.first_name or ''} {lead.last_name or ''}")
    lead_first = _normalized_name(lead.first_name or "")
    return bool(
        (lead_full and sender_name == lead_full)
        or (lead_first and sender_name.startswith(lead_first + " "))
    )


def _looks_like_connect_note_echo(text: str, lead) -> bool:
    """Detect LinkedIn realtime echoes of our own invite note.

    LinkedIn can stream the original connection note as if the lead sent it.
    `Deal.sent_note` is the primary guard, but older rows may not have it
    populated, so keep this fallback intentionally narrow.
    """
    normalized = _normalized_name(text)
    first = _lead_given_name(lead)
    if not normalized or not first:
        return False
    if not (
        normalized.startswith(f"hi {first} ")
        or normalized.startswith(f"hey {first} ")
        or normalized.startswith(f"{first} ")
    ):
        return False
    has_outreach_context = any(
        token in normalized
        for token in (
            "fedramp",
            "boundera",
            "fedrampgpt",
            "public sector",
            "public space",
            "20x",
        )
    )
    has_connect_ask = any(
        phrase in normalized
        for phrase in (
            "would love to connect",
            "good to connect",
            "worth connecting",
            "worth connect",
            "happy to connect",
        )
    )
    return has_outreach_context and has_connect_ask


def _looks_like_self_addressed_outbound_echo(text: str, lead) -> bool:
    """Detect outbound LinkedIn echoes misattributed to the lead.

    Realtime can emit an operator's manual send with the lead as actor. If a
    message is attributed to Ryan and opens with "Hey Ryan," it is almost
    certainly our outbound copy mirrored under the wrong participant, not a
    real inbound reply from Ryan.
    """
    first = _lead_given_name(lead)
    if not first:
        return False
    normalized = _normalized_name(text)
    return any(
        normalized.startswith(f"{greeting} {first} ")
        or normalized == f"{greeting} {first}"
        for greeting in ("hi", "hey", "hello")
    )


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
    return {resolve_operator(s) for s in senders if s and not _looks_like_lead_sender(s, lead)}


def persist_thread(
    *,
    lead,
    parsed: list[dict],
    thread_external_id: str = "",
    source: str = Message.Source.LINKEDIN,
    outbound_senders: set[str] | None = None,
) -> int:
    """Upsert each parsed message into crm.Message. Returns count newly created.

    Idempotent on (source, entity_urn): re-running with the same payload is
    a no-op. Direction is derived by comparing sender to the lead's name —
    1:1 LinkedIn DMs only have two participants, so anything not from the
    lead is from us. This avoids needing to know our own display name (which
    LinkedInProfile doesn't store reliably).
    """
    from linkedin.operators import resolve_operator

    outbound_handles = {
        resolve_operator(sender)
        for sender in (outbound_senders or set())
        if sender and resolve_operator(sender)
    }
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
            # appended like "Bryan Guy, J.D.", and honorifics like
            # "Dr. Jacquelyn Bell").
            is_inbound = _looks_like_lead_sender(sender, lead)
            if outbound_handles and resolve_operator(sender) in outbound_handles:
                is_inbound = False
            text = (m.get("text") or "").strip()
            # LinkedIn conversation payloads sometimes echo our own connection
            # note back with the lead's name as sender. When the body exactly
            # matches a Deal.sent_note we know it was our outbound invite note,
            # not a real reply from the lead.
            if text and (
                text in known_connect_notes
                or _looks_like_connect_note_echo(text, lead)
                or (
                    is_inbound
                    and _looks_like_self_addressed_outbound_echo(text, lead)
                )
            ):
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
