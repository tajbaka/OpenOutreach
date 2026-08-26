"""Safe Google Sheets adapters for the canonical sales CRM.

The adapters in this module deliberately do not import Django models.  They
accept dictionaries (or lightweight serialized model objects) and return plans
that an orchestration layer can validate before touching either the database or
Sheets.

Safety properties:

* schemas evolve by appending missing managed headers;
* canonical tabs are keyed by stable UUID/PK values, never display names;
* durable Opportunities are incrementally merged and never pruned;
* human-owned fields use a conservative three-way merge;
* derived views update/clear only managed cells and never delete worksheets;
* unknown columns, formulas, formatting, validation, notes, and comments are
  outside the write ranges;
* every mutation can be inspected as a no-write plan first.
"""
from __future__ import annotations

import json
import os
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence, TypeVar

from gspread.exceptions import APIError, WorksheetNotFound
from gspread.utils import ValueRenderOption

from linkedin.exceptions import SheetsError


OPPORTUNITIES_TAB = "Opportunities"
PIPELINE_TAB = "Pipeline"
RECOVERY_TAB = "Recovery"

COL_OPPORTUNITY_ID = "Opportunity ID"
COL_ACCOUNT_ID = "Account ID"
COL_ACCOUNT = "Account"
COL_ACCOUNT_DOMAIN = "Account domain"
COL_MOTION_KEY = "Motion key"
COL_OPPORTUNITY_NAME = "Opportunity name"
COL_CONTACT_LEAD_IDS = "Contact Lead IDs"
COL_OWNER = "Owner"
COL_STAGE = "Stage"
COL_STAGE_ENTERED_AT = "Stage entered at"
COL_SALES_MOTION_STEP = "Sales motion step"
COL_CHAMPION = "Champion Lead ID"
COL_DECISION_MAKER = "Decision Maker Lead ID"
COL_STAKEHOLDERS = "Stakeholder Lead IDs"
COL_NEXT_ACTION = "Next action"
COL_NEXT_ACTION_DUE = "Next action due date"
COL_LAST_MEANINGFUL_ACTIVITY = "Last meaningful activity"
COL_MANUAL_PIN = "Manual pin"
COL_WAITING_UNTIL = "Waiting until"
COL_VALUE = "Value"
COL_CURRENCY = "Currency"
COL_PROBABILITY = "Probability"
COL_CLOSED_WON_AT = "Closed won at"
COL_CLOSED_LOST_AT = "Closed lost at"
COL_CLOSED_LOST_REASON = "Closed lost reason"
COL_SOURCE = "Source"
COL_MEETING_CONTEXT = "Meeting context"
COL_MEETING_CONTEXT_SOURCE = "Meeting context source"
COL_OVERDUE = "Overdue"
COL_ACTION_CATEGORY = "Action category"
COL_INACTIVITY_AGE = "Inactivity age"
COL_RECOVERY_ELIGIBILITY = "Recovery eligibility"
COL_PIPELINE_POSITION = "Pipeline position"
COL_HUMAN_REVISION = "Human revision"
COL_HUMAN_BASELINE = "Human sync baseline"
COL_SOURCE_UPDATED_AT = "Source updated at"
COL_LAST_SYNCED = "Last synced"


OPPORTUNITY_HUMAN_FIELDS = (
    COL_OWNER,
    COL_STAGE,
    COL_SALES_MOTION_STEP,
    COL_CHAMPION,
    COL_DECISION_MAKER,
    COL_STAKEHOLDERS,
    COL_NEXT_ACTION,
    COL_NEXT_ACTION_DUE,
    COL_MANUAL_PIN,
    COL_WAITING_UNTIL,
    COL_VALUE,
    COL_CURRENCY,
    COL_PROBABILITY,
    COL_CLOSED_WON_AT,
    COL_CLOSED_LOST_AT,
    COL_CLOSED_LOST_REASON,
)

OPPORTUNITY_SYSTEM_FIELDS = (
    COL_ACCOUNT_ID,
    COL_ACCOUNT,
    COL_ACCOUNT_DOMAIN,
    COL_MOTION_KEY,
    COL_OPPORTUNITY_NAME,
    COL_CONTACT_LEAD_IDS,
    COL_STAGE_ENTERED_AT,
    COL_LAST_MEANINGFUL_ACTIVITY,
    COL_SOURCE,
    COL_MEETING_CONTEXT,
    COL_MEETING_CONTEXT_SOURCE,
    COL_HUMAN_REVISION,
    COL_SOURCE_UPDATED_AT,
)

OPPORTUNITY_DERIVED_FIELDS = (
    COL_OVERDUE,
    COL_ACTION_CATEGORY,
    COL_INACTIVITY_AGE,
    COL_RECOVERY_ELIGIBILITY,
    COL_PIPELINE_POSITION,
)

OPPORTUNITY_HEADERS = (
    COL_OPPORTUNITY_ID,
    COL_ACCOUNT_ID,
    COL_ACCOUNT,
    COL_ACCOUNT_DOMAIN,
    COL_MOTION_KEY,
    COL_OPPORTUNITY_NAME,
    COL_CONTACT_LEAD_IDS,
    *OPPORTUNITY_HUMAN_FIELDS,
    COL_STAGE_ENTERED_AT,
    COL_LAST_MEANINGFUL_ACTIVITY,
    COL_SOURCE,
    COL_MEETING_CONTEXT,
    COL_MEETING_CONTEXT_SOURCE,
    *OPPORTUNITY_DERIVED_FIELDS,
    COL_HUMAN_REVISION,
    COL_HUMAN_BASELINE,
    COL_SOURCE_UPDATED_AT,
    COL_LAST_SYNCED,
)


PIPELINE_STAGE_COLUMNS = {
    "prospecting": "Prospecting",
    "discovery": "Discovery",
    "demo_planning": "Demo Planning",
    "evaluation": "Evaluation",
    "sandbox_pilot": "Sandbox/Pilot",
    "commercial": "Commercial",
    "procurement_legal": "Procurement/Legal",
    "closed_won": "Closed Won",
    "expansion": "Expansion",
    "closed_lost": "Closed Lost",
}

COL_PIPELINE_CARD = "Pipeline card"

PIPELINE_HEADERS = (
    COL_OPPORTUNITY_ID,
    *PIPELINE_STAGE_COLUMNS.values(),
)

RECOVERY_HEADERS = (
    COL_OPPORTUNITY_ID,
    COL_ACCOUNT,
    COL_OWNER,
    COL_STAGE,
    COL_LAST_MEANINGFUL_ACTIVITY,
    COL_INACTIVITY_AGE,
    COL_NEXT_ACTION,
    COL_NEXT_ACTION_DUE,
    COL_RECOVERY_ELIGIBILITY,
)

COL_ACTION_ID = "Action ID"
COL_LEAD_ID = "Lead ID"
COL_CONTACT = "Contact"
COL_CHANNEL = "Channel"
COL_DRAFT = "Draft"
COL_HANDLED = "Handled"
COL_DISPOSITION = "Disposition"

FOLLOWUP_HUMAN_FIELDS = (
    COL_WAITING_UNTIL,
    COL_CHANNEL,
    COL_DRAFT,
    COL_HANDLED,
    COL_DISPOSITION,
    COL_MANUAL_PIN,
)

FOLLOWUP_HEADERS = (
    COL_ACTION_ID,
    COL_OPPORTUNITY_ID,
    COL_LEAD_ID,
    COL_ACCOUNT,
    COL_CONTACT,
    COL_OWNER,
    COL_ACTION_CATEGORY,
    COL_NEXT_ACTION,
    COL_NEXT_ACTION_DUE,
    COL_WAITING_UNTIL,
    COL_LAST_MEANINGFUL_ACTIVITY,
    COL_CHANNEL,
    COL_DRAFT,
    COL_HANDLED,
    COL_DISPOSITION,
    COL_MANUAL_PIN,
    COL_HUMAN_BASELINE,
)


def sender_followups_tab(sender: str) -> str:
    value = " ".join(str(sender or "").split())
    if not value:
        raise ValueError("sender is required")
    title = f"{value} - Followups"
    if len(title) > 100 or any(character in title for character in "[]:*?/\\"):
        raise ValueError("sender produces an invalid Google Sheets tab title")
    return title


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if value is True:
        return "TRUE"
    if value is False:
        return "FALSE"
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (list, tuple, set)):
        return ", ".join(_stringify(item) for item in value if _stringify(item))
    return str(value)


def _cell(row: Sequence[Any], index: int) -> str:
    return _stringify(row[index]).strip() if index < len(row) else ""


def _column_letter(number_1: int) -> str:
    letters = ""
    while number_1 > 0:
        number_1, remainder = divmod(number_1 - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


_SHEET_READ_RETRY_DELAYS = (5, 10, 20, 30)
_SHEET_READ_VALUE = TypeVar("_SHEET_READ_VALUE")


def _api_error_status(exc: APIError) -> int | None:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if status is not None:
        try:
            return int(status)
        except (TypeError, ValueError):
            pass
    try:
        payload = response.json() if response is not None else {}
        return int((payload.get("error") or {}).get("code"))
    except (AttributeError, TypeError, ValueError):
        return None


def retry_sheet_read(
    operation: Callable[[], _SHEET_READ_VALUE],
    *,
    context: str,
) -> _SHEET_READ_VALUE:
    """Run one idempotent Sheets read with bounded, sanitized retries.

    Google can reject any read shape with the same per-user quota response,
    including metadata and worksheet-list calls.  Keep the retry primitive
    independent of a particular gspread object so every read-only call in the
    refresh cutover and verification path gets identical behavior.  Provider
    response text is deliberately excluded from the terminal error because it
    can contain configured workbook identifiers or user-controlled details.
    """
    for attempt in range(len(_SHEET_READ_RETRY_DELAYS) + 1):
        try:
            return operation()
        except APIError as exc:
            status = _api_error_status(exc)
            retryable = status == 429 or (status is not None and status >= 500)
            if not retryable or attempt >= len(_SHEET_READ_RETRY_DELAYS):
                raise SheetsError(
                    f"{context} after a provider read error"
                ) from exc
            time.sleep(_SHEET_READ_RETRY_DELAYS[attempt])
    raise AssertionError("unreachable Sheet read retry state")


def _formula_values(ws) -> list[list[str]]:
    """Read one tab with bounded quota/transient retries and safe errors."""
    def read_values():
        try:
            return ws.get_all_values(
                value_render_option=ValueRenderOption.formula,
            )
        except TypeError:
            # Lightweight fakes and older gspread-compatible adapters may not
            # accept value_render_option. They still return raw test fixtures.
            return ws.get_all_values()

    values = retry_sheet_read(
        read_values,
        context=f"failed reading tab {getattr(ws, 'title', '')!r}",
    )
    return [list(row) for row in values]


@dataclass(frozen=True)
class DuplicateStableKey:
    key: str
    rows: tuple[int, ...]


@dataclass(frozen=True)
class SheetSnapshot:
    title: str
    sheet_id: int | None
    live_headers: tuple[str, ...]
    headers: tuple[str, ...]
    header_additions: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    key_header: str
    rows_by_key: Mapping[str, tuple[int, ...]]
    duplicate_keys: tuple[DuplicateStableKey, ...]
    unkeyed_nonempty_rows: tuple[int, ...]

    @classmethod
    def read(
        cls,
        ws,
        *,
        required_headers: Sequence[str],
        key_header: str,
    ) -> "SheetSnapshot":
        values = _formula_values(ws)
        live_headers = tuple(_stringify(value).strip() for value in (values[0] if values else []))
        duplicate_headers = [
            header for header, count in Counter(h for h in live_headers if h).items()
            if count > 1
        ]
        if duplicate_headers:
            raise SheetsError(
                f"tab {getattr(ws, 'title', '')!r} has duplicate headers: "
                f"{duplicate_headers}"
            )
        additions = tuple(header for header in required_headers if header not in live_headers)
        headers = (*live_headers, *additions)
        if key_header not in headers:
            raise SheetsError(f"managed key header {key_header!r} is missing")
        key_index = headers.index(key_header)

        normalized_rows: list[tuple[str, ...]] = []
        by_key: dict[str, list[int]] = defaultdict(list)
        unkeyed: list[int] = []
        for row_number, raw in enumerate(values[1:], start=2):
            row = tuple(_stringify(value) for value in raw)
            normalized_rows.append(row)
            key = _cell(row, key_index)
            if key:
                by_key[key].append(row_number)
            elif any(_stringify(value).strip() for value in row):
                unkeyed.append(row_number)
        frozen_by_key = {key: tuple(rows) for key, rows in by_key.items()}
        duplicates = tuple(
            DuplicateStableKey(key, rows)
            for key, rows in sorted(frozen_by_key.items())
            if len(rows) > 1
        )
        return cls(
            title=str(getattr(ws, "title", "")),
            sheet_id=getattr(ws, "id", None),
            live_headers=live_headers,
            headers=tuple(headers),
            header_additions=additions,
            rows=tuple(normalized_rows),
            key_header=key_header,
            rows_by_key=frozen_by_key,
            duplicate_keys=duplicates,
            unkeyed_nonempty_rows=tuple(unkeyed),
        )

    def row_dict(self, row_number: int) -> dict[str, str]:
        row = self.rows[row_number - 2]
        return {
            header: _cell(row, index)
            for index, header in enumerate(self.headers)
            if header
        }


def ensure_additive_headers(
    ws,
    required_headers: Sequence[str],
    *,
    dry_run: bool = False,
) -> tuple[str, ...]:
    """Append missing headers; never insert, move, or rewrite live headers."""
    values = _formula_values(ws)
    live = [_stringify(value).strip() for value in (values[0] if values else [])]
    duplicates = [
        header for header, count in Counter(h for h in live if h).items()
        if count > 1
    ]
    if duplicates:
        raise SheetsError(
            f"tab {getattr(ws, 'title', '')!r} has duplicate headers: {duplicates}"
        )
    missing = tuple(header for header in required_headers if header not in live)
    if dry_run or not missing:
        return missing

    required_width = len(live) + len(missing)
    current_width = int(getattr(ws, "col_count", len(live)) or len(live))
    if current_width < required_width:
        try:
            ws.add_cols(required_width - current_width)
        except APIError as exc:
            raise SheetsError(
                f"failed extending tab {getattr(ws, 'title', '')!r}: {exc}"
            ) from exc
    start = _column_letter(len(live) + 1)
    end = _column_letter(required_width)
    try:
        ws.update(values=[list(missing)], range_name=f"{start}1:{end}1")
    except APIError as exc:
        raise SheetsError(
            f"failed appending headers to {getattr(ws, 'title', '')!r}: {exc}"
        ) from exc
    return missing


@dataclass(frozen=True)
class ManagedTabResult:
    title: str
    exists: bool
    would_create: bool
    header_additions: tuple[str, ...]


def ensure_managed_tab(
    spreadsheet,
    *,
    title: str,
    required_headers: Sequence[str],
    dry_run: bool = False,
) -> tuple[Any | None, ManagedTabResult]:
    """Resolve a managed tab, creating it only on an explicit apply path."""
    try:
        ws = spreadsheet.worksheet(title)
    except WorksheetNotFound:
        result = ManagedTabResult(
            title=title,
            exists=False,
            would_create=True,
            header_additions=tuple(required_headers),
        )
        if dry_run:
            return None, result
        try:
            ws = spreadsheet.add_worksheet(
                title=title,
                rows=1000,
                cols=max(1, len(required_headers)),
            )
        except APIError as exc:
            raise SheetsError(f"failed creating managed tab {title!r}: {exc}") from exc
        ensure_additive_headers(ws, required_headers, dry_run=False)
        return ws, result
    except APIError as exc:
        raise SheetsError(f"failed resolving managed tab {title!r}: {exc}") from exc

    additions = ensure_additive_headers(ws, required_headers, dry_run=dry_run)
    return ws, ManagedTabResult(
        title=title,
        exists=True,
        would_create=False,
        header_additions=additions,
    )


@dataclass(frozen=True)
class CellChange:
    row: int
    column: str
    old_value: str
    new_value: str
    kind: str = "update"
    stable_id: str = ""


@dataclass(frozen=True)
class HumanFieldImport:
    stable_id: str
    field: str
    value: str


@dataclass(frozen=True)
class HumanFieldConflict:
    stable_id: str
    field: str
    baseline: str
    sheet_value: str
    database_value: str


@dataclass(frozen=True)
class HumanBaselineUpdate:
    stable_id: str
    values: Mapping[str, str]


@dataclass
class TabMutationPlan:
    title: str
    key_header: str
    headers: tuple[str, ...]
    # Stable keys observed in the planning snapshot. Durable ledgers can use
    # this to reject key-set changes between planning and publication without
    # treating their intentionally retained, non-payload rows as unknown.
    planned_existing_keys: tuple[str, ...] = ()
    enforce_exact_existing_keys: bool = False
    header_additions: tuple[str, ...] = ()
    appends: list[dict[str, str]] = field(default_factory=list)
    changes: list[CellChange] = field(default_factory=list)
    imports: list[HumanFieldImport] = field(default_factory=list)
    conflicts: list[HumanFieldConflict] = field(default_factory=list)
    baseline_updates: list[HumanBaselineUpdate] = field(default_factory=list)
    duplicate_keys: tuple[DuplicateStableKey, ...] = ()
    unkeyed_nonempty_rows: tuple[int, ...] = ()
    retained_missing_keys: tuple[str, ...] = ()

    def summary(self, *, include_key_values: bool = False) -> dict[str, Any]:
        changed_rows = {change.row for change in self.changes}
        duplicates = []
        for duplicate in self.duplicate_keys:
            item: dict[str, Any] = {"rows": list(duplicate.rows)}
            if include_key_values:
                item["key"] = duplicate.key
            duplicates.append(item)
        return {
            "title": self.title,
            "key_header": self.key_header,
            "header_additions": list(self.header_additions),
            "appended": len(self.appends),
            "updated_rows": len(changed_rows),
            "updated_cells": sum(1 for change in self.changes if change.kind == "update"),
            "cleared_cells": sum(1 for change in self.changes if change.kind == "clear"),
            "imports": len(self.imports),
            "conflicts": len(self.conflicts),
            "baseline_updates": len(self.baseline_updates),
            "duplicate_keys": duplicates,
            "unkeyed_nonempty_rows": list(self.unkeyed_nonempty_rows),
            "retained_missing_keys": len(self.retained_missing_keys),
        }


def _assert_unique_desired(rows: Sequence[Mapping[str, Any]], key_header: str) -> None:
    keys = [_stringify(row.get(key_header)).strip() for row in rows]
    missing = [index + 1 for index, key in enumerate(keys) if not key]
    if missing:
        raise SheetsError(
            f"desired {key_header} is blank in payload row(s) {missing}"
        )
    duplicates = [key for key, count in Counter(keys).items() if count > 1]
    if duplicates:
        raise SheetsError(
            f"desired payload contains {len(duplicates)} duplicate {key_header} value(s)"
        )


def _is_truthy_handled(value: Any) -> bool:
    return _stringify(value).strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
        "handled",
        "sent",
        "complete",
        "completed",
        "done",
        "✓",
    }


def _parse_human_baseline(raw: str, *, row_number: int) -> dict[str, str] | None:
    value = (raw or "").strip()
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise SheetsError(
            f"malformed {COL_HUMAN_BASELINE} JSON at row {row_number}"
        ) from exc
    if not isinstance(parsed, dict):
        raise SheetsError(
            f"{COL_HUMAN_BASELINE} must be an object at row {row_number}"
        )
    return {str(key): _stringify(item) for key, item in parsed.items()}


@dataclass(frozen=True)
class HumanMergeResult:
    merged_values: Mapping[str, str]
    baseline_values: Mapping[str, str]
    sheet_updates: Mapping[str, str]
    imports: tuple[tuple[str, str], ...]
    conflicts: tuple[tuple[str, str, str, str], ...]


_STAGE_SEMANTIC_ALIASES = {
    "prospecting": "prospecting",
    "discovery": "discovery",
    "demo planning": "demo_planning",
    "demo_planning": "demo_planning",
    "evaluation": "evaluation",
    "sandbox pilot": "sandbox_pilot",
    "sandbox/pilot": "sandbox_pilot",
    "sandbox_pilot": "sandbox_pilot",
    "commercial": "commercial",
    "procurement legal": "procurement_legal",
    "procurement/legal": "procurement_legal",
    "procurement_legal": "procurement_legal",
    "closed won": "closed_won",
    "closed_won": "closed_won",
    "expansion": "expansion",
    "closed lost": "closed_lost",
    "closed_lost": "closed_lost",
}


def _human_semantic_value(field: str, value: Any) -> str:
    """Normalize accepted Sheet aliases only for merge comparisons.

    The raw Sheet value is still imported and conflict reports still show the
    raw representations. This prevents a valid import from becoming a false
    conflict merely because the database serializes the same value canonically.
    """
    text = _stringify(value)
    stripped = text.strip()
    normalized = " ".join(stripped.casefold().split())
    if field in {COL_MANUAL_PIN, COL_HANDLED}:
        return "TRUE" if normalized in {
            "1", "true", "yes", "y", "checked", "handled", "sent",
            "complete", "completed", "done", "✓",
        } else "FALSE"
    if field == COL_STAGE:
        return _STAGE_SEMANTIC_ALIASES.get(normalized, normalized)
    if field == COL_OWNER:
        return normalized
    if field == COL_SALES_MOTION_STEP:
        try:
            return str(int(stripped)) if stripped else ""
        except ValueError:
            return stripped
    if field in {COL_CHAMPION, COL_DECISION_MAKER, COL_STAKEHOLDERS}:
        parts = [item for item in re.split(r"[,\n]", stripped) if item.strip()]
        cleaned = [item.strip() for item in parts]
        if cleaned and all(item.isdigit() for item in cleaned):
            return ",".join(str(item) for item in sorted({int(item) for item in cleaned}))
        return ",".join(cleaned)
    if field in {COL_NEXT_ACTION_DUE, COL_WAITING_UNTIL}:
        try:
            return date.fromisoformat(stripped[:10]).isoformat() if stripped else ""
        except ValueError:
            return stripped
    if field in {COL_CLOSED_WON_AT, COL_CLOSED_LOST_AT}:
        try:
            return date.fromisoformat(stripped[:10]).isoformat() if stripped else ""
        except ValueError:
            return stripped
    if field in {COL_VALUE, COL_PROBABILITY}:
        raw_decimal = stripped.replace(",", "").replace("$", "")
        if not raw_decimal:
            return ""
        try:
            decimal_value = Decimal(raw_decimal)
        except InvalidOperation:
            return stripped
        return format(decimal_value.normalize(), "f")
    if field == COL_CURRENCY:
        return stripped.upper()
    if field == COL_DISPOSITION:
        return normalized.replace(" ", "_")
    if field == COL_CHANNEL:
        return normalized
    if field == COL_CLOSED_LOST_REASON:
        return stripped
    return text


def merge_human_fields(
    *,
    sheet_values: Mapping[str, Any],
    database_values: Mapping[str, Any],
    baseline_values: Mapping[str, Any] | None,
    human_fields: Sequence[str] = OPPORTUNITY_HUMAN_FIELDS,
) -> HumanMergeResult:
    """Conservatively three-way merge human-owned fields.

    On first sync there is no baseline: a nonblank Sheet value wins and is
    imported, while a blank Sheet cell accepts the database value.  On later
    syncs, independent changes flow in either direction; divergent changes are
    reported without overwriting the Sheet or advancing that field's baseline.
    A blank database value never erases a nonblank human Sheet value.
    """
    bootstrap = baseline_values is None
    baseline = {
        field: _stringify((baseline_values or {}).get(field))
        for field in human_fields
    }
    sheet = {field: _stringify(sheet_values.get(field)) for field in human_fields}
    database = {
        field: _stringify(database_values.get(field)) for field in human_fields
    }
    merged: dict[str, str] = {}
    next_baseline: dict[str, str] = {}
    updates: dict[str, str] = {}
    imports: list[tuple[str, str]] = []
    conflicts: list[tuple[str, str, str, str]] = []

    for field in human_fields:
        before = baseline[field]
        sheet_value = sheet[field]
        database_value = database[field]
        before_semantic = _human_semantic_value(field, before)
        sheet_semantic = _human_semantic_value(field, sheet_value)
        database_semantic = _human_semantic_value(field, database_value)
        if bootstrap:
            if sheet_value:
                merged[field] = sheet_value
                next_baseline[field] = sheet_value
                if sheet_semantic != database_semantic:
                    imports.append((field, sheet_value))
            else:
                merged[field] = database_value
                next_baseline[field] = database_value
                if database_value:
                    updates[field] = database_value
            continue

        # Human cells are never blanked by an empty DB representation.  This
        # also repairs a lost/partial DB write by re-importing the Sheet value.
        if sheet_value and not database_value:
            merged[field] = sheet_value
            next_baseline[field] = sheet_value
            if sheet_semantic != database_semantic:
                imports.append((field, sheet_value))
            continue

        sheet_changed = sheet_semantic != before_semantic
        database_changed = database_semantic != before_semantic
        if (
            sheet_changed
            and database_changed
            and sheet_semantic != database_semantic
        ):
            merged[field] = sheet_value
            next_baseline[field] = before
            conflicts.append((field, before, sheet_value, database_value))
        elif sheet_changed and database_changed:
            # Same accepted value, different representation (for example
            # "Discovery" versus "discovery" or "100" versus "100.00").
            merged[field] = database_value
            next_baseline[field] = database_value
            if sheet_value != database_value:
                updates[field] = database_value
        elif sheet_changed:
            merged[field] = sheet_value
            next_baseline[field] = sheet_value
            if sheet_semantic != database_semantic:
                imports.append((field, sheet_value))
        elif database_changed:
            merged[field] = database_value
            next_baseline[field] = database_value
            if database_semantic != sheet_semantic:
                updates[field] = database_value
        else:
            merged[field] = sheet_value
            next_baseline[field] = before

    return HumanMergeResult(
        merged_values=merged,
        baseline_values=next_baseline,
        sheet_updates=updates,
        imports=tuple(imports),
        conflicts=tuple(conflicts),
    )


class OpportunitySheetAdapter:
    """Incremental, non-pruning adapter for the canonical Opportunities tab."""

    def __init__(self, ws):
        self.ws = ws

    def plan(
        self,
        desired_rows: Iterable[Mapping[str, Any]],
        *,
        baseline_by_id: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> TabMutationPlan:
        """Plan a durable Opportunity sync.

        ``baseline_by_id`` should normally come from each Opportunity's
        ``sheet_state.published_human_snapshot``.  The per-row JSON column is
        retained as a portable fallback for migrations and disaster recovery.
        """
        desired = [dict(row) for row in desired_rows]
        _assert_unique_desired(desired, COL_OPPORTUNITY_ID)
        desired_ids = {
            _stringify(row.get(COL_OPPORTUNITY_ID)).strip()
            for row in desired
        }
        snapshot = SheetSnapshot.read(
            self.ws,
            required_headers=OPPORTUNITY_HEADERS,
            key_header=COL_OPPORTUNITY_ID,
        )
        plan = TabMutationPlan(
            title=snapshot.title or OPPORTUNITIES_TAB,
            key_header=COL_OPPORTUNITY_ID,
            headers=snapshot.headers,
            planned_existing_keys=tuple(sorted(snapshot.rows_by_key)),
            enforce_exact_existing_keys=True,
            header_additions=snapshot.header_additions,
            duplicate_keys=snapshot.duplicate_keys,
            unkeyed_nonempty_rows=snapshot.unkeyed_nonempty_rows,
            # Opportunities is non-pruning, so these rows remain untouched.
            # The top-level full-CRM refresh treats their stable identities as
            # a fail-closed review condition: a deleted/malformed/unknown key
            # must never strand human edits while a replacement row appends.
            retained_missing_keys=tuple(
                sorted(set(snapshot.rows_by_key) - desired_ids)
            ),
        )
        if snapshot.duplicate_keys:
            raise SheetsError(
                f"{plan.title} contains duplicate {COL_OPPORTUNITY_ID} rows; "
                "resolve them before publishing"
            )

        for raw in desired:
            stable_id = _stringify(raw.get(COL_OPPORTUNITY_ID)).strip()
            existing_rows = snapshot.rows_by_key.get(stable_id, ())
            desired_row = {
                header: _stringify(raw.get(header))
                for header in OPPORTUNITY_HEADERS
            }
            desired_row[COL_OPPORTUNITY_ID] = stable_id
            if not existing_rows:
                baseline = {
                    field: desired_row.get(field, "")
                    for field in OPPORTUNITY_HUMAN_FIELDS
                }
                desired_row[COL_HUMAN_BASELINE] = json.dumps(
                    baseline, sort_keys=True, separators=(",", ":")
                )
                plan.appends.append(desired_row)
                plan.baseline_updates.append(
                    HumanBaselineUpdate(stable_id, baseline)
                )
                continue

            row_number = existing_rows[0]
            current = snapshot.row_dict(row_number)
            if baseline_by_id is not None and stable_id in baseline_by_id:
                baseline = {
                    field: _stringify(baseline_by_id[stable_id].get(field))
                    for field in OPPORTUNITY_HUMAN_FIELDS
                }
            else:
                baseline = _parse_human_baseline(
                    current.get(COL_HUMAN_BASELINE, ""),
                    row_number=row_number,
                )
            merge = merge_human_fields(
                sheet_values=current,
                database_values=desired_row,
                baseline_values=baseline,
            )
            for field, value in merge.imports:
                plan.imports.append(HumanFieldImport(stable_id, field, value))
            for field, before, sheet_value, database_value in merge.conflicts:
                plan.conflicts.append(
                    HumanFieldConflict(
                        stable_id,
                        field,
                        before,
                        sheet_value,
                        database_value,
                    )
                )

            for column in (*OPPORTUNITY_SYSTEM_FIELDS, *OPPORTUNITY_DERIVED_FIELDS):
                if column not in raw:
                    continue
                old = current.get(column, "")
                new = desired_row[column]
                if old != new:
                    plan.changes.append(
                        CellChange(
                            row_number,
                            column,
                            old,
                            new,
                            stable_id=stable_id,
                        )
                    )
            for column, new in merge.sheet_updates.items():
                old = current.get(column, "")
                if old != new:
                    plan.changes.append(
                        CellChange(
                            row_number,
                            column,
                            old,
                            new,
                            stable_id=stable_id,
                        )
                    )

            baseline_json = json.dumps(
                dict(merge.baseline_values),
                sort_keys=True,
                separators=(",", ":"),
            )
            if current.get(COL_HUMAN_BASELINE, "") != baseline_json:
                plan.changes.append(
                    CellChange(
                        row_number,
                        COL_HUMAN_BASELINE,
                        current.get(COL_HUMAN_BASELINE, ""),
                        baseline_json,
                        stable_id=stable_id,
                    )
                )
            plan.baseline_updates.append(
                HumanBaselineUpdate(stable_id, dict(merge.baseline_values))
            )
        return plan

    def apply(
        self,
        plan: TabMutationPlan,
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        if plan.imports and not dry_run:
            raise SheetsError(
                f"refusing to publish {plan.title}: apply its "
                f"{len(plan.imports)} human-field import(s) to the database, "
                "then rebuild and re-plan from fresh database state"
            )
        return apply_tab_plan(
            self.ws,
            plan,
            required_headers=OPPORTUNITY_HEADERS,
            dry_run=dry_run,
        )


class DerivedSheetAdapter:
    """Stable-ID in-place publisher for Pipeline, Recovery, and Followups."""

    def __init__(
        self,
        ws,
        *,
        headers: Sequence[str],
        key_header: str,
        human_fields: Sequence[str] = (),
        human_baseline_header: str | None = None,
        preserve_missing_fields: Sequence[str] = (),
        retain_handled_missing: bool = False,
    ):
        self.ws = ws
        self.headers = tuple(headers)
        self.key_header = key_header
        self.human_fields = tuple(human_fields)
        self.human_baseline_header = human_baseline_header
        self.preserve_missing_fields = frozenset(preserve_missing_fields)
        self.retain_handled_missing = retain_handled_missing
        if self.human_fields and not self.human_baseline_header:
            raise ValueError("human_baseline_header is required with human_fields")
        if self.human_baseline_header and self.human_baseline_header not in self.headers:
            raise ValueError("human_baseline_header must be a managed header")

    def read_human_fields(self) -> tuple[dict[str, str], ...]:
        """Read stable-keyed human cells without mutating the worksheet."""
        if not self.human_fields:
            return ()
        snapshot = SheetSnapshot.read(
            self.ws,
            required_headers=self.headers,
            key_header=self.key_header,
        )
        if snapshot.duplicate_keys:
            raise SheetsError(
                f"{snapshot.title} contains duplicate {self.key_header} rows"
            )
        values = []
        for stable_id, row_numbers in snapshot.rows_by_key.items():
            current = snapshot.row_dict(row_numbers[0])
            values.append({
                self.key_header: stable_id,
                **{
                    column: current.get(column, "")
                    for column in self.human_fields
                },
            })
        return tuple(values)

    def plan(
        self,
        desired_rows: Iterable[Mapping[str, Any]],
        *,
        remove_missing: bool = True,
        baseline_by_id: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> TabMutationPlan:
        desired = [dict(row) for row in desired_rows]
        _assert_unique_desired(desired, self.key_header)
        snapshot = SheetSnapshot.read(
            self.ws,
            required_headers=self.headers,
            key_header=self.key_header,
        )
        plan = TabMutationPlan(
            title=snapshot.title,
            key_header=self.key_header,
            headers=snapshot.headers,
            planned_existing_keys=tuple(sorted(snapshot.rows_by_key)),
            header_additions=snapshot.header_additions,
            duplicate_keys=snapshot.duplicate_keys,
            unkeyed_nonempty_rows=snapshot.unkeyed_nonempty_rows,
        )
        if snapshot.duplicate_keys:
            raise SheetsError(
                f"{snapshot.title} contains duplicate {self.key_header} rows"
            )

        desired_keys: set[str] = set()
        for raw in desired:
            stable_id = _stringify(raw.get(self.key_header)).strip()
            desired_keys.add(stable_id)
            existing_rows = snapshot.rows_by_key.get(stable_id, ())
            desired_row = {
                header: _stringify(raw.get(header))
                for header in self.headers
            }
            desired_row[self.key_header] = stable_id
            if not existing_rows:
                if self.human_fields:
                    baseline = {
                        column: desired_row.get(column, "")
                        for column in self.human_fields
                    }
                    desired_row[self.human_baseline_header] = json.dumps(
                        baseline, sort_keys=True, separators=(",", ":")
                    )
                    plan.baseline_updates.append(
                        HumanBaselineUpdate(stable_id, baseline)
                    )
                plan.appends.append(desired_row)
                continue
            row_number = existing_rows[0]
            current = snapshot.row_dict(row_number)
            if self.human_fields:
                if baseline_by_id is not None and stable_id in baseline_by_id:
                    baseline = {
                        field: _stringify(baseline_by_id[stable_id].get(field))
                        for field in self.human_fields
                    }
                else:
                    baseline = _parse_human_baseline(
                        current.get(self.human_baseline_header, ""),
                        row_number=row_number,
                    )
                # An omitted human field means "not supplied", not blank.
                # Preserve the current database/sheet representation until a
                # caller intentionally includes that field in its payload.
                database_human = {
                    column: (
                        desired_row[column]
                        if column in raw
                        else current.get(column, "")
                    )
                    for column in self.human_fields
                }
                merge = merge_human_fields(
                    sheet_values=current,
                    database_values=database_human,
                    baseline_values=baseline,
                    human_fields=self.human_fields,
                )
                for column, value in merge.imports:
                    plan.imports.append(
                        HumanFieldImport(stable_id, column, value)
                    )
                for column, before, sheet_value, database_value in merge.conflicts:
                    plan.conflicts.append(
                        HumanFieldConflict(
                            stable_id,
                            column,
                            before,
                            sheet_value,
                            database_value,
                        )
                    )
                for column, new in merge.sheet_updates.items():
                    old = current.get(column, "")
                    if old != new:
                        plan.changes.append(
                            CellChange(
                                row_number,
                                column,
                                old,
                                new,
                                stable_id=stable_id,
                            )
                        )
                baseline_json = json.dumps(
                    dict(merge.baseline_values),
                    sort_keys=True,
                    separators=(",", ":"),
                )
                old_baseline = current.get(self.human_baseline_header, "")
                if old_baseline != baseline_json:
                    plan.changes.append(
                        CellChange(
                            row_number,
                            self.human_baseline_header,
                            old_baseline,
                            baseline_json,
                            stable_id=stable_id,
                        )
                    )
                plan.baseline_updates.append(
                    HumanBaselineUpdate(stable_id, dict(merge.baseline_values))
                )
            for column in self.headers:
                if column not in raw and column != self.key_header:
                    continue
                if column in self.human_fields or column == self.human_baseline_header:
                    continue
                old = current.get(column, "")
                new = desired_row[column]
                if old != new:
                    plan.changes.append(
                        CellChange(
                            row_number,
                            column,
                            old,
                            new,
                            stable_id=stable_id,
                        )
                    )

        missing_keys = set(snapshot.rows_by_key) - desired_keys
        if remove_missing:
            for stable_id in sorted(missing_keys):
                row_number = snapshot.rows_by_key[stable_id][0]
                current = snapshot.row_dict(row_number)
                if self.retain_handled_missing and _is_truthy_handled(
                    current.get(COL_HANDLED, "")
                ):
                    plan.retained_missing_keys = (
                        *plan.retained_missing_keys,
                        stable_id,
                    )
                    continue
                for column in self.headers:
                    if column == self.key_header:
                        # Preserve the stable key so a later reappearance is
                        # rewritten in place rather than appended elsewhere.
                        continue
                    if (
                        column in self.preserve_missing_fields
                        or column == self.human_baseline_header
                    ):
                        continue
                    old = current.get(column, "")
                    if old:
                        plan.changes.append(
                            CellChange(
                                row_number,
                                column,
                                old,
                                "",
                                kind="clear",
                                stable_id=stable_id,
                            )
                        )
        else:
            plan.retained_missing_keys = tuple(sorted(missing_keys))
        return plan

    def apply(
        self,
        plan: TabMutationPlan,
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        if plan.imports and not dry_run:
            raise SheetsError(
                f"refusing to publish {plan.title}: apply its "
                f"{len(plan.imports)} human-field import(s) to the database, "
                "then rebuild and re-plan from fresh database state"
            )
        return apply_tab_plan(
            self.ws,
            plan,
            required_headers=self.headers,
            dry_run=dry_run,
        )


def pipeline_stage_row(
    *,
    opportunity_id: Any,
    stage: Any,
    card_summary: Any,
) -> dict[str, str]:
    """Build one stable row for the stage-as-columns Pipeline view.

    The canonical stage is an explicit database input.  Nothing in the Sheet
    adapter infers stage from which visual column happens to contain text.
    """
    stable_id = _stringify(opportunity_id).strip()
    stage_slug = _stringify(stage).strip()
    if not stable_id:
        raise SheetsError(f"pipeline row is missing {COL_OPPORTUNITY_ID}")
    try:
        stage_column = PIPELINE_STAGE_COLUMNS[stage_slug]
    except KeyError as exc:
        raise SheetsError(f"pipeline row has unknown canonical stage {stage_slug!r}") from exc
    row = {header: "" for header in PIPELINE_HEADERS}
    row[COL_OPPORTUNITY_ID] = stable_id
    row[stage_column] = _stringify(card_summary)
    # Carry explicit, non-published source metadata so the specialized
    # adapter can safely re-normalize rows without inferring stage from a
    # visually populated column.
    row[COL_STAGE] = stage_slug
    row[COL_PIPELINE_CARD] = _stringify(card_summary)
    return row


def pipeline_card_summary(row: Mapping[str, Any]) -> str:
    """Render a compact multi-line card from canonical Opportunity fields."""
    title = (
        _stringify(row.get(COL_ACCOUNT)).strip()
        or _stringify(row.get(COL_OPPORTUNITY_NAME)).strip()
        or f"Opportunity {_stringify(row.get(COL_OPPORTUNITY_ID)).strip()}"
    )
    lines = [title]
    details = (
        ("Owner", row.get(COL_OWNER)),
        ("Step", row.get(COL_SALES_MOTION_STEP)),
        ("Next", row.get(COL_NEXT_ACTION)),
        ("Due", row.get(COL_NEXT_ACTION_DUE)),
        ("Probability", row.get(COL_PROBABILITY)),
    )
    for label, value in details:
        rendered = _stringify(value).strip()
        if rendered:
            lines.append(f"{label}: {rendered}")
    return "\n".join(lines)


class PipelineSheetAdapter(DerivedSheetAdapter):
    def plan(
        self,
        desired_rows: Iterable[Mapping[str, Any]],
        *,
        remove_missing: bool = True,
        baseline_by_id: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> TabMutationPlan:
        visual_rows = []
        for raw in desired_rows:
            card = (
                raw.get(COL_PIPELINE_CARD)
                if COL_PIPELINE_CARD in raw
                else pipeline_card_summary(raw)
            )
            visual_rows.append(pipeline_stage_row(
                opportunity_id=raw.get(COL_OPPORTUNITY_ID),
                stage=raw.get(COL_STAGE),
                card_summary=card,
            ))
        return super().plan(
            visual_rows,
            remove_missing=remove_missing,
            baseline_by_id=baseline_by_id,
        )


def pipeline_adapter(ws) -> PipelineSheetAdapter:
    return PipelineSheetAdapter(
        ws,
        headers=PIPELINE_HEADERS,
        key_header=COL_OPPORTUNITY_ID,
    )


def recovery_adapter(ws) -> DerivedSheetAdapter:
    return DerivedSheetAdapter(
        ws,
        headers=RECOVERY_HEADERS,
        key_header=COL_OPPORTUNITY_ID,
    )


def followups_adapter(ws) -> DerivedSheetAdapter:
    return DerivedSheetAdapter(
        ws,
        headers=FOLLOWUP_HEADERS,
        key_header=COL_ACTION_ID,
        human_fields=FOLLOWUP_HUMAN_FIELDS,
        human_baseline_header=COL_HUMAN_BASELINE,
        preserve_missing_fields=FOLLOWUP_HUMAN_FIELDS,
        retain_handled_missing=True,
    )


def apply_tab_plan(
    ws,
    plan: TabMutationPlan,
    *,
    required_headers: Sequence[str],
    dry_run: bool = False,
) -> dict[str, Any]:
    """Apply one precomputed plan without clearing or replacing the worksheet."""
    summary = plan.summary()
    if dry_run:
        return summary
    if plan.conflicts:
        raise SheetsError(
            f"refusing to publish {plan.title}: {len(plan.conflicts)} "
            "human-field conflict(s)"
        )

    # Optimistic preflight: do not apply a stale plan over edits made after
    # planning. Resolve rows by stable ID so harmless row movement is safe.
    before = SheetSnapshot.read(
        ws,
        required_headers=required_headers,
        key_header=plan.key_header,
    )
    if before.duplicate_keys:
        raise SheetsError(
            f"refusing to publish {plan.title}: duplicate {plan.key_header} rows"
        )
    appended_keys = {
        _stringify(row.get(plan.key_header)).strip()
        for row in plan.appends
    }
    appeared = sorted(key for key in appended_keys if key in before.rows_by_key)
    if appeared:
        raise SheetsError(
            f"refusing to publish stale {plan.title} plan: "
            f"{len(appeared)} planned append key(s) now exist"
        )

    # Baselines are committed by the caller immediately after a successful
    # apply. Even when no cell value needs updating, every existing baseline
    # target must therefore still resolve exactly once in this final snapshot.
    # Otherwise a deleted or mutated stable key could advance durable DB state
    # for a row that is no longer the row we planned against.
    baseline_existing_keys = {
        _stringify(update.stable_id).strip()
        for update in plan.baseline_updates
        if _stringify(update.stable_id).strip() not in appended_keys
    }
    unresolved_baseline_keys = [
        key
        for key in baseline_existing_keys
        if len(before.rows_by_key.get(key, ())) != 1
    ]
    if unresolved_baseline_keys:
        raise SheetsError(
            f"refusing to publish stale {plan.title} plan: "
            f"{len(unresolved_baseline_keys)} baseline stable row(s) are "
            "missing or ambiguous"
        )

    if plan.enforce_exact_existing_keys:
        planned_existing = set(plan.planned_existing_keys)
        live_existing = set(before.rows_by_key)
        missing = planned_existing - live_existing
        unknown = live_existing - planned_existing
        if missing or unknown:
            raise SheetsError(
                f"refusing to publish stale {plan.title} plan: durable stable "
                f"key set changed after planning ({len(missing)} missing, "
                f"{len(unknown)} unknown)"
            )
    resolved_changes: list[tuple[CellChange, int]] = []
    for change in plan.changes:
        row_number = change.row
        if change.stable_id:
            rows = before.rows_by_key.get(change.stable_id, ())
            if len(rows) != 1:
                raise SheetsError(
                    f"refusing to publish stale {plan.title} plan: stable row "
                    f"for {change.column!r} is missing or ambiguous"
                )
            row_number = rows[0]
        if row_number < 2 or row_number - 2 >= len(before.rows):
            raise SheetsError(
                f"refusing to publish stale {plan.title} plan: row {row_number} moved"
            )
        current = before.row_dict(row_number).get(change.column, "")
        if current != change.old_value:
            raise SheetsError(
                f"refusing to publish stale {plan.title} plan: "
                f"{change.column!r} changed after planning"
            )
        resolved_changes.append((change, row_number))

    ensure_additive_headers(ws, required_headers, dry_run=False)
    # ``before.headers`` includes planned additive headers and preserves any
    # operator column appended between planning and application.
    headers = list(before.headers)
    index = {header: position for position, header in enumerate(headers)}
    updates = []
    for change, row_number in resolved_changes:
        if change.column not in index:
            raise SheetsError(
                f"planned column {change.column!r} is absent from {plan.title}"
            )
        letter = _column_letter(index[change.column] + 1)
        updates.append({
            "range": f"{letter}{row_number}:{letter}{row_number}",
            "values": [[change.new_value]],
        })
    if updates:
        try:
            ws.batch_update(updates, value_input_option="RAW")
        except APIError as exc:
            raise SheetsError(f"failed updating {plan.title}: {exc}") from exc

    if plan.appends:
        rows = [
            [_stringify(row.get(header)) for header in headers]
            for row in plan.appends
        ]
        # Never use Sheets' values.append table detection here.  Derived views
        # deliberately keep stable IDs on rows whose visible managed cells were
        # cleared.  Google may treat the first such visually blank row as the
        # end of the table and overwrite that durable identity.  Write one
        # explicit rectangle after the final material snapshot row instead.
        start_row = len(before.rows) + 2
        end_row = start_row + len(rows) - 1
        if getattr(ws, "row_count", end_row) < end_row:
            try:
                ws.add_rows(end_row - ws.row_count)
            except APIError as exc:
                raise SheetsError(f"failed expanding {plan.title}: {exc}") from exc
        last_column = _column_letter(len(headers))
        try:
            ws.update(
                values=rows,
                range_name=f"A{start_row}:{last_column}{end_row}",
            )
        except APIError as exc:
            raise SheetsError(f"failed appending to {plan.title}: {exc}") from exc
    return summary


def opportunity_to_sheet_row(
    opportunity: Any,
    *,
    action: Any | None = None,
    meeting_context: str = "",
    meeting_context_source: str = "",
    derived: Mapping[str, Any] | None = None,
    synced_at: datetime | None = None,
) -> dict[str, str]:
    """Serialize the canonical model shape without importing model classes."""

    def get(obj: Any, name: str, default: Any = "") -> Any:
        if obj is None:
            return default
        if isinstance(obj, Mapping):
            return obj.get(name, default)
        return getattr(obj, name, default)

    account = get(opportunity, "account", None)
    owner = get(opportunity, "owner", None)
    contacts_value = get(opportunity, "contacts", ())
    if hasattr(contacts_value, "all"):
        contacts_value = contacts_value.all()
    contacts = list(contacts_value or ())

    lead_ids: list[str] = []
    role_ids: dict[str, list[str]] = defaultdict(list)
    for contact in contacts:
        lead_id = _stringify(get(contact, "lead_id", "")).strip()
        if not lead_id:
            continue
        lead_ids.append(lead_id)
        role_ids[_stringify(get(contact, "role", "other")).strip()].append(lead_id)

    derived_values = dict(derived or {})
    row = {
        COL_OPPORTUNITY_ID: get(opportunity, "id"),
        COL_ACCOUNT_ID: get(account, "id"),
        COL_ACCOUNT: get(account, "name"),
        COL_ACCOUNT_DOMAIN: get(account, "domain"),
        COL_MOTION_KEY: get(opportunity, "motion_key"),
        COL_OPPORTUNITY_NAME: get(opportunity, "name"),
        COL_CONTACT_LEAD_IDS: lead_ids,
        COL_OWNER: get(owner, "handle") or get(owner, "display_name"),
        COL_STAGE: get(opportunity, "stage"),
        COL_STAGE_ENTERED_AT: get(opportunity, "stage_entered_at"),
        COL_SALES_MOTION_STEP: get(opportunity, "sales_motion_step"),
        COL_CHAMPION: role_ids.get("champion", []),
        COL_DECISION_MAKER: role_ids.get("decision_maker", []),
        COL_STAKEHOLDERS: role_ids.get("stakeholder", []),
        COL_NEXT_ACTION: get(action, "description"),
        COL_NEXT_ACTION_DUE: get(action, "due_on"),
        COL_LAST_MEANINGFUL_ACTIVITY: get(
            opportunity, "last_meaningful_activity_at"
        ),
        COL_MANUAL_PIN: get(opportunity, "manual_pin"),
        COL_WAITING_UNTIL: get(action, "waiting_until"),
        COL_VALUE: get(opportunity, "value"),
        COL_CURRENCY: get(opportunity, "currency"),
        COL_PROBABILITY: get(opportunity, "probability"),
        COL_CLOSED_WON_AT: get(opportunity, "closed_won_at"),
        COL_CLOSED_LOST_AT: get(opportunity, "closed_lost_at"),
        COL_CLOSED_LOST_REASON: get(opportunity, "closed_lost_reason"),
        COL_SOURCE: get(opportunity, "source"),
        COL_MEETING_CONTEXT: meeting_context,
        COL_MEETING_CONTEXT_SOURCE: meeting_context_source,
        COL_OVERDUE: derived_values.get(COL_OVERDUE, ""),
        COL_ACTION_CATEGORY: derived_values.get(COL_ACTION_CATEGORY, ""),
        COL_INACTIVITY_AGE: derived_values.get(COL_INACTIVITY_AGE, ""),
        COL_RECOVERY_ELIGIBILITY: derived_values.get(COL_RECOVERY_ELIGIBILITY, ""),
        COL_PIPELINE_POSITION: derived_values.get(COL_PIPELINE_POSITION, ""),
        COL_HUMAN_REVISION: get(opportunity, "human_revision"),
        COL_SOURCE_UPDATED_AT: get(opportunity, "updated_at"),
        COL_LAST_SYNCED: synced_at or datetime.now(timezone.utc),
    }
    return {header: _stringify(value) for header, value in row.items()}


def _read_spreadsheet_metadata(
    spreadsheet,
    *,
    params: Mapping[str, Any],
    context: str,
) -> Mapping[str, Any] | None:
    fetch_metadata = getattr(spreadsheet, "fetch_sheet_metadata", None)
    if not callable(fetch_metadata):
        return None

    def read_metadata():
        try:
            return fetch_metadata(params=dict(params))
        except TypeError:
            return fetch_metadata()

    return retry_sheet_read(read_metadata, context=context)


def _read_worksheets(spreadsheet, *, context: str) -> list[Any]:
    worksheets = retry_sheet_read(
        spreadsheet.worksheets,
        context=context,
    )
    return list(worksheets)


def _inventory_from_captures(
    spreadsheet,
    captures: Sequence[tuple[Any, Sequence[Sequence[Any]]]],
    *,
    stable_keys: Mapping[str, str] | None = None,
    schema_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the structural inventory from already captured formula grids."""
    key_by_title = dict(stable_keys or {})
    structure_by_id: dict[int, Mapping[str, Any]] = {}
    for item in (schema_metadata or {}).get("sheets", []):
        sheet_id = (item.get("properties") or {}).get("sheetId")
        if sheet_id is not None:
            structure_by_id[int(sheet_id)] = item

    tabs = []
    for ws, captured_values in captures:
        values = [list(row) for row in captured_values]
        headers = [_stringify(value).strip() for value in (values[0] if values else [])]
        used_rows = 0
        used_cols = 0
        formula_count = 0
        for row_number, row in enumerate(values, start=1):
            nonblank = [
                index + 1 for index, value in enumerate(row)
                if _stringify(value).strip()
            ]
            if nonblank:
                used_rows = row_number
                used_cols = max(used_cols, max(nonblank))
            formula_count += sum(
                1 for value in row
                if isinstance(value, str) and value.startswith("=")
            )
        duplicate_groups = 0
        duplicate_extra_rows = 0
        key_header = key_by_title.get(str(getattr(ws, "title", "")))
        if key_header and key_header in headers:
            key_index = headers.index(key_header)
            counts = Counter(
                _cell(row, key_index)
                for row in values[1:]
                if _cell(row, key_index)
            )
            duplicate_groups = sum(1 for count in counts.values() if count > 1)
            duplicate_extra_rows = sum(count - 1 for count in counts.values() if count > 1)
        worksheet_id = getattr(ws, "id", None)
        structure = (
            structure_by_id.get(int(worksheet_id), {})
            if worksheet_id is not None
            else {}
        )
        properties = structure.get("properties") or getattr(ws, "_properties", {}) or {}
        grid = properties.get("gridProperties") or {}
        tabs.append({
            "title": str(getattr(ws, "title", "")),
            "sheet_id": worksheet_id,
            "grid_rows": getattr(ws, "row_count", None),
            "grid_cols": getattr(ws, "col_count", None),
            "used_rows": used_rows,
            "used_cols": used_cols,
            "headers": headers,
            "formula_count": formula_count,
            "duplicate_key_groups": duplicate_groups,
            "duplicate_key_extra_rows": duplicate_extra_rows,
            "hidden": bool(properties.get("hidden", False)),
            "frozen_rows": int(grid.get("frozenRowCount", 0) or 0),
            "frozen_columns": int(grid.get("frozenColumnCount", 0) or 0),
            "protected_range_count": len(structure.get("protectedRanges") or []),
            "merged_range_count": len(structure.get("merges") or []),
        })
    return {
        "title": str(getattr(spreadsheet, "title", "")),
        "spreadsheet_id": str(getattr(spreadsheet, "id", "")),
        "tab_count": len(tabs),
        "tabs": tabs,
    }


def inventory_spreadsheet(
    spreadsheet,
    *,
    stable_keys: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Return structural counts only; no row values or PII are emitted."""
    fields = (
        "sheets(properties(sheetId,title,hidden,gridProperties),"
        "protectedRanges(range),merges)"
    )
    schema_metadata = _read_spreadsheet_metadata(
        spreadsheet,
        params={"fields": fields},
        context="failed reading spreadsheet structure",
    )
    worksheets = _read_worksheets(
        spreadsheet,
        context="failed listing spreadsheet tabs for structural inventory",
    )
    captures = [(ws, _formula_values(ws)) for ws in worksheets]
    return _inventory_from_captures(
        spreadsheet,
        captures,
        stable_keys=stable_keys,
        schema_metadata=schema_metadata,
    )


def build_backup_payload(
    spreadsheet,
    *,
    titles: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Capture values, formulas, and workbook schema for local recovery."""
    selected = set(titles or ())
    schema_metadata = _read_spreadsheet_metadata(
        spreadsheet,
        params={"includeGridData": "false"},
        context="failed backing up spreadsheet schema",
    )
    worksheets = _read_worksheets(
        spreadsheet,
        context="failed listing spreadsheet tabs for backup",
    )
    tabs = []
    captures = []
    for ws in worksheets:
        formulas = _formula_values(ws)
        captures.append((ws, formulas))
        if selected and ws.title not in selected:
            continue
        displayed = retry_sheet_read(
            ws.get_all_values,
            context=f"failed backing up displayed values for tab {ws.title!r}",
        )
        tabs.append({
            "title": ws.title,
            "sheet_id": getattr(ws, "id", None),
            "grid_rows": getattr(ws, "row_count", None),
            "grid_cols": getattr(ws, "col_count", None),
            "formula_values": formulas,
            "displayed_values": displayed,
        })
    missing = selected - {tab["title"] for tab in tabs}
    if missing:
        raise SheetsError(f"backup requested missing tab(s): {sorted(missing)}")
    return {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "spreadsheet_id": str(getattr(spreadsheet, "id", "")),
        "spreadsheet_title": str(getattr(spreadsheet, "title", "")),
        "inventory": _inventory_from_captures(
            spreadsheet,
            captures,
            schema_metadata=schema_metadata,
        ),
        "schema_metadata": schema_metadata,
        "tabs": tabs,
    }


def backup_spreadsheet(
    spreadsheet,
    destination_directory: str | Path,
    *,
    titles: Iterable[str] | None = None,
    prefix: str = "crm-sheets-backup",
) -> Path:
    """Write an exclusive timestamped JSON backup and return its path."""
    if not prefix or Path(prefix).name != prefix:
        raise ValueError("backup prefix must be a single filename component")
    destination = Path(destination_directory)
    destination.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    path = destination / f"{prefix}-{stamp}.json"
    payload = build_backup_payload(spreadsheet, titles=titles)
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    except OSError as exc:
        raise SheetsError(f"failed writing Sheets backup {path}: {exc}") from exc
    return path
