"""Gmail fallback sequence templates."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from linkedin.conf import OUR_COMPANY_NAME, OUR_WEBSITE_URL
from linkedin.exceptions import SheetsError
from linkedin.name_utils import greeting_first_name
from linkedin.notifications.sheets import FU_ROLE_TO_ICP

TEMPLATES_PATH = Path(__file__).with_name("icp_emails.json")


@dataclass(frozen=True)
class GmailTemplateStep:
    delay_days: int
    subject_variants: list[str]
    body_variants: list[str]


@dataclass(frozen=True)
class FilledEmail:
    subject: str
    body: str


def _load() -> dict:
    return json.loads(TEMPLATES_PATH.read_text())


def _icp_for_role(role: str) -> str:
    icp = FU_ROLE_TO_ICP.get(role)
    if not icp:
        raise SheetsError(f"gmail templates: ROLE {role!r} has no ICP mapping")
    return icp


def steps(*, sender: str, icp: str, sequence_name: str = "") -> list[GmailTemplateStep]:
    by_sender = _load()
    sender_block = by_sender.get(sender)
    if sender_block is None:
        raise SheetsError(f"gmail templates: sender {sender!r} has no block")
    raw = sender_block.get(icp)
    if raw is None:
        raise SheetsError(f"gmail templates: sender {sender!r} has no ICP {icp!r}")
    if not isinstance(raw, list) or not raw:
        raise SheetsError(f"gmail templates: {sender}.{icp} must be a non-empty step list")
    out: list[GmailTemplateStep] = []
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            raise SheetsError(f"gmail templates: step {idx} must be an object")
        subjects = item.get("subject_variants")
        bodies = item.get("body_variants")
        if not isinstance(subjects, list) or not subjects or not all(
            isinstance(v, str) and v.strip() for v in subjects
        ):
            raise SheetsError(f"gmail templates: step {idx} needs subject_variants")
        if not isinstance(bodies, list) or not bodies or not all(
            isinstance(v, str) and v.strip() for v in bodies
        ):
            raise SheetsError(f"gmail templates: step {idx} needs body_variants")
        delay_days = int(item.get("delay_days", 0))
        if delay_days < 0:
            raise SheetsError(f"gmail templates: step {idx} has negative delay_days")
        out.append(
            GmailTemplateStep(
                delay_days=delay_days,
                subject_variants=list(subjects),
                body_variants=list(bodies),
            )
        )
    return out


def steps_for_lead(*, sender: str, role: str, sequence_name: str = "") -> list[GmailTemplateStep]:
    return steps(sender=sender, icp=_icp_for_role(role), sequence_name=sequence_name)


def render_for_lead(
    *,
    sender: str,
    role: str,
    lead,
    step_index: int,
    sequence_name: str = "",
) -> FilledEmail:
    sequence_steps = steps_for_lead(
        sender=sender,
        role=role,
        sequence_name=sequence_name,
    )
    if step_index < 0 or step_index >= len(sequence_steps):
        raise SheetsError(
            f"gmail templates: {sender}.{role}.{sequence_name} has no step {step_index}"
        )
    step = sequence_steps[step_index]
    lead_id = getattr(lead, "id", None)
    subject = step.subject_variants[(lead_id or 0) % len(step.subject_variants)]
    body = step.body_variants[(lead_id or 0) % len(step.body_variants)]

    values = {
        "first_name": greeting_first_name(getattr(lead, "first_name", "") or ""),
        "last_name": getattr(lead, "last_name", "") or "",
        "company_name": getattr(lead, "company_name", "") or "",
        "my_name": sender,
        "our_company_name": OUR_COMPANY_NAME,
        "our_website_url": OUR_WEBSITE_URL,
    }
    return FilledEmail(
        subject=subject.format(**values).strip(),
        body=body.format(**values).strip(),
    )
