"""Hourly synthesis pass: email extraction (D1) and Wants Meeting LLM (D2).

Designed to run inside the per-Deal loop of manage.py sync_attio. All
operations are best-effort — failures must never block the existing
Stage/Status sync from completing.
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
from linkedin.notifications.attio import (
    OUTREACH_RANK,
    STATUS_WANTS_MEETING,
    add_person_email,
    create_person_note,
    get_person_outreach_status,
    set_person_outreach_status,
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
    from linkedin.conf import AI_MODEL, LLM_API_KEY, LLM_API_BASE

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


def synthesize_for_deal(deal) -> None:
    """Run D1 (email) and D2 (wants meeting) for a single Deal. Best-effort.

    Gates (any one true → skip the corresponding pass):
      D1: lead.email is already populated.
      D2: deal.wants_meeting_detected_at is set, OR current Outreach status
          rank >= Wants Meeting rank.
    Outer gate: no new messages since last_synthesized_at.
    """
    lead = deal.lead
    msgs = list(lead.messages.all())
    if not msgs:
        return

    last_msg_at = max(m.sent_at for m in msgs)
    if deal.last_synthesized_at and deal.last_synthesized_at >= last_msg_at:
        return

    # D1: Email extraction.
    if not lead.email:
        try:
            extracted = extract_email_from_messages(msgs)
            if extracted:
                lead.email = extracted
                lead.save(update_fields=["email"])
                if lead.attio_person_id:
                    add_person_email(lead.attio_person_id, extracted)
        except Exception as e:
            logger.warning("D1 email extraction failed for lead %s: %s", lead.pk, e)

    # D2: Wants Meeting LLM.
    should_run_d2 = (
        deal.wants_meeting_detected_at is None
        and lead.attio_person_id
    )
    if should_run_d2:
        try:
            current_status = get_person_outreach_status(lead.attio_person_id)
            current_rank = OUTREACH_RANK.get(current_status, 0)
            if current_rank < OUTREACH_RANK[STATUS_WANTS_MEETING]:
                decision = detect_wants_meeting(msgs)
                if decision.wants_meeting:
                    if should_patch_outreach_status(current_status, STATUS_WANTS_MEETING):
                        set_person_outreach_status(lead.attio_person_id, STATUS_WANTS_MEETING)
                        try:
                            create_person_note(
                                person_id=lead.attio_person_id,
                                title="Wants Meeting (auto-detected)",
                                content=(
                                    f"Flagged based on message thread: {decision.reason}\n\n"
                                    f"— Auto-flagged by sync_attio synthesis pass on "
                                    f"{timezone.now().date().isoformat()}."
                                ),
                            )
                        except Exception as e:
                            logger.warning("Could not write Attio note: %s", e)
                    deal.wants_meeting_detected_at = timezone.now()
        except Exception as e:
            logger.warning("D2 wants-meeting detection failed for deal %s: %s", deal.pk, e)

    deal.last_synthesized_at = timezone.now()
    deal.save(update_fields=["last_synthesized_at", "wants_meeting_detected_at"])
