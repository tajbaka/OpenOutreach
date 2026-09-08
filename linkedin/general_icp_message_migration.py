"""Render legacy sender role-persona tabs into the general wide schema."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping, Sequence

from linkedin.exceptions import GeneralICPMessageError
from linkedin.general_icp_messages import (
    CHANNEL_GMAIL,
    CHANNEL_LINKEDIN_CONNECT,
    CHANNEL_LINKEDIN_FOLLOWUP,
    GENERAL_ICP_MESSAGES_HEADERS,
    GENERAL_ICP_PROGRAM_KEY,
    GENERAL_ICP_PROGRAM_NAME,
    parse_general_icp_message_rows,
)
from linkedin.icp_outbound import CSP_ROLE_PERSONA_AUTHORING_BUCKETS
from linkedin.operators import resolve_operator


_FOLLOWUP_RE = re.compile(r"^followup message(?:\s+(\d+))?$", re.IGNORECASE)
_EMAIL_SUBJECT_RE = re.compile(r"^email subject(?:\s+(\d+))?$", re.IGNORECASE)
_EMAIL_BODY_RE = re.compile(r"^email body(?:\s+(\d+))?$", re.IGNORECASE)


@dataclass(frozen=True)
class _Candidate:
    sender: str
    audience_key: str
    role_persona: str
    channel: str
    step_key: str
    step_index: int
    delay_hours: int | float
    subject: str
    body: str

    @property
    def route(self) -> tuple[object, ...]:
        return (
            self.audience_key,
            self.role_persona,
            self.channel,
            self.step_key,
            self.step_index,
        )

    @property
    def copy(self) -> tuple[object, ...]:
        return (self.delay_hours, self.subject, self.body)


def render_role_persona_migration_rows(
    sender_rows: Mapping[str, Sequence[Sequence[object]]],
    *,
    program_key: str,
    program_name: str,
) -> list[list[str]]:
    """Convert role-persona rows and collapse identical sender sequences."""
    if program_key != GENERAL_ICP_PROGRAM_KEY or program_name != GENERAL_ICP_PROGRAM_NAME:
        raise GeneralICPMessageError(
            "General ICP Messages uses the fixed FedRAMP Marketplace CSP program identity"
        )
    normalized_senders = tuple(dict.fromkeys(
        resolve_operator(sender) for sender in sender_rows if resolve_operator(sender)
    ))
    if not normalized_senders:
        raise GeneralICPMessageError("at least one sender tab is required for migration")

    candidates: list[_Candidate] = []
    for raw_sender, rows in sender_rows.items():
        sender = resolve_operator(raw_sender)
        candidates.extend(_legacy_candidates(sender, rows))
    if not candidates:
        raise GeneralICPMessageError("selected sender tabs contain no role-persona rows")

    rendered: list[list[str]] = [list(GENERAL_ICP_MESSAGES_HEADERS)]
    by_sender_audience: dict[tuple[str, str], list[_Candidate]] = {}
    for candidate in candidates:
        by_sender_audience.setdefault((candidate.sender, candidate.audience_key), []).append(candidate)

    audiences = sorted({candidate.audience_key for candidate in candidates})
    for audience_key in audiences:
        sequences = {
            sender: tuple(sorted(
                by_sender_audience.get((sender, audience_key), ()),
                key=lambda item: (item.channel, item.step_index, item.step_key),
            ))
            for sender in normalized_senders
        }
        present = {sender: sequence for sender, sequence in sequences.items() if sequence}
        if len(present) == len(normalized_senders) and len({
            tuple(candidate.copy + (candidate.channel, candidate.step_key, candidate.step_index) for candidate in sequence)
            for sequence in present.values()
        }) == 1:
            exemplar = present[normalized_senders[0]]
            rendered.append(_render_wide_row(exemplar))
            continue
        raise GeneralICPMessageError(
            f"{audience_key!r} differs by sender; the shared General tab cannot "
            "represent sender-specific variants"
        )

    parse_general_icp_message_rows(rendered)
    return rendered


def _legacy_candidates(
    sender: str,
    rows: Sequence[Sequence[object]],
) -> list[_Candidate]:
    if not rows:
        raise GeneralICPMessageError(f"{sender} ICP Messages tab is empty")
    headers = [_text(value) for value in rows[0]]
    lowered = [header.casefold() for header in headers]
    try:
        icp_index = lowered.index("icp")
        connect_index = lowered.index("connect message")
    except ValueError as exc:
        raise GeneralICPMessageError(
            f"{sender} ICP Messages is missing ICP or Connect Message"
        ) from exc

    followups: dict[int, int] = {}
    subjects: dict[int, int] = {}
    bodies: dict[int, int] = {}
    for index, header in enumerate(headers):
        if match := _FOLLOWUP_RE.fullmatch(header):
            followups[int(match.group(1) or 1)] = index
        elif match := _EMAIL_SUBJECT_RE.fullmatch(header):
            subjects[int(match.group(1) or 1)] = index
        elif match := _EMAIL_BODY_RE.fullmatch(header):
            bodies[int(match.group(1) or 1)] = index
    if set(subjects) != set(bodies):
        raise GeneralICPMessageError(
            f"{sender} ICP Messages must pair every Email Subject N with Email Body N"
        )

    out: list[_Candidate] = []
    seen_personas: set[str] = set()
    for row_number, row in enumerate(rows[1:], start=2):
        persona = _cell(row, icp_index)
        if persona not in CSP_ROLE_PERSONA_AUTHORING_BUCKETS:
            continue
        if persona in seen_personas:
            raise GeneralICPMessageError(
                f"{sender} ICP Messages row {row_number} duplicates {persona!r}"
            )
        seen_personas.add(persona)
        audience_key = _keyify(persona)
        connect = _cell(row, connect_index)
        if not connect:
            raise GeneralICPMessageError(
                f"{sender} ICP Messages row {row_number} has blank Connect Message"
            )
        out.append(_Candidate(
            sender=sender,
            audience_key=audience_key,
            role_persona=persona,
            channel=CHANNEL_LINKEDIN_CONNECT,
            step_key="connect",
            step_index=0,
            delay_hours=0,
            subject="",
            body=connect,
        ))

        for step_number, index in sorted(followups.items()):
            body = _cell(row, index)
            if not body:
                continue
            out.append(_Candidate(
                sender=sender,
                audience_key=audience_key,
                role_persona=persona,
                channel=CHANNEL_LINKEDIN_FOLLOWUP,
                step_key=f"followup-{step_number}",
                step_index=step_number - 1,
                delay_hours=_default_linkedin_followup_delay(step_number),
                subject="",
                body=body,
            ))

        for step_number in sorted(subjects):
            subject = _cell(row, subjects[step_number])
            body = _cell(row, bodies[step_number])
            if not subject and not body:
                continue
            if not subject or not body:
                raise GeneralICPMessageError(
                    f"{sender} ICP Messages row {row_number} Email step {step_number} "
                    "must include both subject and body"
                )
            out.append(_Candidate(
                sender=sender,
                audience_key=audience_key,
                role_persona=persona,
                channel=CHANNEL_GMAIL,
                step_key=f"email-{step_number}",
                step_index=step_number - 1,
                delay_hours=_default_email_delay(step_number),
                subject=subject,
                body=body,
            ))
    return out


def _render_wide_row(
    sequence: Sequence[_Candidate],
) -> list[str]:
    if not sequence:
        raise GeneralICPMessageError("cannot render an empty ICP sequence")
    exemplar = sequence[0]
    unsupported = [
        item for item in sequence
        if item.step_index > 1
    ]
    if unsupported:
        raise GeneralICPMessageError(
            f"{exemplar.role_persona!r} has more than two steps in a channel; "
            "the General wide layout supports two LinkedIn and two Gmail steps"
        )
    by_route = {(item.channel, item.step_index): item for item in sequence}

    def body(channel: str, index: int) -> str:
        item = by_route.get((channel, index))
        return item.body if item else ""

    def subject(index: int) -> str:
        item = by_route.get((CHANNEL_GMAIL, index))
        return item.subject if item else ""

    role, company_size = _legacy_persona_parts(exemplar.role_persona)
    return [
        f"{role} | {company_size} | Stage needs review | Unclear",
        body(CHANNEL_LINKEDIN_CONNECT, 0),
        body(CHANNEL_LINKEDIN_FOLLOWUP, 0),
        subject(0),
        body(CHANNEL_GMAIL, 0),
        body(CHANNEL_LINKEDIN_FOLLOWUP, 1),
        subject(1),
        body(CHANNEL_GMAIL, 1),
    ]


def _legacy_persona_parts(role_persona: str) -> tuple[str, str]:
    parts = [part.strip() for part in role_persona.split("|")]
    if len(parts) != 2 or not parts[0].startswith("CSP ") or not parts[1]:
        raise GeneralICPMessageError(
            f"legacy persona {role_persona!r} must use 'CSP Company Size | Role'"
        )
    return parts[1], parts[0].removeprefix("CSP ").strip()


def _default_email_delay(step_number: int) -> int | float:
    if step_number == 1:
        return 0.33
    if step_number == 2:
        return 192
    return 192 + ((step_number - 2) * 168)


def _default_linkedin_followup_delay(step_number: int) -> int:
    if step_number == 1:
        return 0
    if step_number == 2:
        return 72
    return 72 + ((step_number - 2) * 168)


def _keyify(value: str) -> str:
    key = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    if not key:
        raise GeneralICPMessageError(f"could not derive Audience Key from {value!r}")
    return key[:160].rstrip("-")


def _cell(row: Sequence[object], index: int) -> str:
    return _text(row[index]) if index < len(row) else ""


def _text(value: object) -> str:
    if value is None:
        return ""
    return str(value).replace("\r\n", "\n").replace("\r", "\n").strip()


def _migration_route_sort_key(route: tuple[object, ...]) -> tuple[object, ...]:
    audience, _persona, channel, _step_key, step_index = route
    channel_order = {
        CHANNEL_LINKEDIN_CONNECT: 0,
        CHANNEL_LINKEDIN_FOLLOWUP: 1,
        CHANNEL_GMAIL: 2,
    }
    return (audience, channel_order[str(channel)], step_index)
