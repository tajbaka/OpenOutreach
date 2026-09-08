"""Operator-only review ledger for the General ICP Messages Sheet.

This module is deliberately outside the campaign-message import and runtime
paths.  It records which exact Sheet cell an operator reviewed and the hash of
the content that was present at that time, so later manual edits become stale
instead of silently retaining a reviewed status.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from copy import deepcopy
from pathlib import Path
from typing import Mapping, Sequence

from linkedin.exceptions import GeneralICPMessageError
from linkedin.general_icp_messages import (
    FEDRAMP_REVENUE_INTENTS,
    GENERAL_ICP_MESSAGES_HEADERS,
    GENERAL_ICP_MESSAGES_TAB,
)


GENERAL_ICP_REVIEW_STATUS_PATH = Path(__file__).with_name(
    "general_icp_review_status.json"
)
GENERAL_ICP_REVIEW_STATUS_SCHEMA_VERSION = 1

GENERAL_ICP_REVIEW_FIELDS = {
    "connect_message": "Connect Message",
    "followup_message_1": "Followup Message 1",
    "email_subject_1": "Email Subject 1",
    "email_body_1": "Email Body 1",
    "followup_message_2": "Followup Message 2",
    "email_subject_2": "Email Subject 2",
    "email_body_2": "Email Body 2",
}

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_CHANGE_KINDS = frozenset({"edited", "reviewed_unchanged"})
_ENTRY_KEYS = frozenset({
    "change_kind",
    "content_sha256",
    "last_modified_at",
    "last_modified_date",
    "reviewed_at",
    "reviewed_by",
})


def empty_general_icp_review_status() -> dict[str, object]:
    return {
        "schema_version": GENERAL_ICP_REVIEW_STATUS_SCHEMA_VERSION,
        "worksheet": GENERAL_ICP_MESSAGES_TAB,
        "rows": {},
    }


def canonical_message_cell(value: object) -> str:
    """Match the Sheet parser's stable newline and surrounding-space rules."""
    if value is None:
        return ""
    return str(value).replace("\r\n", "\n").replace("\r", "\n").strip()


def message_cell_hash(value: object) -> str:
    return hashlib.sha256(canonical_message_cell(value).encode("utf-8")).hexdigest()


def load_general_icp_review_status(
    path: Path = GENERAL_ICP_REVIEW_STATUS_PATH,
) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise GeneralICPMessageError(f"review ledger not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise GeneralICPMessageError(
            f"review ledger is not valid JSON: {path}: {exc}"
        ) from exc
    validate_general_icp_review_status(payload)
    return payload


def validate_general_icp_review_status(payload: object) -> None:
    if not isinstance(payload, dict):
        raise GeneralICPMessageError("review ledger must be a JSON object")
    if set(payload) != {"schema_version", "worksheet", "rows"}:
        raise GeneralICPMessageError(
            "review ledger must contain exactly schema_version, worksheet, and rows"
        )
    if payload["schema_version"] != GENERAL_ICP_REVIEW_STATUS_SCHEMA_VERSION:
        raise GeneralICPMessageError(
            "review ledger has an unsupported schema_version"
        )
    if payload["worksheet"] != GENERAL_ICP_MESSAGES_TAB:
        raise GeneralICPMessageError(
            f"review ledger worksheet must be {GENERAL_ICP_MESSAGES_TAB!r}"
        )
    rows = payload["rows"]
    if not isinstance(rows, dict):
        raise GeneralICPMessageError("review ledger rows must be an object")

    for icp_label, fields in rows.items():
        if not isinstance(icp_label, str) or not icp_label.strip():
            raise GeneralICPMessageError("review ledger ICP labels must be nonblank strings")
        if not isinstance(fields, dict) or not fields:
            raise GeneralICPMessageError(
                f"review ledger row {icp_label!r} must contain reviewed fields"
            )
        for field_key, entry in fields.items():
            if field_key not in GENERAL_ICP_REVIEW_FIELDS:
                raise GeneralICPMessageError(
                    f"review ledger row {icp_label!r} has unknown field {field_key!r}"
                )
            _validate_review_entry(icp_label, field_key, entry)


def write_general_icp_review_status(
    payload: Mapping[str, object],
    path: Path = GENERAL_ICP_REVIEW_STATUS_PATH,
) -> None:
    validate_general_icp_review_status(payload)
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(rendered, encoding="utf-8")
    os.replace(temporary, path)


def select_general_icp_labels(
    rows: Sequence[Sequence[object]],
    *,
    exact_labels: Sequence[str] = (),
    cohorts: Sequence[str] = (),
) -> tuple[str, ...]:
    """Resolve exact labels and three-part cohort labels against the live Sheet."""
    sheet_rows = _sheet_rows_by_label(rows)
    selected: set[str] = set()

    for raw_label in exact_labels:
        label = canonical_message_cell(raw_label)
        if label not in sheet_rows:
            raise GeneralICPMessageError(f"review ICP not found in Sheet: {label!r}")
        selected.add(label)

    for raw_cohort in cohorts:
        cohort = canonical_message_cell(raw_cohort)
        if len([part for part in cohort.split("|") if part.strip()]) != 3:
            raise GeneralICPMessageError(
                f"review cohort must use 'Role | Company Size | FedRAMP Stage': {cohort!r}"
            )
        prefix = f"{cohort} | "
        matches = sorted(label for label in sheet_rows if label.startswith(prefix))
        intents = {label.removeprefix(prefix) for label in matches}
        if intents != set(FEDRAMP_REVENUE_INTENTS):
            raise GeneralICPMessageError(
                f"review cohort {cohort!r} must resolve to all four revenue intents; "
                f"got {sorted(intents)!r}"
            )
        selected.update(matches)

    if not selected:
        raise GeneralICPMessageError("at least one --icp or --cohort is required")
    return tuple(sorted(selected))


def mark_general_icp_fields_reviewed(
    rows: Sequence[Sequence[object]],
    ledger: Mapping[str, object],
    *,
    icp_labels: Sequence[str],
    field_keys: Sequence[str],
    reviewed_at: str,
    reviewed_by: str,
    changed: bool,
    modified_at: str | None = None,
    modified_date: str | None = None,
) -> dict[str, object]:
    """Return an updated ledger without writing it."""
    validate_general_icp_review_status(ledger)
    reviewer = canonical_message_cell(reviewed_by)
    if not reviewer:
        raise GeneralICPMessageError("reviewed_by is required")
    _validate_iso_datetime(reviewed_at, "reviewed_at")
    if modified_at:
        _validate_iso_datetime(modified_at, "modified_at")
    if modified_date:
        _validate_iso_date(modified_date, "modified_date")
    if changed and not (modified_at or modified_date):
        raise GeneralICPMessageError(
            "edited review entries require modified_at or modified_date"
        )
    if not changed and (modified_at or modified_date):
        raise GeneralICPMessageError(
            "modification timestamps require changed=True"
        )
    if modified_at and modified_date:
        raise GeneralICPMessageError(
            "use modified_at for an exact time or modified_date for a date-only backfill"
        )

    unknown_fields = sorted(set(field_keys) - set(GENERAL_ICP_REVIEW_FIELDS))
    if unknown_fields:
        raise GeneralICPMessageError(f"unknown review fields: {unknown_fields!r}")
    if not field_keys:
        raise GeneralICPMessageError("at least one review field is required")

    sheet_rows = _sheet_rows_by_label(rows)
    updated = deepcopy(ledger)
    ledger_rows = updated["rows"]
    for icp_label in icp_labels:
        if icp_label not in sheet_rows:
            raise GeneralICPMessageError(f"review ICP not found in Sheet: {icp_label!r}")
        review_fields = ledger_rows.setdefault(icp_label, {})
        for field_key in field_keys:
            header = GENERAL_ICP_REVIEW_FIELDS[field_key]
            previous = review_fields.get(field_key, {})
            entry = {
                "change_kind": "edited" if changed else "reviewed_unchanged",
                "content_sha256": message_cell_hash(sheet_rows[icp_label][header]),
                "last_modified_at": (
                    modified_at if changed else previous.get("last_modified_at")
                ),
                "reviewed_at": reviewed_at,
                "reviewed_by": reviewer,
            }
            last_modified_date = (
                modified_date if changed else previous.get("last_modified_date")
            )
            if last_modified_date:
                entry["last_modified_date"] = last_modified_date
            review_fields[field_key] = entry

    validate_general_icp_review_status(updated)
    return updated


def compare_general_icp_review_status(
    rows: Sequence[Sequence[object]],
    ledger: Mapping[str, object],
) -> dict[str, object]:
    """Compare every tracked hash with the current Sheet cell."""
    validate_general_icp_review_status(ledger)
    sheet_rows = _sheet_rows_by_label(rows)
    current: list[str] = []
    stale: list[str] = []
    missing: list[str] = []
    tracked_keys: set[tuple[str, str]] = set()

    for icp_label, fields in ledger["rows"].items():
        for field_key, entry in fields.items():
            item = f"{icp_label} :: {field_key}"
            tracked_keys.add((icp_label, field_key))
            row = sheet_rows.get(icp_label)
            if row is None:
                missing.append(item)
                continue
            header = GENERAL_ICP_REVIEW_FIELDS[field_key]
            if message_cell_hash(row[header]) == entry["content_sha256"]:
                current.append(item)
            else:
                stale.append(item)

    all_keys = {
        (icp_label, field_key)
        for icp_label in sheet_rows
        for field_key in GENERAL_ICP_REVIEW_FIELDS
    }
    return {
        "sheet_icp_count": len(sheet_rows),
        "sheet_field_count": len(all_keys),
        "tracked_field_count": len(tracked_keys),
        "current_field_count": len(current),
        "stale_field_count": len(stale),
        "missing_field_count": len(missing),
        "untracked_field_count": len(all_keys - tracked_keys),
        "current": sorted(current),
        "stale": sorted(stale),
        "missing": sorted(missing),
    }


def _sheet_rows_by_label(
    rows: Sequence[Sequence[object]],
) -> dict[str, dict[str, str]]:
    if not rows:
        raise GeneralICPMessageError(
            f"{GENERAL_ICP_MESSAGES_TAB} is empty; expected a header row"
        )
    headers = tuple(canonical_message_cell(value) for value in rows[0])
    if headers != GENERAL_ICP_MESSAGES_HEADERS:
        raise GeneralICPMessageError(
            f"{GENERAL_ICP_MESSAGES_TAB} headers must exactly equal "
            f"{list(GENERAL_ICP_MESSAGES_HEADERS)!r}"
        )
    by_label: dict[str, dict[str, str]] = {}
    for row_number, raw_row in enumerate(rows[1:], start=2):
        values = [
            canonical_message_cell(raw_row[index]) if index < len(raw_row) else ""
            for index in range(len(headers))
        ]
        if not any(values):
            continue
        label = values[0]
        if not label:
            raise GeneralICPMessageError(f"row {row_number} ICP is required")
        if label in by_label:
            raise GeneralICPMessageError(f"duplicate Sheet ICP label: {label!r}")
        by_label[label] = dict(zip(headers, values, strict=True))
    return by_label


def _validate_review_entry(icp_label: str, field_key: str, entry: object) -> None:
    context = f"review ledger row {icp_label!r} field {field_key!r}"
    if not isinstance(entry, dict):
        raise GeneralICPMessageError(f"{context} must be an object")
    unknown = set(entry) - _ENTRY_KEYS
    required = {
        "change_kind",
        "content_sha256",
        "last_modified_at",
        "reviewed_at",
        "reviewed_by",
    }
    missing = required - set(entry)
    if unknown or missing:
        raise GeneralICPMessageError(
            f"{context} has unknown keys {sorted(unknown)!r} or missing keys "
            f"{sorted(missing)!r}"
        )
    if entry["change_kind"] not in _CHANGE_KINDS:
        raise GeneralICPMessageError(f"{context} has an invalid change_kind")
    if not isinstance(entry["content_sha256"], str) or not _HASH_RE.fullmatch(
        entry["content_sha256"]
    ):
        raise GeneralICPMessageError(f"{context} content_sha256 must be 64 lowercase hex")
    _validate_iso_datetime(entry["reviewed_at"], f"{context} reviewed_at")
    if not isinstance(entry["reviewed_by"], str) or not entry["reviewed_by"].strip():
        raise GeneralICPMessageError(f"{context} reviewed_by must be nonblank")
    modified_at = entry["last_modified_at"]
    if modified_at is not None:
        _validate_iso_datetime(modified_at, f"{context} last_modified_at")
    modified_date = entry.get("last_modified_date")
    if modified_date is not None:
        _validate_iso_date(modified_date, f"{context} last_modified_date")
    if modified_at is not None and modified_date is not None:
        raise GeneralICPMessageError(
            f"{context} cannot contain both last_modified_at and last_modified_date"
        )
    if entry["change_kind"] == "edited" and modified_at is None and modified_date is None:
        raise GeneralICPMessageError(
            f"{context} edited entries require a modification date"
        )


def _validate_iso_datetime(value: object, field: str) -> None:
    from datetime import datetime

    if not isinstance(value, str) or not value:
        raise GeneralICPMessageError(f"{field} must be a nonblank ISO 8601 datetime")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise GeneralICPMessageError(f"{field} must be an ISO 8601 datetime") from exc
    if parsed.tzinfo is None:
        raise GeneralICPMessageError(f"{field} must include a UTC offset")


def _validate_iso_date(value: object, field: str) -> None:
    from datetime import date

    if not isinstance(value, str) or not value:
        raise GeneralICPMessageError(f"{field} must use YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise GeneralICPMessageError(f"{field} must use YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise GeneralICPMessageError(f"{field} must use YYYY-MM-DD")
