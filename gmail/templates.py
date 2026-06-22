"""Gmail post-accept sequence templates."""
from __future__ import annotations

import json
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from string import Formatter
from types import SimpleNamespace

from linkedin.conf import OUR_COMPANY_NAME, OUR_WEBSITE_URL
from linkedin.exceptions import SheetsError
from linkedin.icp_outbound import safe_company_name
from linkedin.name_utils import greeting_first_name
from linkedin.notifications.sheets import FU_ROLE_TO_ICP

TEMPLATES_PATH = Path(__file__).with_name("icp_emails.json")
ALLOWED_PLACEHOLDERS = frozenset({
    "first_name",
    "last_name",
    "company_name",
    "my_name",
    "our_company_name",
    "our_website_url",
})
_FORMATTER = Formatter()


@dataclass(frozen=True)
class GmailTemplateStep:
    delay_hours: float
    subject_variants: list[str]
    body_variants: list[str]


@dataclass(frozen=True)
class FilledEmail:
    subject: str
    body: str


@dataclass(frozen=True)
class TemplateValidationResult:
    enabled_steps: int
    disabled_blocks: int


def _load() -> dict:
    return json.loads(TEMPLATES_PATH.read_text())


def _icp_for_role(role: str) -> str:
    icp = FU_ROLE_TO_ICP.get(role)
    if not icp:
        raise SheetsError(f"gmail templates: ROLE {role!r} has no ICP mapping")
    return icp


def _field_root(field_name: str) -> str:
    root = field_name
    for separator in (".", "["):
        root = root.split(separator, 1)[0]
    return root


def _placeholder_names(template: str, *, context: str) -> set[str]:
    try:
        parsed = list(_FORMATTER.parse(template))
    except ValueError as exc:
        raise SheetsError(f"gmail templates: {context} has invalid braces: {exc}") from exc

    names: set[str] = set()
    for _literal, field_name, format_spec, conversion in parsed:
        if field_name is None:
            continue
        if not field_name:
            raise SheetsError(f"gmail templates: {context} uses positional placeholder {{}}")
        root = _field_root(field_name)
        if root != field_name:
            raise SheetsError(
                f"gmail templates: {context} uses unsupported placeholder {field_name!r}; "
                f"use {{{root}}}"
            )
        if conversion or format_spec:
            raise SheetsError(
                f"gmail templates: {context} uses unsupported formatting on {{{field_name}}}"
            )
        names.add(root)
    return names


def _validate_template_string(template: str, *, context: str) -> None:
    unknown = _placeholder_names(template, context=context) - ALLOWED_PLACEHOLDERS
    if unknown:
        allowed = ", ".join(sorted(ALLOWED_PLACEHOLDERS))
        found = ", ".join(sorted(unknown))
        raise SheetsError(
            f"gmail templates: {context} has unknown placeholder(s): {found}. "
            f"Allowed: {allowed}"
        )


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
        raw_delay_hours = item.get("delay_hours", 0)
        try:
            delay_hours = float(raw_delay_hours)
        except (TypeError, ValueError) as exc:
            raise SheetsError(
                f"gmail templates: step {idx} has invalid delay_hours={raw_delay_hours!r}"
            ) from exc
        if not isfinite(delay_hours):
            raise SheetsError(
                f"gmail templates: step {idx} has invalid delay_hours={raw_delay_hours!r}"
            )
        if delay_hours < 0:
            raise SheetsError(f"gmail templates: step {idx} has negative delay_hours")
        for variant_idx, subject in enumerate(subjects):
            _validate_template_string(
                subject,
                context=f"{sender}.{icp}.step-{idx}.subject-{variant_idx}",
            )
        for variant_idx, body in enumerate(bodies):
            _validate_template_string(
                body,
                context=f"{sender}.{icp}.step-{idx}.body-{variant_idx}",
            )
        out.append(
            GmailTemplateStep(
                delay_hours=delay_hours,
                subject_variants=list(subjects),
                body_variants=list(bodies),
            )
        )
    return out


def steps_for_icp(*, sender: str, icp: str, sequence_name: str = "") -> list[GmailTemplateStep]:
    return steps(sender=sender, icp=icp, sequence_name=sequence_name)


def steps_for_lead(*, sender: str, role: str, sequence_name: str = "") -> list[GmailTemplateStep]:
    return steps_for_icp(sender=sender, icp=_icp_for_role(role), sequence_name=sequence_name)


def render_for_icp(
    *,
    sender: str,
    icp: str,
    lead,
    step_index: int,
    sequence_name: str = "",
) -> FilledEmail:
    sequence_steps = steps_for_icp(
        sender=sender,
        icp=icp,
        sequence_name=sequence_name,
    )
    if step_index < 0 or step_index >= len(sequence_steps):
        raise SheetsError(
            f"gmail templates: {sender}.{icp}.{sequence_name} has no step {step_index}"
        )
    step = sequence_steps[step_index]
    lead_id = getattr(lead, "id", None)
    subject = step.subject_variants[(lead_id or 0) % len(step.subject_variants)]
    body = step.body_variants[(lead_id or 0) % len(step.body_variants)]
    company_name = safe_company_name(getattr(lead, "company_name", "") or "") or "your team"

    values = {
        "first_name": greeting_first_name(getattr(lead, "first_name", "") or ""),
        "last_name": getattr(lead, "last_name", "") or "",
        "company_name": company_name,
        "my_name": sender,
        "our_company_name": OUR_COMPANY_NAME,
        "our_website_url": OUR_WEBSITE_URL,
    }
    return FilledEmail(
        subject=subject.format(**values).strip(),
        body=body.format(**values).strip(),
    )


def render_for_lead(
    *,
    sender: str,
    role: str,
    lead,
    step_index: int,
    sequence_name: str = "",
) -> FilledEmail:
    return render_for_icp(
        sender=sender,
        icp=_icp_for_role(role),
        sequence_name=sequence_name,
        lead=lead,
        step_index=step_index,
    )


def _assert_no_leftover_placeholders(*, rendered: FilledEmail, context: str) -> None:
    for field_name, value in (("subject", rendered.subject), ("body", rendered.body)):
        if "{" in value or "}" in value:
            raise SheetsError(
                f"gmail templates: {context} rendered leftover braces in {field_name}"
            )


def validate_all_templates() -> TemplateValidationResult:
    """Validate every checked-in Gmail template without sending email."""
    by_sender = _load()
    if not isinstance(by_sender, dict):
        raise SheetsError("gmail templates: root must be an object")

    enabled_steps = 0
    disabled_blocks = 0
    validation_lead = SimpleNamespace(
        id=0,
        first_name="Ada",
        last_name="Lovelace",
        company_name="Analytical Engines",
    )
    for sender, sender_block in by_sender.items():
        if not isinstance(sender_block, dict):
            raise SheetsError(f"gmail templates: sender {sender!r} block must be an object")
        for icp, raw in sender_block.items():
            if raw == []:
                disabled_blocks += 1
                continue
            sequence_steps = steps(sender=sender, icp=icp)
            for step_index in range(len(sequence_steps)):
                rendered = render_for_icp(
                    sender=sender,
                    icp=icp,
                    lead=validation_lead,
                    step_index=step_index,
                )
                _assert_no_leftover_placeholders(
                    rendered=rendered,
                    context=f"{sender}.{icp}.step-{step_index}",
                )
                enabled_steps += 1
    return TemplateValidationResult(
        enabled_steps=enabled_steps,
        disabled_blocks=disabled_blocks,
    )
