"""Codex review/apply helpers for manual followup sheet generation."""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Iterable

from django.utils import timezone

from crm.models import Lead, Message
from linkedin.exceptions import SheetsError
from linkedin.models import WorkflowRun
from linkedin.notifications.calendar_events import latest_meeting_for
from linkedin.notifications.sheets import (
    COL_LINKEDIN_URL,
    COL_OUTREACH_STATUS,
    FU_COL_CONVO,
    FU_COL_DAYS_SINCE,
    FU_COL_DAYS_SINCE_CONNECTION,
    FU_COL_DRAFT_EMAIL,
    FU_COL_DRAFT_LINKEDIN,
    FU_COL_EMAIL_LINK,
    FU_COL_LINKEDIN_MSG_URL,
    FU_COL_NAME,
    FU_COL_PRIORITY,
    FU_COL_QUALIFY,
    FU_COL_ROLE,
    FU_COL_SENT_EMAIL,
    FU_COL_SENT_LINKEDIN,
    FU_COL_STATE,
    FU_COL_STATUS,
    FU_PRIORITIES,
    FU_ROLE_TO_ICP,
    MET_STATUSES,
    PRE_MEETING_STATUSES,
    STATE_BALL_ON_THEM,
    STATE_BALL_ON_US,
    STATE_COLD_THREAD,
    deal_to_outreach_status,
    email_search_hyperlink,
    linkedin_message_hyperlink,
    read_followup_sent_rows,
    read_icp_goals,
    SheetIndex,
    write_followups,
)
from linkedin.operators import resolve_operator


DEFAULT_OPERATORS = ("Arian", "Chuka", "Athena", "Leili")
NUDGE_AFTER_DAYS = 5
ACTIVE_THREAD_DAYS = 7
WARM_THREAD_DAYS = 21
STALE_THREAD_DAYS = 60
COLD_THREAD_DAYS = 90

NO_PHRASES = (
    "not interested",
    "not a fit",
    "not the best audience",
    "best of luck",
    "wishing you the best",
    "i'm good",
    "i'm not able",
    "no opportunity",
    "not the right time",
    "timing is not right",
    "do not play",
    "unable to participate",
    "appreciate staying in touch",
    "no longer with",
    "may be a coi",
    "seeking a long-term role",
    "our client is hiring",
)


@dataclass(frozen=True)
class FollowupDraftDecision:
    lead_id: int
    operator: str
    status: str
    state: str
    role: str
    priority: str
    convo: str
    draft_email: str
    draft_linkedin: str
    raw: dict


def codex_followup_instructions() -> str:
    return (
        "Draft manual followup rows for Boundera. Use the merged LinkedIn + "
        "Gmail timeline, meeting notes, role, status, freshness posture, and "
        "ICP goal. Write concise human messages. Do not draft active-in-flight "
        "rows. Do not apologize for gaps. For post-meeting rows, use the "
        "meeting notes or leave the draft blank if context is insufficient. "
        "Return JSON only in the declared schema; do not send messages."
    )


def serialize_followup_queue(
    *,
    operators: Iterable[str] | None = DEFAULT_OPERATORS,
    campaign_id: int | None = None,
    limit: int | None = None,
    include_active: bool = True,
    read_sheet: bool = True,
) -> dict:
    rows, warnings = followup_candidates(
        operators=operators,
        campaign_id=campaign_id,
        limit=limit,
        include_active=include_active,
        read_sheet=read_sheet,
    )
    return {
        "instructions": codex_followup_instructions(),
        "schema": {
            "rows": [{
                "lead_id": "integer from candidates[].lead.id",
                "operator": list(DEFAULT_OPERATORS),
                "status": "use candidate.sheet_row.Status unless correcting a clear mismatch",
                "state": [STATE_BALL_ON_US, STATE_COLD_THREAD, STATE_BALL_ON_THEM],
                "role": "ROLE value for the sheet, e.g. CSP, 3PAO, Advisor, Assessor, Channel",
                "priority": FU_PRIORITIES,
                "convo": "one or two sentence relationship summary",
                "draft_email": "email draft or blank",
                "draft_linkedin": "LinkedIn DM draft or blank",
            }],
        },
        "warnings": warnings,
        "candidates": rows,
    }


def followup_candidates(
    *,
    operators: Iterable[str] | None = DEFAULT_OPERATORS,
    campaign_id: int | None = None,
    limit: int | None = None,
    include_active: bool = True,
    read_sheet: bool = True,
) -> tuple[list[dict], list[str]]:
    now = timezone.now()
    operator_values = tuple(operators or DEFAULT_OPERATORS)
    operator_set = {resolve_operator(op) for op in operator_values if resolve_operator(op)}
    warnings: list[str] = []
    sent_names = _sent_names(operator_set, warnings=warnings) if read_sheet else set()
    status_by_url = _status_by_url(warnings=warnings) if read_sheet else {}
    icp_goals = _icp_goals(warnings=warnings) if read_sheet else {}

    qs = (
        Lead.objects.filter(disqualified=False)
        .filter(messages__isnull=False)
        .distinct()
        .order_by("id")
    )
    if campaign_id is not None:
        qs = qs.filter(deal__campaign_id=campaign_id)

    out: list[dict] = []
    for lead in qs.iterator(chunk_size=200):
        if limit is not None and len(out) >= limit:
            break
        if _norm_name(f"{lead.first_name} {lead.last_name}") in sent_names:
            continue
        row = _candidate_for_lead(
            lead,
            now=now,
            status_by_url=status_by_url,
            icp_goals=icp_goals,
        )
        if row is None:
            continue
        if row["classification"] == "active_in_flight" and not include_active:
            continue
        if operator_set and row["operator"] not in operator_set:
            continue
        out.append(row)
    return out, warnings


def load_followup_decisions(path: str | Path) -> list[FollowupDraftDecision]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = payload if isinstance(payload, list) else payload.get("rows", [])
    if not isinstance(rows, list):
        raise ValueError("Followup draft JSON must be a list or an object with a rows list.")
    return [followup_decision_from_mapping(row) for row in rows]


def followup_decision_from_mapping(row: dict) -> FollowupDraftDecision:
    if "lead_id" not in row:
        raise ValueError("Every followup row must include lead_id.")
    operator = resolve_operator(str(row.get("operator") or ""))
    if not operator:
        raise ValueError(f"Followup row for lead_id={row.get('lead_id')} is missing operator.")
    state = str(row.get("state") or "").strip()
    if state not in {STATE_BALL_ON_US, STATE_COLD_THREAD, STATE_BALL_ON_THEM}:
        raise ValueError(f"Invalid state for lead_id={row.get('lead_id')}: {state!r}")
    priority = str(row.get("priority") or "").strip() or "LOW"
    if priority not in FU_PRIORITIES:
        raise ValueError(f"Invalid priority for lead_id={row.get('lead_id')}: {priority!r}")
    return FollowupDraftDecision(
        lead_id=int(row["lead_id"]),
        operator=operator,
        status=str(row.get("status") or "").strip(),
        state=state,
        role=str(row.get("role") or "").strip(),
        priority=priority,
        convo=str(row.get("convo") or "").strip(),
        draft_email=str(row.get("draft_email") or row.get("Draft Email") or "").strip(),
        draft_linkedin=str(
            row.get("draft_linkedin") or row.get("Draft LinkedIn") or ""
        ).strip(),
        raw=dict(row),
    )


def apply_followup_decisions(
    decisions: list[FollowupDraftDecision],
    *,
    record_workflow: bool = True,
) -> dict[str, int]:
    rows_by_operator: dict[str, list[dict]] = {}
    now = timezone.now()
    for decision in decisions:
        lead = Lead.objects.get(pk=decision.lead_id)
        sheet_row = _sheet_row_for_decision(lead, decision, now=now)
        rows_by_operator.setdefault(decision.operator, []).append(sheet_row)

    counts = write_followups(rows_by_operator)
    if record_workflow:
        WorkflowRun.objects.create(
            name="followup",
            operator="",
            summary=(
                f"operators={len(rows_by_operator)} "
                f"rows={sum(counts.values())}"
            ),
            counts={
                "operators": len(rows_by_operator),
                "rows": sum(counts.values()),
                "by_operator": counts,
            },
        )
    return counts


def _candidate_for_lead(
    lead: Lead,
    *,
    now,
    status_by_url: dict[str, str],
    icp_goals: dict[str, dict[str, str]],
) -> dict | None:
    msgs = list(lead.messages.order_by("sent_at"))
    if not msgs:
        return None
    if not any(m.direction == Message.Direction.INBOUND for m in msgs):
        return None

    deal = lead.deal_set.order_by("-creation_date").first()
    status = status_by_url.get(lead.linkedin_url or "")
    if not status and deal is not None:
        status = deal_to_outreach_status(deal)
    status = status or ""

    classification, latest = _classify(status=status, msgs=msgs, now=now)
    if classification in {"no_messages", "no_inbound"}:
        return None

    operator = _operator_for(lead, msgs)
    if not operator:
        return None

    role = _role_for_lead(lead)
    icp = FU_ROLE_TO_ICP.get(role, lead.icp or "")
    latest_meeting = latest_meeting_for(lead)
    latest_any = msgs[-1] if msgs else None
    latest_inbound = _latest_by_direction(msgs, Message.Direction.INBOUND)
    latest_outbound = _latest_by_direction(msgs, Message.Direction.OUTBOUND)
    days_since = _days_since_anchor(
        classification=classification,
        latest_any=latest_any,
        latest_meeting=latest_meeting,
        now=now,
    )
    freshness, posture, freshness_reason = _freshness_context(
        classification=classification,
        latest_any=latest_any,
        latest_meeting=latest_meeting,
        now=now,
    )
    polite_no = _contains_polite_no(msgs)
    state = _state_for_classification(classification)
    priority = _priority_for(
        classification=classification,
        days_since=days_since,
        polite_no=polite_no,
    )

    return {
        "lead": {
            "id": lead.pk,
            "name": _lead_name(lead),
            "first_name": lead.first_name,
            "last_name": lead.last_name,
            "company": lead.company_name,
            "linkedin_url": lead.linkedin_url,
            "email": lead.email,
            "current_icp": lead.icp,
        },
        "deal": {
            "id": deal.pk if deal else None,
            "state": deal.state if deal else "",
            "connected_at": deal.connected_at.isoformat() if deal and deal.connected_at else "",
            "campaign_id": deal.campaign_id if deal else None,
            "campaign": deal.campaign.name if deal else "",
        },
        "operator": operator,
        "classification": classification,
        "polite_no_candidate": polite_no,
        "freshness": {
            "conversation_freshness": freshness,
            "draft_posture": posture,
            "reason": freshness_reason,
            "days_since": days_since,
            "days_since_inbound": _days_since(latest_inbound.sent_at, now) if latest_inbound else None,
            "days_since_outbound": _days_since(latest_outbound.sent_at, now) if latest_outbound else None,
            "latest_direction": latest.direction if latest else "",
            "latest_at": latest.sent_at.isoformat() if latest else "",
        },
        "role": role,
        "icp_goal": {
            "icp": icp,
            "goal": (icp_goals.get(icp) or {}).get("goal", ""),
        },
        "meeting": {
            "latest_at": latest_meeting.start_at.isoformat() if latest_meeting else "",
            "title": latest_meeting.title if latest_meeting else "",
            "gemini_notes": (latest_meeting.gemini_notes_raw or "")[:3500] if latest_meeting else "",
        },
        "messages": [_serialize_message(m) for m in msgs[-20:]],
        "sheet_row": {
            FU_COL_NAME: _lead_name(lead),
            FU_COL_STATUS: status,
            FU_COL_STATE: state,
            FU_COL_ROLE: role,
            FU_COL_PRIORITY: priority,
            FU_COL_DAYS_SINCE: days_since if days_since is not None else "",
            FU_COL_DAYS_SINCE_CONNECTION: _days_since(deal.connected_at, now) if deal and deal.connected_at else "",
            FU_COL_CONVO: "",
            FU_COL_DRAFT_EMAIL: "",
            FU_COL_EMAIL_LINK: email_search_hyperlink(lead.email),
            FU_COL_SENT_EMAIL: "No",
            FU_COL_DRAFT_LINKEDIN: "",
            FU_COL_LINKEDIN_MSG_URL: linkedin_message_hyperlink(
                _latest_linkedin_thread(msgs),
                lead.linkedin_url,
            ),
            FU_COL_SENT_LINKEDIN: "No",
            FU_COL_QUALIFY: "Qualify",
        },
    }


def _sheet_row_for_decision(lead: Lead, decision: FollowupDraftDecision, *, now) -> dict:
    msgs = list(lead.messages.order_by("sent_at"))
    deal = lead.deal_set.order_by("-creation_date").first()
    return {
        FU_COL_NAME: _lead_name(lead),
        FU_COL_STATUS: decision.status or (deal_to_outreach_status(deal) if deal else ""),
        FU_COL_STATE: decision.state,
        FU_COL_ROLE: decision.role or _role_for_lead(lead),
        FU_COL_PRIORITY: decision.priority,
        FU_COL_DAYS_SINCE: _days_since(msgs[-1].sent_at, now) if msgs else "",
        FU_COL_DAYS_SINCE_CONNECTION: _days_since(deal.connected_at, now) if deal and deal.connected_at else "",
        FU_COL_CONVO: decision.convo,
        FU_COL_DRAFT_EMAIL: decision.draft_email,
        FU_COL_EMAIL_LINK: email_search_hyperlink(lead.email),
        FU_COL_SENT_EMAIL: "No",
        FU_COL_DRAFT_LINKEDIN: decision.draft_linkedin,
        FU_COL_LINKEDIN_MSG_URL: linkedin_message_hyperlink(
            _latest_linkedin_thread(msgs),
            lead.linkedin_url,
        ),
        FU_COL_SENT_LINKEDIN: "No",
        FU_COL_QUALIFY: "Qualify",
    }


def _sent_names(operators: set[str], *, warnings: list[str]) -> set[str]:
    names: set[str] = set()
    for operator in operators or set(DEFAULT_OPERATORS):
        try:
            rows = read_followup_sent_rows(operator)
        except SheetsError as exc:
            warnings.append(f"Could not read existing {operator} followups: {exc}")
            continue
        for row in rows:
            name = _norm_name(row.get(FU_COL_NAME, ""))
            if name:
                names.add(name)
    return names


def _status_by_url(*, warnings: list[str]) -> dict[str, str]:
    try:
        idx = SheetIndex.load()
    except SheetsError as exc:
        warnings.append(f"Could not load People tab; meeting/scheduling statuses may be incomplete: {exc}")
        return {}
    if COL_LINKEDIN_URL not in idx.actual_index_0 or COL_OUTREACH_STATUS not in idx.actual_index_0:
        return {}
    url_col = idx.actual_index_0[COL_LINKEDIN_URL]
    status_col = idx.actual_index_0[COL_OUTREACH_STATUS]
    out: dict[str, str] = {}
    for row in idx.rows[1:]:
        url = (row[url_col] if url_col < len(row) else "").strip()
        status = (row[status_col] if status_col < len(row) else "").strip()
        if url and status:
            out[url] = status
    return out


def _icp_goals(*, warnings: list[str]) -> dict[str, dict[str, str]]:
    try:
        return read_icp_goals()
    except SheetsError as exc:
        warnings.append(f"Could not read ICP Goals tab: {exc}")
        return {}


def _classify(*, status: str, msgs: list[Message], now) -> tuple[str, Message | None]:
    if not msgs:
        return "no_messages", None
    if status in MET_STATUSES:
        return "met", msgs[-1]
    if status in PRE_MEETING_STATUSES:
        return "pre_meeting", msgs[-1]
    if not any(m.direction == Message.Direction.INBOUND for m in msgs):
        return "no_inbound", msgs[-1]
    latest = msgs[-1]
    if latest.direction == Message.Direction.INBOUND:
        return "ball_on_us", latest
    cutoff = now - timedelta(days=NUDGE_AFTER_DAYS)
    if latest.sent_at < cutoff:
        return "cold_thread", latest
    return "active_in_flight", latest


def _freshness_context(*, classification: str, latest_any, latest_meeting, now):
    anchors = []
    if latest_any:
        anchors.append(latest_any.sent_at)
    if classification == "met" and latest_meeting:
        anchors.append(latest_meeting.start_at)
    anchor = max(anchors) if anchors else None
    age = _days_since(anchor, now) if anchor else None
    if age is None:
        return "unknown", "new_touch", "no dated conversation anchor"
    if classification == "active_in_flight":
        return "active", "hold", "latest outbound is still fresh"
    if age <= ACTIVE_THREAD_DAYS:
        return "active", "reply", "continue the thread directly"
    if age <= WARM_THREAD_DAYS:
        return "warm", "light_followup", "reference prior context lightly"
    if age <= STALE_THREAD_DAYS:
        return "stale", "reopen", "reopen the thread; do not write as if ongoing"
    if age <= COLD_THREAD_DAYS:
        return "cold", "memory_reopen", "treat as a new touch with light memory"
    return "archival", "skip_or_new_reason", "draft only with a fresh external reason"


def _days_since_anchor(*, classification: str, latest_any, latest_meeting, now) -> int | None:
    anchors = []
    if latest_any:
        anchors.append(latest_any.sent_at)
    if classification == "met" and latest_meeting:
        anchors.append(latest_meeting.start_at)
    return _days_since(max(anchors), now) if anchors else None


def _state_for_classification(classification: str) -> str:
    if classification == "ball_on_us":
        return STATE_BALL_ON_US
    if classification == "cold_thread":
        return STATE_COLD_THREAD
    return STATE_BALL_ON_THEM


def _priority_for(*, classification: str, days_since: int | None, polite_no: bool) -> str:
    if polite_no:
        return "HOLD"
    if classification in {"met", "pre_meeting", "ball_on_us"}:
        return "HIGH"
    if classification == "cold_thread":
        return "MEDIUM-HIGH" if (days_since or 0) <= 21 else "MEDIUM"
    return "LOW"


def _operator_for(lead: Lead, msgs: list[Message]) -> str:
    outbound = [
        resolve_operator(m.sender)
        for m in msgs
        if m.direction == Message.Direction.OUTBOUND and resolve_operator(m.sender)
    ]
    counts = Counter(outbound)
    if counts:
        return counts.most_common(1)[0][0]
    deal = lead.deal_set.select_related("campaign__user").order_by("-creation_date").first()
    if deal and deal.campaign and deal.campaign.user:
        user = deal.campaign.user
        candidates = [
            user.username,
            user.email,
            f"{user.first_name} {user.last_name}".strip(),
            user.first_name,
        ]
        for candidate in candidates:
            op = resolve_operator(candidate)
            if op:
                return op
    return ""


def _role_for_lead(lead: Lead) -> str:
    value = (lead.icp or "").strip()
    if value == "CSPs":
        return "CSP"
    if value == "3PAOs/Assessors":
        return "3PAO"
    if value == "Advisors":
        return "Advisor"
    if value == "Channel":
        return "Channel"
    if value == "CMMC Advisor/Channel":
        return "Channel"
    if value == "CMMC Buyers":
        return "CSP"
    return "Advisor"


def _latest_by_direction(msgs: list[Message], direction: str) -> Message | None:
    return next((m for m in reversed(msgs) if m.direction == direction), None)


def _latest_linkedin_thread(msgs: list[Message]) -> str:
    for msg in reversed(msgs):
        if msg.source == Message.Source.LINKEDIN and msg.thread_external_id:
            return msg.thread_external_id
    return ""


def _contains_polite_no(msgs: list[Message]) -> bool:
    inbound = " ".join(
        (m.body or "").lower()
        for m in msgs
        if m.direction == Message.Direction.INBOUND
    )
    return any(phrase in inbound for phrase in NO_PHRASES)


def _serialize_message(msg: Message) -> dict:
    return {
        "source": msg.source,
        "direction": msg.direction,
        "sent_at": msg.sent_at.isoformat(),
        "sender": msg.sender,
        "body": (msg.body or "")[:1200],
    }


def _lead_name(lead: Lead) -> str:
    return f"{lead.first_name} {lead.last_name}".strip() or lead.public_identifier or str(lead.pk)


def _norm_name(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip()).lower()


def _days_since(dt, now) -> int:
    return max(0, (now - dt).days)


def write_review_queue(path: str | Path, payload: dict) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
