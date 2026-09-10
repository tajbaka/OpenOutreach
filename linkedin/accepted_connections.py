"""Read-only, sender-specific projection of connections awaiting a reply."""
from __future__ import annotations

from dataclasses import dataclass
from collections import defaultdict
from datetime import datetime, time, timezone as dt_timezone
from zoneinfo import ZoneInfo

from django.db.models import Q

from crm.models import Deal, Message
from linkedin.crm_v2_evidence import _is_automated
from linkedin.enums import ProfileState
from linkedin.operators import resolve_sales_owner_handle


REPORT_SENDERS = {"Arian": "Arian", "Chuka": "Eddy"}
REPORT_TIMEZONE = ZoneInfo("America/Toronto")


@dataclass(frozen=True)
class AcceptedConnection:
    lead_id: int
    operator: str
    recorded_at: datetime | None
    name: str
    company: str
    linkedin_url: str

    def sheet_values(self) -> list:
        recorded = ""
        if self.recorded_at is not None:
            utc = self.recorded_at.astimezone(dt_timezone.utc)
            # Legacy connection imports encoded date-only values at UTC
            # midnight. Do not invent an evening acceptance on the prior day.
            if utc.time() == time.min:
                local = datetime.combine(utc.date(), time.min)
            else:
                local = self.recorded_at.astimezone(REPORT_TIMEZONE).replace(tzinfo=None)
            recorded = (local - datetime(1899, 12, 30)).total_seconds() / 86400
        return [recorded, REPORT_SENDERS[self.operator], self.name, self.company, self.linkedin_url]

    @property
    def date_format(self) -> str:
        if self.recorded_at and self.recorded_at.astimezone(dt_timezone.utc).time() == time.min:
            return "mmm d, yyyy"
        return "mmm d, yyyy h:mm AM/PM"


def build_accepted_connections() -> tuple[list[AcceptedConnection], dict[str, int]]:
    """No writes, no live LinkedIn calls, no campaign eligibility changes.

    Campaigns may be inactive or finished. A later automation state must not
    erase a recorded connection. Unknown timestamps stay blank, never inferred
    from update_date. Earliest observation deduplicates repeat campaign Deals.
    """
    candidates = Deal.objects.filter(
        Q(connected_at__isnull=False) | Q(state=ProfileState.CONNECTED),
    ).select_related("lead", "campaign__user", "message_enrollment").only(
        "lead_id", "connected_at", "invitation_sender", "campaign__user__username",
        "lead__first_name", "lead__last_name", "lead__company_name", "lead__linkedin_url",
        "message_enrollment__operator",
    ).order_by("pk")
    counts = {"source_deals": 0, "held_sender_identity": 0, "duplicate_deals": 0,
              "replied_pairs": 0, "unattributed_replies": 0, "unknown_dates": 0,
              "thread_attributed_replies": 0}
    by_pair: dict[tuple[int, str], AcceptedConnection] = {}
    # Do not load Lead.raw, embeddings, campaign model blobs, or version payloads
    # for a five-column report, especially across a remote DB connection.
    for deal in candidates.iterator():
        counts["source_deals"] += 1
        operator = resolve_sales_owner_handle(deal.campaign.user.username)
        enrollment = getattr(deal, "message_enrollment", None)
        identities = [value for value in (
            deal.invitation_sender,
            enrollment.operator if enrollment else "",
        ) if value]
        if not operator or any(resolve_sales_owner_handle(v) != operator for v in identities):
            counts["held_sender_identity"] += 1
            continue
        if operator not in REPORT_SENDERS:
            continue
        key = (deal.lead_id, operator)
        row = AcceptedConnection(
            lead_id=deal.lead_id, operator=operator, recorded_at=deal.connected_at,
            name=f"{deal.lead.first_name or ''} {deal.lead.last_name or ''}".strip(),
            company=deal.lead.company_name or "", linkedin_url=deal.lead.linkedin_url or "",
        )
        old = by_pair.get(key)
        if old is not None:
            counts["duplicate_deals"] += 1
        if old is None or (row.recorded_at is not None and (
            old.recorded_at is None or row.recorded_at < old.recorded_at
        )):
            by_pair[key] = row

    # last_reply_at has lead-wide stamping paths. Current ingestion commonly
    # stores operator only on outbound Messages, so a reply may also inherit
    # one unambiguous owner from its exact (Lead, source, thread) conversation.
    # Never borrow an owner from a different thread or the Lead's other Deals.
    inbound = []
    thread_owners = defaultdict(set)
    for message in Message.objects.filter(
        lead_id__in={key[0] for key in by_pair},
        source__in=[Message.Source.LINKEDIN, Message.Source.GMAIL],
    ).select_related("operator").only(
        "lead_id", "operator__handle", "sender", "body", "raw",
        "source", "direction", "thread_external_id",
    ).order_by().iterator():
        if _is_automated(message):
            continue
        operator = resolve_sales_owner_handle(message.operator.handle if message.operator else "")
        key = (message.lead_id, message.source, message.thread_external_id)
        if message.direction == Message.Direction.OUTBOUND and message.thread_external_id:
            sender = resolve_sales_owner_handle(message.sender)
            # An explicit unknown owner or conflicting known sender makes this
            # thread ambiguous. A missing owner can use an exact sender alias.
            owners = {operator} if message.operator_id else {sender}
            if sender:
                owners.add(sender)
            thread_owners[key].update(owners)
        elif message.direction == Message.Direction.INBOUND:
            inbound.append((message.lead_id, key, operator, message.operator_id is not None))

    replied = set()
    for lead_id, key, operator, has_explicit_owner in inbound:
        owners = thread_owners.get(key, set()) if key[2] else set()
        if not has_explicit_owner and len(owners) == 1 and "" not in owners:
            operator = next(iter(owners))
            counts["thread_attributed_replies"] += 1
        if not operator:
            counts["unattributed_replies"] += 1
            continue
        replied.add((lead_id, operator))
    counts["replied_pairs"] = len(set(by_pair) & replied)
    rows = [row for key, row in by_pair.items() if key not in replied]
    rows.sort(key=lambda row: (
        -(row.recorded_at.timestamp() if row.recorded_at else float("-inf")),
        row.operator, row.lead_id,
    ))
    counts["unknown_dates"] = sum(row.recorded_at is None for row in rows)
    return rows, counts
