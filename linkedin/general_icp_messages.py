"""Validate the wide General ICP Messages authoring sheet.

The Google Sheet is deliberately mutable authoring state.  An explicit import
command turns that state into the checked-in LinkedIn and Gmail JSON stores;
runtime code never reads the Sheet.
Parsing and canonicalization are kept free of Django/DB imports so copy can be
reviewed and tested without a database connection.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from math import isfinite
from string import Formatter
from typing import Iterable, Sequence

from linkedin.exceptions import GeneralICPMessageError
from linkedin.operators import CANONICAL_OPERATOR_HANDLES, resolve_operator


GENERAL_ICP_MESSAGES_TAB = "General ICP Messages"
GENERAL_ICP_MESSAGES_HEADERS = (
    "ICP",
    "Connect Message",
    "Followup Message 1",
    "Email Subject 1",
    "Email Body 1",
    "Followup Message 2",
    "Email Subject 2",
    "Email Body 2",
)

GENERAL_ICP_PROGRAM_KEY = "fedramp-marketplace-csp"
GENERAL_ICP_PROGRAM_NAME = "FedRAMP Marketplace CSP Outreach"

FEDRAMP_REVENUE_INTENTS = (
    "Direct agency",
    "CSP ecosystem",
    "Both",
    "Unclear",
)

GENERAL_ICP_LONG_FORM_HEADERS = (
    "ICP",
    "Program Key",
    "Program Name",
    "Audience Key",
    "Role Persona",
    "FedRAMP Segment",
    "Channel",
    "Step Key",
    "Step Index",
    "Delay Hours",
    "Variant Key",
    "Subject",
    "Body",
    "Media",
    "Sender Override",
    "Notes",
)

SCHEMA_VERSION = 1
CHANNEL_LINKEDIN_CONNECT = "linkedin_connect"
CHANNEL_LINKEDIN_FOLLOWUP = "linkedin_followup"
CHANNEL_GMAIL = "gmail"
CHANNELS = (
    CHANNEL_LINKEDIN_CONNECT,
    CHANNEL_LINKEDIN_FOLLOWUP,
    CHANNEL_GMAIL,
)
_CHANNEL_SORT_ORDER = {channel: index for index, channel in enumerate(CHANNELS)}

SUPPORTED_PLACEHOLDERS = frozenset({
    "role",
    "first_name",
    "last_name",
    "company_name",
    "my_name",
    "our_company_name",
    "our_website_url",
})

_KEY_RE = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,159})$")
_FORMATTER = Formatter()


@dataclass(frozen=True)
class GeneralMessage:
    icp_label: str
    audience_key: str
    role_persona: str
    fedramp_segment: str
    channel: str
    step_key: str
    step_index: int
    delay_hours: int | float
    variant_key: str
    subject: str
    body: str
    media: tuple[str, ...]
    sender_override: str
    notes: str
    source_row: int

    def payload(self) -> dict[str, object]:
        """Return only durable, source-independent message fields."""
        return {
            "audience_key": self.audience_key,
            "role_persona": self.role_persona,
            "fedramp_segment": self.fedramp_segment,
            "channel": self.channel,
            "step_key": self.step_key,
            "step_index": self.step_index,
            "delay_hours": self.delay_hours,
            "variant_key": self.variant_key,
            "subject": self.subject,
            "body": self.body,
            "media": list(self.media),
            "sender_override": self.sender_override,
            "notes": self.notes,
        }


@dataclass(frozen=True)
class GeneralMessageProgramDraft:
    key: str
    name: str
    messages: tuple[GeneralMessage, ...]

    def payload(self) -> dict[str, object]:
        return {
            "program_key": self.key,
            "program_name": self.name,
            "messages": [message.payload() for message in self.messages],
        }

    @property
    def content_hash(self) -> str:
        return content_hash(self.payload())


@dataclass(frozen=True)
class PublicationDecision:
    program_key: str
    program_name: str
    action: str
    content_hash: str
    target_version: int
    message_count: int

    def as_dict(self) -> dict[str, object]:
        return {
            "program_key": self.program_key,
            "program_name": self.program_name,
            "action": self.action,
            "content_hash": self.content_hash,
            "target_version": self.target_version,
            "message_count": self.message_count,
        }


def parse_general_icp_message_rows(
    rows: Sequence[Sequence[object]],
) -> tuple[GeneralMessageProgramDraft, ...]:
    """Parse the exact wide Sheet schema into canonical program drafts."""
    return _parse_general_icp_long_rows(_wide_rows_to_long_rows(rows))


def general_drafts_to_wide_rows(
    drafts: Iterable[GeneralMessageProgramDraft],
) -> list[list[str]]:
    """Render canonical drafts into the sequence-friendly Sheet layout."""
    rows: list[list[str]] = [list(GENERAL_ICP_MESSAGES_HEADERS)]
    grouped: dict[tuple[str, str, str], list[tuple[GeneralMessageProgramDraft, GeneralMessage]]] = {}
    for draft in drafts:
        for message in draft.messages:
            grouped.setdefault(
                (draft.key, message.audience_key, message.sender_override.casefold()),
                [],
            ).append((draft, message))

    for group_key in grouped:
        pairs = grouped[group_key]
        draft = pairs[0][0]
        messages = [pair[1] for pair in pairs]
        exemplar = messages[0]
        routes = {(message.channel, message.step_index): message for message in messages}
        if len(routes) != len(messages):
            raise GeneralICPMessageError(
                f"audience {exemplar.audience_key!r} cannot fit the wide primary-route layout"
            )
        if any(
            message.variant_key != "primary"
            or message.media
            or message.sender_override
            or message.notes
            for message in messages
        ):
            raise GeneralICPMessageError(
                f"audience {exemplar.audience_key!r} uses hidden routing metadata not "
                "supported by the eight-column layout"
            )

        def body(channel: str, index: int) -> str:
            message = routes.get((channel, index))
            return message.body if message else ""

        def subject(index: int) -> str:
            message = routes.get((CHANNEL_GMAIL, index))
            return message.subject if message else ""

        rows.append([
            exemplar.icp_label,
            body(CHANNEL_LINKEDIN_CONNECT, 0),
            body(CHANNEL_LINKEDIN_FOLLOWUP, 0),
            subject(0),
            body(CHANNEL_GMAIL, 0),
            body(CHANNEL_LINKEDIN_FOLLOWUP, 1),
            subject(1),
            body(CHANNEL_GMAIL, 1),
        ])
    return rows


def _wide_rows_to_long_rows(
    rows: Sequence[Sequence[object]],
) -> list[list[str]]:
    if not rows:
        raise GeneralICPMessageError(
            f"{GENERAL_ICP_MESSAGES_TAB} is empty; expected a header row"
        )
    header = tuple(_text(cell) for cell in rows[0])
    if header != GENERAL_ICP_MESSAGES_HEADERS:
        raise GeneralICPMessageError(
            f"{GENERAL_ICP_MESSAGES_TAB} headers must exactly equal "
            f"{list(GENERAL_ICP_MESSAGES_HEADERS)!r}; got {list(header)!r}"
        )

    long_rows: list[list[str]] = [list(GENERAL_ICP_LONG_FORM_HEADERS)]
    routes = (
        ("Connect Message", CHANNEL_LINKEDIN_CONNECT, "connect", 0, 0, ""),
        ("Followup Message 1", CHANNEL_LINKEDIN_FOLLOWUP, "followup-1", 0, 0, ""),
        ("Email Body 1", CHANNEL_GMAIL, "email-1", 0, 0.33, "Email Subject 1"),
        ("Followup Message 2", CHANNEL_LINKEDIN_FOLLOWUP, "followup-2", 1, 72, ""),
        ("Email Body 2", CHANNEL_GMAIL, "email-2", 1, 192, "Email Subject 2"),
    )
    for row_number, raw_row in enumerate(rows[1:], start=2):
        raw_values = list(raw_row)
        if len(raw_values) > len(GENERAL_ICP_MESSAGES_HEADERS) and any(
            _text(value) for value in raw_values[len(GENERAL_ICP_MESSAGES_HEADERS):]
        ):
            raise GeneralICPMessageError(
                f"row {row_number} has values beyond the exact managed columns"
            )
        values = [
            _text(raw_values[index]) if index < len(raw_values) else ""
            for index in range(len(GENERAL_ICP_MESSAGES_HEADERS))
        ]
        if not any(values):
            continue
        by_header = dict(zip(GENERAL_ICP_MESSAGES_HEADERS, values, strict=True))
        icp_label = _required(by_header["ICP"], "ICP", row_number)
        role_persona, fedramp_segment, audience_key = _derive_general_icp_metadata(
            icp_label,
            context=f"row {row_number}",
        )
        if not by_header["Connect Message"]:
            raise GeneralICPMessageError(f"row {row_number} Connect Message is required")
        for body_header, channel, step_key, step_index, delay_hours, subject_header in routes:
            body = by_header[body_header]
            subject = by_header[subject_header] if subject_header else ""
            if not body and not subject:
                continue
            if channel == CHANNEL_GMAIL and (not body or not subject):
                raise GeneralICPMessageError(
                    f"row {row_number} {subject_header} and {body_header} must both be filled"
                )
            if not body:
                raise GeneralICPMessageError(f"row {row_number} {body_header} is required")
            long_rows.append([
                icp_label,
                GENERAL_ICP_PROGRAM_KEY,
                GENERAL_ICP_PROGRAM_NAME,
                audience_key,
                role_persona,
                fedramp_segment,
                channel,
                step_key,
                str(step_index),
                str(delay_hours),
                "primary",
                subject,
                body,
                "[]",
                "",
                "",
            ])
    return long_rows


def _derive_general_icp_metadata(
    icp_label: str,
    *,
    context: str = "ICP",
) -> tuple[str, str, str]:
    parts = [part.strip() for part in icp_label.split("|")]
    if len(parts) != 4 or not all(parts):
        raise GeneralICPMessageError(
            f"{context} must use "
            "'Role | Company Size | FedRAMP Stage | Revenue Intent'"
        )
    role, company_size, fedramp_segment, revenue_intent = parts
    if revenue_intent not in FEDRAMP_REVENUE_INTENTS:
        raise GeneralICPMessageError(
            f"{context} Revenue Intent must be one of "
            f"{list(FEDRAMP_REVENUE_INTENTS)!r}; got {revenue_intent!r}"
        )
    role_persona = f"CSP {company_size} | {role}"
    audience_key = _keyify(
        f"CSP {company_size} {role} {fedramp_segment} {revenue_intent}"
    )
    return role_persona, fedramp_segment, audience_key


def _keyify(value: str) -> str:
    normalized = value.replace("→", " to ")
    key = re.sub(r"[^a-z0-9]+", "-", normalized.casefold()).strip("-")
    if not key:
        raise GeneralICPMessageError(f"could not derive a routing key from {value!r}")
    return key[:160].rstrip("-")


def _parse_general_icp_long_rows(
    rows: Sequence[Sequence[object]],
) -> tuple[GeneralMessageProgramDraft, ...]:
    """Validate normalized long rows produced from the authoring surface."""
    messages_by_program: dict[str, list[GeneralMessage]] = {}
    names_by_program: dict[str, str] = {}
    unique_routes: dict[tuple[str, ...], int] = {}
    audience_metadata: dict[tuple[str, str], tuple[str, str, str, int]] = {}
    step_keys_by_index: dict[tuple[str, str, str, str, int], tuple[str, int]] = {}
    step_indexes_by_key: dict[tuple[str, str, str, str, str], tuple[int, int]] = {}
    step_delays: dict[tuple[str, str, str, str, str], tuple[int | float, int]] = {}

    for row_number, raw_row in enumerate(rows[1:], start=2):
        raw_values = list(raw_row)
        if len(raw_values) > len(GENERAL_ICP_LONG_FORM_HEADERS) and any(
            _text(value) for value in raw_values[len(GENERAL_ICP_LONG_FORM_HEADERS):]
        ):
            raise GeneralICPMessageError(
                f"row {row_number} has values beyond the exact managed columns"
            )
        values = [
            _text(raw_values[index]) if index < len(raw_values) else ""
            for index in range(len(GENERAL_ICP_LONG_FORM_HEADERS))
        ]
        if not any(values):
            continue
        by_header = dict(zip(GENERAL_ICP_LONG_FORM_HEADERS, values, strict=True))

        program_key = _required_key(by_header["Program Key"], "Program Key", row_number)
        program_name = _required(by_header["Program Name"], "Program Name", row_number)
        if len(program_name) > 200:
            raise GeneralICPMessageError(f"row {row_number} Program Name exceeds 200 characters")
        audience_key = _required_key(by_header["Audience Key"], "Audience Key", row_number)
        channel = _required(by_header["Channel"], "Channel", row_number)
        if channel not in CHANNELS:
            raise GeneralICPMessageError(
                f"row {row_number} Channel must be one of {list(CHANNELS)!r}; got {channel!r}"
            )
        step_key = _required_key(by_header["Step Key"], "Step Key", row_number)
        step_index = _nonnegative_integer(by_header["Step Index"], "Step Index", row_number)
        delay_hours = _nonnegative_number(by_header["Delay Hours"], "Delay Hours", row_number)
        variant_key = _required_key(by_header["Variant Key"], "Variant Key", row_number)
        subject = by_header["Subject"]
        body = _required(by_header["Body"], "Body", row_number)
        media = parse_media_cell(by_header["Media"], row_number=row_number)
        sender_override = resolve_operator(by_header["Sender Override"])
        if len(sender_override) > 64:
            raise GeneralICPMessageError(
                f"row {row_number} Sender Override exceeds 64 characters"
            )
        if sender_override and sender_override not in CANONICAL_OPERATOR_HANDLES:
            raise GeneralICPMessageError(
                f"row {row_number} Sender Override must be blank or a known "
                f"canonical sender; got {sender_override!r}"
            )

        if channel == CHANNEL_GMAIL:
            if not subject:
                raise GeneralICPMessageError(
                    f"row {row_number} Gmail messages require both Subject and Body"
                )
        elif subject:
            raise GeneralICPMessageError(
                f"row {row_number} {channel} Subject must be blank"
            )
        if len(subject) > 998:
            raise GeneralICPMessageError(f"row {row_number} Subject exceeds 998 characters")
        _validate_placeholders(subject, context=f"row {row_number} Subject")
        _validate_placeholders(body, context=f"row {row_number} Body")

        existing_name = names_by_program.setdefault(program_key, program_name)
        if existing_name != program_name:
            raise GeneralICPMessageError(
                f"row {row_number} Program Name {program_name!r} conflicts with "
                f"{existing_name!r} for Program Key {program_key!r}"
            )

        icp_label = _required(by_header["ICP"], "ICP", row_number)
        role_persona = by_header["Role Persona"]
        fedramp_segment = by_header["FedRAMP Segment"]
        metadata_key = (program_key, audience_key)
        existing_metadata = audience_metadata.setdefault(
            metadata_key,
            (icp_label, role_persona, fedramp_segment, row_number),
        )
        if existing_metadata[:3] != (icp_label, role_persona, fedramp_segment):
            raise GeneralICPMessageError(
                f"row {row_number} audience {audience_key!r} changes ICP, Role Persona, "
                f"or FedRAMP Segment from row {existing_metadata[3]}"
            )

        sender_identity = sender_override.casefold()
        route_key = (
            program_key,
            audience_key,
            channel,
            step_key,
            variant_key,
            sender_identity,
        )
        duplicate_row = unique_routes.setdefault(route_key, row_number)
        if duplicate_row != row_number:
            raise GeneralICPMessageError(
                f"row {row_number} duplicates the message route from row {duplicate_row}"
            )

        index_key = (program_key, audience_key, channel, sender_identity, step_index)
        existing_step_key = step_keys_by_index.setdefault(index_key, (step_key, row_number))
        if existing_step_key[0] != step_key:
            raise GeneralICPMessageError(
                f"row {row_number} maps Step Index {step_index} to {step_key!r}, but row "
                f"{existing_step_key[1]} maps it to {existing_step_key[0]!r}"
            )
        key_key = (program_key, audience_key, channel, sender_identity, step_key)
        existing_step_index = step_indexes_by_key.setdefault(key_key, (step_index, row_number))
        if existing_step_index[0] != step_index:
            raise GeneralICPMessageError(
                f"row {row_number} maps Step Key {step_key!r} to index {step_index}, but row "
                f"{existing_step_index[1]} maps it to {existing_step_index[0]}"
            )
        existing_delay = step_delays.setdefault(key_key, (delay_hours, row_number))
        if existing_delay[0] != delay_hours:
            raise GeneralICPMessageError(
                f"row {row_number} changes Delay Hours for {step_key!r} from row "
                f"{existing_delay[1]}"
            )

        message = GeneralMessage(
            icp_label=icp_label,
            audience_key=audience_key,
            role_persona=role_persona,
            fedramp_segment=fedramp_segment,
            channel=channel,
            step_key=step_key,
            step_index=step_index,
            delay_hours=delay_hours,
            variant_key=variant_key,
            subject=subject,
            body=body,
            media=media,
            sender_override=sender_override,
            notes=by_header["Notes"],
            source_row=row_number,
        )
        messages_by_program.setdefault(program_key, []).append(message)

    if not messages_by_program:
        raise GeneralICPMessageError(f"{GENERAL_ICP_MESSAGES_TAB} has no message rows")

    drafts = []
    for program_key in sorted(messages_by_program):
        messages = tuple(sorted(messages_by_program[program_key], key=_message_sort_key))
        drafts.append(GeneralMessageProgramDraft(
            key=program_key,
            name=names_by_program[program_key],
            messages=messages,
        ))
    return tuple(drafts)


def canonical_payload_json(payload: dict[str, object]) -> str:
    """Return the one byte-stable JSON representation used for hashing."""
    return json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def content_hash(payload: dict[str, object]) -> str:
    return hashlib.sha256(canonical_payload_json(payload).encode("utf-8")).hexdigest()


def plan_message_program_publication(
    drafts: Iterable[GeneralMessageProgramDraft],
) -> tuple[PublicationDecision, ...]:
    """Read current versions and describe the exact no-write publication diff."""
    from linkedin.models import MessageProgram

    decisions = []
    for draft in sorted(drafts, key=lambda item: item.key):
        draft_hash = draft.content_hash
        program = MessageProgram.objects.filter(key=draft.key).first()
        if program is None:
            action = "create_program_and_version"
            target_version = 1
        else:
            matching = program.versions.filter(content_hash=draft_hash).first()
            latest = program.versions.order_by("-version").first()
            if matching is not None:
                action = "unchanged" if latest and matching.pk == latest.pk else "reuse_version"
                target_version = matching.version
            else:
                action = "create_version"
                target_version = (latest.version if latest else 0) + 1
        decisions.append(PublicationDecision(
            program_key=draft.key,
            program_name=draft.name,
            action=action,
            content_hash=draft_hash,
            target_version=target_version,
            message_count=len(draft.messages),
        ))
    return tuple(decisions)


def publish_message_programs(
    drafts: Iterable[GeneralMessageProgramDraft],
    *,
    published_by: str,
) -> tuple[PublicationDecision, ...]:
    """Create or reuse immutable program versions in one DB transaction."""
    from django.db import transaction

    with transaction.atomic():
        return _publish_message_programs_atomic(
            drafts,
            published_by=published_by,
        )


def _publish_message_programs_atomic(
    drafts: Iterable[GeneralMessageProgramDraft],
    *,
    published_by: str,
) -> tuple[PublicationDecision, ...]:
    from linkedin.models import MessageProgram, MessageProgramVersion

    publisher = str(published_by or "").strip()
    if not publisher:
        raise GeneralICPMessageError("published_by is required when applying publication")
    if len(publisher) > 150:
        raise GeneralICPMessageError("published_by exceeds 150 characters")

    decisions = []
    for draft in sorted(drafts, key=lambda item: item.key):
        draft_hash = draft.content_hash
        program = MessageProgram.objects.select_for_update().filter(key=draft.key).first()
        created_program = program is None
        if program is None:
            program = MessageProgram(key=draft.key, name=draft.name)
            program.full_clean()
            program.save()

        matching = program.versions.filter(content_hash=draft_hash).first()
        latest = program.versions.order_by("-version").first()
        if matching is not None:
            action = "unchanged" if latest and matching.pk == latest.pk else "reuse_version"
            target_version = matching.version
        else:
            target_version = (latest.version if latest else 0) + 1
            version = MessageProgramVersion(
                program=program,
                version=target_version,
                schema_version=SCHEMA_VERSION,
                payload=draft.payload(),
                content_hash=draft_hash,
                published_by=publisher,
                based_on_version=latest,
            )
            version.full_clean()
            version.save()
            action = "create_program_and_version" if created_program else "create_version"

        if program.name != draft.name:
            program.name = draft.name
            program.full_clean()
            program.save(update_fields=["name", "updated_at"])

        decisions.append(PublicationDecision(
            program_key=draft.key,
            program_name=draft.name,
            action=action,
            content_hash=draft_hash,
            target_version=target_version,
            message_count=len(draft.messages),
        ))
    return tuple(decisions)


def parse_media_cell(value: str, *, row_number: int) -> tuple[str, ...]:
    if not value:
        return ()
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise GeneralICPMessageError(
            f"row {row_number} Media must be a JSON list of strings"
        ) from exc
    if not isinstance(parsed, list):
        raise GeneralICPMessageError(f"row {row_number} Media must be a JSON list")
    normalized = []
    for item in parsed:
        if not isinstance(item, str) or not item.strip():
            raise GeneralICPMessageError(
                f"row {row_number} Media entries must be nonblank strings"
            )
        filename = item.strip()
        if filename in normalized:
            raise GeneralICPMessageError(
                f"row {row_number} Media contains duplicate entry {filename!r}"
            )
        normalized.append(filename)
    return tuple(normalized)


def _text(value: object) -> str:
    if value is None:
        return ""
    return str(value).replace("\r\n", "\n").replace("\r", "\n").strip()


def _required(value: str, field: str, row_number: int) -> str:
    if not value:
        raise GeneralICPMessageError(f"row {row_number} {field} is required")
    return value


def _required_key(value: str, field: str, row_number: int) -> str:
    value = _required(value, field, row_number)
    if not _KEY_RE.fullmatch(value):
        raise GeneralICPMessageError(
            f"row {row_number} {field} must be a lowercase key using only letters, "
            "numbers, underscores, and hyphens (maximum 160 characters)"
        )
    return value


def _nonnegative_integer(value: str, field: str, row_number: int) -> int:
    if not re.fullmatch(r"\d+", value):
        raise GeneralICPMessageError(
            f"row {row_number} {field} must be a nonnegative integer"
        )
    return int(value)


def _nonnegative_number(value: str, field: str, row_number: int) -> int | float:
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise GeneralICPMessageError(f"row {row_number} {field} must be numeric") from exc
    if not parsed.is_finite() or parsed < 0:
        raise GeneralICPMessageError(
            f"row {row_number} {field} must be a finite nonnegative number"
        )
    if parsed == parsed.to_integral_value():
        return int(parsed)
    normalized = float(parsed.normalize())
    if not isfinite(normalized) or (normalized == 0 and parsed != 0):
        raise GeneralICPMessageError(
            f"row {row_number} {field} is outside the supported numeric range"
        )
    return normalized


def _validate_placeholders(template: str, *, context: str) -> None:
    try:
        parts = list(_FORMATTER.parse(template))
    except ValueError as exc:
        raise GeneralICPMessageError(f"{context} has invalid braces: {exc}") from exc
    for _literal, field_name, format_spec, conversion in parts:
        if field_name is None:
            continue
        if not field_name:
            raise GeneralICPMessageError(f"{context} uses positional placeholder {{}}")
        if any(separator in field_name for separator in (".", "[")):
            raise GeneralICPMessageError(
                f"{context} uses unsupported nested placeholder {field_name!r}"
            )
        if format_spec or conversion:
            raise GeneralICPMessageError(
                f"{context} uses unsupported formatting on {{{field_name}}}"
            )
        if field_name not in SUPPORTED_PLACEHOLDERS:
            raise GeneralICPMessageError(
                f"{context} has unsupported placeholder {field_name!r}; allowed: "
                f"{sorted(SUPPORTED_PLACEHOLDERS)!r}"
            )


def _message_sort_key(message: GeneralMessage) -> tuple[object, ...]:
    return (
        message.audience_key,
        _CHANNEL_SORT_ORDER[message.channel],
        message.step_index,
        message.step_key,
        message.variant_key,
        message.sender_override.casefold(),
    )
