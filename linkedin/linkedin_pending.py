"""Read-only projection of LinkedIn conversations waiting on a human reply."""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

from crm.models import Message
from linkedin.crm_v2_evidence import (
    _is_acknowledgement,
    _is_automated,
    _is_polite_decline,
    _normalized_body,
)
from linkedin.operators import resolve_sales_owner_handle


LOOKBACK_DAYS = 14
REPORT_TIMEZONE = ZoneInfo("America/Toronto")
_SPACE_RE = re.compile(r"\s+")
_WORD_RE = re.compile(r"[\w'-]+", re.UNICODE)
_ACTION_INTENT_RE = re.compile(
    r"(?:\?|\b(?:"
    r"are you|can you|could you|did you|do you|how (?:can|do|would)|"
    r"is there|let me know|let's|next step|please|send|share|review|"
    r"confirm|schedule|book|meet|call|introduce|follow up|"
    r"tell me|interested|available|availability|proposal|pricing|demo|"
    r"what|when|where|who|why|would you"
    r")\b)",
    re.IGNORECASE,
)
_ACK_ONLY_PHRASES = (
    "appreciate it",
    "bye",
    "cheers",
    "good bye",
    "goodbye",
    "got it",
    "great",
    "happy to connect",
    "have a good day",
    "have a great day",
    "have a nice day",
    "ok",
    "okay",
    "perfect",
    "sounds good",
    "talk soon",
    "thank you",
    "thanks",
    "understood",
    "will do",
)


@dataclass(frozen=True)
class LinkedInPending:
    pending_id: str
    lead_id: int
    operator: str
    thread_external_id: str
    message_external_id: str
    received_at: datetime
    name: str
    company: str
    linkedin_url: str
    message: str

    def sheet_values(self, *, now: datetime) -> list:
        local = self.received_at.astimezone(REPORT_TIMEZONE).replace(tzinfo=None)
        serial = (local - datetime(1899, 12, 30)).total_seconds() / 86400
        days_waiting = max(0, (now - self.received_at).days)
        return [
            serial,
            self.operator,
            self.name,
            self.company,
            self.linkedin_url,
            self.message,
            days_waiting,
            self.pending_id,
            self.lead_id,
            self.thread_external_id,
            self.message_external_id,
        ]


def needs_linkedin_reply(body: str) -> bool:
    """Return true only for deterministic question/request/interest signals."""
    normalized = _normalized_body(body)
    if not normalized or _is_acknowledgement(normalized) or _is_polite_decline(normalized):
        return False
    words = _WORD_RE.findall(normalized)
    collapsed = normalized.strip(" .,!?:;-—")
    if len(words) <= 12 and not _ACTION_INTENT_RE.search(normalized):
        if any(
            collapsed == phrase
            or collapsed.startswith(f"{phrase} ")
            or collapsed.endswith(f" {phrase}")
            for phrase in _ACK_ONLY_PHRASES
        ):
            return False
    return bool(_ACTION_INTENT_RE.search(normalized))


def build_linkedin_pending(
    *,
    now: datetime,
    lookback_days: int = LOOKBACK_DAYS,
) -> tuple[list[LinkedInPending], dict[str, int]]:
    """Build pending rows without writing DB state or calling LinkedIn.

    Only exact nonblank thread IDs are eligible. Ownership is the one canonical
    operator observed anywhere in that exact Lead/thread conversation; missing
    or conflicting owners fail closed. Within an eligible thread, actionable
    inbound replaces the prior pending item, a human outbound clears it, and a
    later polite decline clears it. Acknowledgements never create or clear work.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if lookback_days <= 0:
        raise ValueError("lookback_days must be positive")
    cutoff = now - timedelta(days=lookback_days)
    counts = {
        "recent_inbound": 0,
        "actionable_inbound": 0,
        "acknowledgements_filtered": 0,
        "declines_filtered": 0,
        "non_actionable_filtered": 0,
        "blank_thread_held": 0,
        "unattributed_threads": 0,
        "ambiguous_threads": 0,
        "replied_threads": 0,
    }

    recent_inbound = list(
        Message.objects.filter(
            source=Message.Source.LINKEDIN,
            direction=Message.Direction.INBOUND,
            sent_at__gte=cutoff,
            sent_at__lte=now,
        )
        .select_related("lead", "operator")
        .only(
            "id",
            "lead_id",
            "operator_id",
            "operator__handle",
            "source",
            "external_id",
            "direction",
            "sender",
            "body",
            "sent_at",
            "thread_external_id",
            "raw",
            "lead__first_name",
            "lead__last_name",
            "lead__company_name",
            "lead__linkedin_url",
        )
        .order_by("sent_at", "id")
    )
    candidates: dict[tuple[int, str], list[Message]] = defaultdict(list)
    for message in recent_inbound:
        if _is_automated(message):
            continue
        counts["recent_inbound"] += 1
        thread_id = (message.thread_external_id or "").strip()
        if not thread_id:
            counts["blank_thread_held"] += 1
            continue
        normalized = _normalized_body(message.body)
        if _is_polite_decline(normalized):
            counts["declines_filtered"] += 1
        elif _is_acknowledgement(normalized) or _is_ack_only(normalized):
            counts["acknowledgements_filtered"] += 1
        elif needs_linkedin_reply(normalized):
            counts["actionable_inbound"] += 1
        else:
            counts["non_actionable_filtered"] += 1
        candidates[(message.lead_id, thread_id)].append(message)

    if not candidates:
        return [], counts

    lead_ids = {key[0] for key in candidates}
    thread_ids = {key[1] for key in candidates}
    conversations: dict[tuple[int, str], list[Message]] = defaultdict(list)
    messages = (
        Message.objects.filter(
            source=Message.Source.LINKEDIN,
            lead_id__in=lead_ids,
            thread_external_id__in=thread_ids,
            sent_at__lte=now,
        )
        .select_related("lead", "operator")
        .only(
            "id",
            "lead_id",
            "operator_id",
            "operator__handle",
            "source",
            "external_id",
            "direction",
            "sender",
            "body",
            "sent_at",
            "thread_external_id",
            "raw",
            "lead__first_name",
            "lead__last_name",
            "lead__company_name",
            "lead__linkedin_url",
        )
        .order_by("sent_at", "id")
    )
    for message in messages.iterator():
        key = (message.lead_id, (message.thread_external_id or "").strip())
        if key in candidates and not _is_automated(message):
            conversations[key].append(message)

    rows: list[LinkedInPending] = []
    for (lead_id, thread_id), thread in conversations.items():
        owners: set[str] = set()
        for message in thread:
            explicit = resolve_sales_owner_handle(
                message.operator.handle if message.operator_id else ""
            )
            sender = resolve_sales_owner_handle(message.sender)
            if explicit:
                owners.add(explicit)
            if message.direction == Message.Direction.OUTBOUND and sender:
                owners.add(sender)
        if not owners:
            counts["unattributed_threads"] += 1
            continue
        if len(owners) != 1:
            counts["ambiguous_threads"] += 1
            continue
        operator = next(iter(owners))

        pending: Message | None = None
        had_actionable = False
        for message in thread:
            if message.direction == Message.Direction.OUTBOUND:
                if pending is not None:
                    counts["replied_threads"] += 1
                pending = None
                continue
            normalized = _normalized_body(message.body)
            if _is_polite_decline(normalized):
                pending = None
            elif needs_linkedin_reply(normalized):
                had_actionable = True
                pending = message
            # Acknowledgements and non-actionable statements neither create
            # work nor erase a still-unanswered explicit request.

        if pending is None or not had_actionable or pending.sent_at < cutoff:
            continue
        lead = pending.lead
        excerpt = _message_excerpt(pending.body)
        pending_id = str(uuid5(
            NAMESPACE_URL,
            f"openoutreach:linkedin-pending:{lead_id}:{operator}:{thread_id}",
        ))
        rows.append(LinkedInPending(
            pending_id=pending_id,
            lead_id=lead_id,
            operator=operator,
            thread_external_id=thread_id,
            message_external_id=pending.external_id,
            received_at=pending.sent_at,
            name=f"{lead.first_name or ''} {lead.last_name or ''}".strip(),
            company=lead.company_name or "",
            linkedin_url=lead.linkedin_url or "",
            message=excerpt,
        ))

    # Oldest unanswered work is the most urgent.  This also keeps the three
    # age-based Sheet cohorts (7+ days, 3-6 days, 0-2 days) contiguous.
    rows.sort(key=lambda row: (row.received_at.timestamp(), row.operator, row.lead_id))
    return rows, counts


def _is_ack_only(normalized: str) -> bool:
    collapsed = normalized.strip(" .,!?:;-—")
    words = _WORD_RE.findall(collapsed)
    if len(words) > 12 or _ACTION_INTENT_RE.search(collapsed):
        return False
    return any(
        collapsed == phrase
        or collapsed.startswith(f"{phrase} ")
        or collapsed.endswith(f" {phrase}")
        for phrase in _ACK_ONLY_PHRASES
    )


def _message_excerpt(body: str, *, limit: int = 500) -> str:
    """Keep both the setup and the usually-actionable end of long messages."""
    compact = _SPACE_RE.sub(" ", (body or "").strip())
    if len(compact) <= limit:
        return compact
    prefix = min(350, limit // 2)
    suffix = limit - prefix - 3
    return f"{compact[:prefix].rstrip()} … {compact[-suffix:].lstrip()}"
