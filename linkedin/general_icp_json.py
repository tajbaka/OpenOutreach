"""Checked-in JSON boundary for shared General ICP message programs."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from linkedin.conf import ROOT_DIR
from linkedin.exceptions import GeneralICPMessageError
from linkedin.general_icp_messages import (
    CHANNEL_GMAIL,
    GENERAL_ICP_LONG_FORM_HEADERS,
    GeneralMessageProgramDraft,
    _parse_general_icp_long_rows,
)


STORE_SCHEMA_VERSION = 2
LINKEDIN_STORE_PATH = ROOT_DIR / "linkedin" / "icp_messages.json"
GMAIL_STORE_PATH = ROOT_DIR / "gmail" / "icp_emails.json"


def render_general_icp_stores(
    drafts: Iterable[GeneralMessageProgramDraft],
    *,
    linkedin_path: Path = LINKEDIN_STORE_PATH,
    gmail_path: Path = GMAIL_STORE_PATH,
) -> tuple[dict[str, object], dict[str, object]]:
    """Return complete v2 stores while preserving legacy sender programs."""
    linkedin_current = _read_store(linkedin_path)
    gmail_current = _read_store(gmail_path)
    linkedin_programs: dict[str, object] = {}
    gmail_programs: dict[str, object] = {}
    for draft in sorted(drafts, key=lambda item: item.key):
        linkedin_programs[draft.key] = {
            "program_name": draft.name,
            "icp_labels": _icp_labels(draft),
            "messages": [
                message.payload()
                for message in draft.messages
                if message.channel != CHANNEL_GMAIL
            ],
        }
        gmail_programs[draft.key] = {
            "program_name": draft.name,
            "icp_labels": _icp_labels(draft),
            "messages": [
                message.payload()
                for message in draft.messages
                if message.channel == CHANNEL_GMAIL
            ],
        }
    return (
        {
            "schema_version": STORE_SCHEMA_VERSION,
            "shared_programs": linkedin_programs,
            "sender_icps": linkedin_current["sender_icps"],
        },
        {
            "schema_version": STORE_SCHEMA_VERSION,
            "shared_programs": gmail_programs,
            "sender_icps": gmail_current["sender_icps"],
        },
    )


def write_general_icp_stores(
    linkedin_store: dict[str, object],
    gmail_store: dict[str, object],
    *,
    linkedin_path: Path = LINKEDIN_STORE_PATH,
    gmail_path: Path = GMAIL_STORE_PATH,
) -> None:
    """Atomically replace both generated JSON stores."""
    _write_json(linkedin_path, linkedin_store)
    _write_json(gmail_path, gmail_store)


def load_general_message_programs(
    *,
    linkedin_path: Path = LINKEDIN_STORE_PATH,
    gmail_path: Path = GMAIL_STORE_PATH,
) -> tuple[GeneralMessageProgramDraft, ...]:
    """Load and strictly validate shared programs from both JSON stores."""
    linkedin_store = _read_store(linkedin_path)
    gmail_store = _read_store(gmail_path)
    linkedin_programs = linkedin_store["shared_programs"]
    gmail_programs = gmail_store["shared_programs"]
    if set(linkedin_programs) != set(gmail_programs):
        raise GeneralICPMessageError(
            "LinkedIn and Gmail JSON shared_programs must contain identical program keys"
        )

    long_rows: list[list[str]] = [list(GENERAL_ICP_LONG_FORM_HEADERS)]
    for program_key in sorted(linkedin_programs):
        linkedin_program = _program(linkedin_programs[program_key], program_key, "LinkedIn")
        gmail_program = _program(gmail_programs[program_key], program_key, "Gmail")
        if linkedin_program["program_name"] != gmail_program["program_name"]:
            raise GeneralICPMessageError(
                f"program {program_key!r} has different names in LinkedIn and Gmail JSON"
            )
        if linkedin_program["icp_labels"] != gmail_program["icp_labels"]:
            raise GeneralICPMessageError(
                f"program {program_key!r} has different icp_labels in LinkedIn and Gmail JSON"
            )
        for message in [*linkedin_program["messages"], *gmail_program["messages"]]:
            if not isinstance(message, dict):
                raise GeneralICPMessageError(
                    f"program {program_key!r} contains a non-object message"
                )
            audience_key = str(message.get("audience_key") or "")
            icp_label = linkedin_program["icp_labels"].get(audience_key)
            if not isinstance(icp_label, str) or not icp_label.strip():
                raise GeneralICPMessageError(
                    f"program {program_key!r} has no ICP label for {audience_key!r}"
                )
            long_rows.append(_payload_message_row(
                program_key,
                linkedin_program["program_name"],
                icp_label,
                message,
            ))
    return _parse_general_icp_long_rows(long_rows)


def _read_store(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GeneralICPMessageError(f"failed reading {path}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != STORE_SCHEMA_VERSION:
        raise GeneralICPMessageError(
            f"{path} must use shared ICP JSON schema version {STORE_SCHEMA_VERSION}"
        )
    if not isinstance(payload.get("shared_programs"), dict):
        raise GeneralICPMessageError(f"{path} shared_programs must be an object")
    if not isinstance(payload.get("sender_icps"), dict):
        raise GeneralICPMessageError(f"{path} sender_icps must be an object")
    return payload


def _program(value: object, key: str, store_name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise GeneralICPMessageError(f"{store_name} program {key!r} must be an object")
    if not isinstance(value.get("program_name"), str) or not value["program_name"].strip():
        raise GeneralICPMessageError(f"{store_name} program {key!r} needs program_name")
    if not isinstance(value.get("messages"), list):
        raise GeneralICPMessageError(f"{store_name} program {key!r} messages must be a list")
    if not isinstance(value.get("icp_labels"), dict):
        raise GeneralICPMessageError(f"{store_name} program {key!r} icp_labels must be an object")
    return value


def _payload_message_row(
    program_key: str,
    program_name: str,
    icp_label: str,
    value: object,
) -> list[str]:
    if not isinstance(value, dict):
        raise GeneralICPMessageError(f"program {program_key!r} contains a non-object message")
    required = {
        "audience_key", "role_persona", "fedramp_segment",
        "channel", "step_key", "step_index", "delay_hours", "variant_key",
        "subject", "body", "media", "sender_override", "notes",
    }
    if set(value) != required:
        raise GeneralICPMessageError(
            f"program {program_key!r} message fields must exactly equal {sorted(required)!r}"
        )
    return [
        icp_label,
        program_key,
        program_name,
        str(value["audience_key"]),
        str(value["role_persona"]),
        str(value["fedramp_segment"]),
        str(value["channel"]),
        str(value["step_key"]),
        str(value["step_index"]),
        str(value["delay_hours"]),
        str(value["variant_key"]),
        str(value["subject"]),
        str(value["body"]),
        json.dumps(value["media"], ensure_ascii=False),
        str(value["sender_override"]),
        str(value["notes"]),
    ]


def _write_json(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _icp_labels(draft: GeneralMessageProgramDraft) -> dict[str, str]:
    labels: dict[str, str] = {}
    for message in draft.messages:
        previous = labels.setdefault(message.audience_key, message.icp_label)
        if previous != message.icp_label:
            raise GeneralICPMessageError(
                f"audience {message.audience_key!r} has conflicting ICP labels"
            )
    return dict(sorted(labels.items()))
