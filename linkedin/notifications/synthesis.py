"""Synthesis pass: email extraction (D1) and Wants Meeting LLM (D2).

Designed to run inside the per-Deal loop of manage.py sync_sheets. All
operations are best-effort — failures must never block the existing
Stage/Status sync from completing.

Differences from the Airtable-era version:
- Doesn't write to any external store directly. The caller (sync_sheets)
  reads `current_outreach_status` off the live Sheet row and passes it in,
  then takes the SynthResult back and folds the new status / Notes block
  into the row payload it's about to write. One Sheet write per Lead
  total — no separate PATCH for status, emails, notes.
- D1 still mutates `lead.email` (Django field) directly because that's
  where the truth lives; the Sheet's Email addresses column gets the
  union when sync_sheets builds the row payload.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Iterable

import jinja2
from django.utils import timezone
from pydantic import BaseModel, Field

from crm.models import Message
from linkedin.notifications.sheets import (
    OUTREACH_RANK,
    STATUS_WANTS_MEETING,
    should_patch_outreach_status,
)

logger = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")


# ---------------------------------------------------------------------------
# D1 — Email extraction
# ---------------------------------------------------------------------------


def extract_email_from_messages(messages: Iterable[Message]) -> str:
    """Return the first email mentioned in an inbound message, or ''."""
    for msg in sorted(
        (m for m in messages if m.direction == Message.Direction.INBOUND),
        key=lambda m: m.sent_at,
    ):
        match = EMAIL_RE.search(msg.body or "")
        if match:
            return match.group(0)
    return ""


# ---------------------------------------------------------------------------
# D2 — Wants Meeting LLM detection
# ---------------------------------------------------------------------------


class WantsMeetingDecision(BaseModel):
    wants_meeting: bool = Field(description="True if the prospect expressed meeting intent.")
    reason: str = Field(description="Quoted line if true; 'no clear signal' if false.")


@dataclass
class DetectionResult:
    wants_meeting: bool
    reason: str


def detect_wants_meeting(messages: Iterable[Message]) -> DetectionResult:
    """Run LLM over the thread; return structured decision."""
    msgs = sorted(messages, key=lambda m: m.sent_at)
    llm = _build_llm()
    prompt = _render_prompt(msgs)
    decision = llm.invoke(prompt)
    return DetectionResult(
        wants_meeting=bool(decision.wants_meeting),
        reason=str(decision.reason or ""),
    )


def _build_llm():
    from langchain_openai import ChatOpenAI

    from linkedin.conf import AI_MODEL, LLM_API_BASE, LLM_API_KEY

    if not LLM_API_KEY:
        raise ValueError("LLM_API_KEY is not set")
    base = ChatOpenAI(
        model=AI_MODEL, temperature=0, api_key=LLM_API_KEY,
        base_url=LLM_API_BASE, timeout=30,
    )
    return base.with_structured_output(WantsMeetingDecision)


def _render_prompt(messages: list[Message]) -> str:
    from linkedin.conf import PROMPTS_DIR
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(PROMPTS_DIR)))
    template = env.get_template("wants_meeting.j2")
    payload = [
        {
            "timestamp": m.sent_at.isoformat(),
            "direction": m.direction,
            "body": m.body,
        }
        for m in messages
    ]
    return template.render(messages=payload)


# ---------------------------------------------------------------------------
# D5 — Per-Deal orchestrator
# ---------------------------------------------------------------------------


@dataclass
class SynthResult:
    """What sync_sheets needs to know after synthesis runs.

    `wants_meeting_now` is True only on the first detection — once
    `Deal.wants_meeting_detected_at` is set, subsequent calls return False
    so the caller doesn't keep re-advancing the status. The Notes column
    is reserved for human-written notes; we never write to it.
    """
    wants_meeting_now: bool = False


def synthesize_for_deal(
    deal,
    *,
    current_outreach_status: str = "",
) -> SynthResult | None:
    """Run D1 (email) and D2 (wants meeting) for a single Deal. Best-effort.

    Gates (any one true → skip the corresponding pass):
      D1: lead.email is already populated.
      D2: deal.wants_meeting_detected_at is set, OR current Outreach status
          rank >= Wants Meeting rank.
    Outer gate: no new messages since last_synthesized_at.

    Returns None when the outer gate trips (no new signal). Otherwise
    returns a SynthResult that the caller folds into the Sheet row.
    """
    lead = deal.lead
    msgs = list(lead.messages.all())
    if not msgs:
        return None

    last_msg_at = max(m.sent_at for m in msgs)
    if deal.last_synthesized_at and deal.last_synthesized_at >= last_msg_at:
        return None

    result = SynthResult()

    # D1: Email extraction → mutate Lead.email directly (caller folds into Sheet).
    if not lead.email:
        try:
            extracted = extract_email_from_messages(msgs)
            if extracted:
                lead.email = extracted
                lead.save(update_fields=["email"])
        except Exception as e:
            logger.warning("D1 email extraction failed for lead %s: %s", lead.pk, e)

    # D2: Wants Meeting LLM. Only run when no prior detection AND the
    # current status (read from the sheet by the caller) is rank-below
    # Wants Meeting — humans manually advancing past it should win.
    should_run_d2 = deal.wants_meeting_detected_at is None
    if should_run_d2:
        try:
            current_rank = OUTREACH_RANK.get(current_outreach_status, 0)
            if current_rank < OUTREACH_RANK[STATUS_WANTS_MEETING]:
                decision = detect_wants_meeting(msgs)
                if decision.wants_meeting:
                    if should_patch_outreach_status(
                        current_outreach_status, STATUS_WANTS_MEETING,
                    ):
                        result.wants_meeting_now = True
                    deal.wants_meeting_detected_at = timezone.now()
        except Exception as e:
            logger.warning("D2 wants-meeting detection failed for deal %s: %s", deal.pk, e)

    deal.last_synthesized_at = timezone.now()
    deal.save(update_fields=["last_synthesized_at", "wants_meeting_detected_at"])

    return result
