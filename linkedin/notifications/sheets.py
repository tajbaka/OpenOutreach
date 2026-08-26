"""Google Sheets CRM sync via the Sheets API (gspread).

Publishes the durable Lead ledger into a single People tab. Every Lead is
eligible regardless of its current automation Deal state; Deals merely supply
optional status/stage signals. Existing rows are never rebuilt or removed.

Why a single tab instead of Companies/People/Deals like Airtable? Sheets has
no first-class linked records — multi-tab joins would just be VLOOKUPs. The
operator's actual workflow is "scan a People list and triage" so we
denormalize Company name + aggregate Stage onto each Person row.

Idempotent: rows are upserted by stable ``Lead ID`` first, with a canonicalized
``LinkedIn URL`` fallback for legacy rows. The whole sheet is loaded once per
sync and all new rows are indexed immediately, preventing one Lead with two
Deals from being appended twice in a single run.

Don't-downgrade rules from Airtable carry over for Outreach status + Stage:
human edits in the sheet survive the next auto-sync. Notes, AI Notes,
Priority, Primary location, Title — sheet wins on subsequent syncs (they're
not auto-derived; what's in the sheet is more recent than Attio's snapshot).

Operator-managed columns (anything in the sheet not in HEADERS, e.g.
"Apollo Email") are left untouched on every write — addressing is by
column name against the live sheet header row, not by fixed position.

Failure mode: raises SheetsError on auth / API errors. Callers (sync_sheets)
decide whether to skip-and-continue or abort.
"""
from __future__ import annotations

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import quote, unquote, urlsplit

import gspread
from google.oauth2.service_account import Credentials
from gspread.exceptions import APIError, SpreadsheetNotFound, WorksheetNotFound
from gspread.utils import ValueRenderOption

from linkedin.conf import (
    GOOGLE_SHEETS_CREDENTIALS_PATH,
    GOOGLE_SHEETS_ID,
    GOOGLE_SHEETS_TAB_NAME,
)
from linkedin.exceptions import SheetsError
from linkedin.notifications.crm_sheets import retry_sheet_read

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Stage names + ranking. Same semantics as the Airtable module — Stage is
# a company-level rollup of all leads' Deal states. Don't-downgrade
# preserves manual advances (e.g. operator drags a row to "Meeting").
# ----------------------------------------------------------------------
STAGE_PROSPECTING = "Prospecting"
STAGE_QUALIFICATION = "Qualification"
STAGE_MEETING = "Meeting"
STAGE_CLOSING = "Closing"
STAGE_WON = "Won"
STAGE_LOST = "Lost"

PROGRESSION_RANK = {
    STAGE_PROSPECTING:   1,
    STAGE_QUALIFICATION: 2,
    STAGE_MEETING:       3,
    STAGE_CLOSING:       4,
    STAGE_WON:           5,
}


def deal_to_stage(deal) -> str:
    """Map Deal.state to a Stage.

    COMPLETED is the daemon's automation-finished state, not a won deal —
    the stage is derived from engagement (had a meeting > replied > neither).
    Won is human-set only; the auto-mapping never produces it.
    """
    from crm.models import Meeting
    from linkedin.enums import ProfileState

    state = deal.state
    if state == ProfileState.FAILED:
        return STAGE_LOST
    if state == ProfileState.COMPLETED:
        has_meeting = getattr(deal, "has_meeting", None)
        if has_meeting is None:
            has_meeting = Meeting.objects.filter(lead_id=deal.lead_id).exists()
        if has_meeting:
            return STAGE_MEETING
        if deal.last_reply_at is not None:
            return STAGE_QUALIFICATION
        return STAGE_PROSPECTING
    if state == ProfileState.CONNECTED and deal.last_reply_at is not None:
        return STAGE_QUALIFICATION
    return STAGE_PROSPECTING


def aggregate_company_stage(stages: list[str]) -> str:
    """Won wins; else furthest-along active stage; all-Lost → Lost."""
    if not stages:
        return STAGE_PROSPECTING
    if STAGE_WON in stages:
        return STAGE_WON
    active = [s for s in stages if s in PROGRESSION_RANK]
    if not active:
        return STAGE_LOST
    return max(active, key=lambda s: PROGRESSION_RANK[s])


def should_patch_stage(current: str, target: str) -> bool:
    """Don't downgrade. Won always overrides. Lost is overridable."""
    if current == target:
        return False
    if target == STAGE_WON:
        return True
    cur_rank = PROGRESSION_RANK.get(current, 0)
    tgt_rank = PROGRESSION_RANK.get(target, 0)
    if cur_rank > tgt_rank and current != STAGE_LOST:
        return False
    return True


# ----------------------------------------------------------------------
# Per-person Outreach status (single value, dropdown-validated).
# ----------------------------------------------------------------------
STATUS_INVITE_SENT = "Invite Sent"
STATUS_CONNECTED = "Connected"
STATUS_WAITING = "Waiting"
STATUS_REPLIED = "Replied"
STATUS_WANTS_MEETING = "Wants Meeting"
STATUS_MEETING_BOOKED = "Meeting Booked"
STATUS_HAD_MEETING = "Had Meeting"
STATUS_MANUAL_FOLLOWUP = "Manual followup"
STATUS_PROSPECTING_TO_CLOSE = "Prospecting to close"
STATUS_WON = "Won"
STATUS_LOST = "Lost"
STATUS_DONT_SEND = "Don't send"

# MET = "we've already met them" — strictly post-meeting states. Drafts for
# this cohort assume a real meeting happened and lead with a deliverable
# (Loom, doc, repo link) or a forward-looking question rooted in what was
# discussed on the call.
#
# Refinement 2026-05-11 (second pass): previously MET also included
# Wants Meeting / Meeting Booked, but those are pre-meeting states with
# fundamentally different draft strategies (resurface time slots / pre-meeting
# confirm vs. deliverable-first follow-up). Surfacing them under the same
# "🤝 MET" section misled the operator — a row labeled "Met" they hadn't
# actually met yet. They now live in PRE_MEETING_STATUSES → "📅 SCHEDULING".
MET_STATUSES: set[str] = {
    STATUS_HAD_MEETING, STATUS_MANUAL_FOLLOWUP, STATUS_PROSPECTING_TO_CLOSE,
}

# Pre-meeting states — they've agreed to meet but haven't picked a slot,
# OR the meeting is on the calendar but hasn't happened yet. Distinct from
# MET because the draft shape is different (slot pin / pre-meeting confirm
# vs. post-meeting follow-up).
PRE_MEETING_STATUSES: set[str] = {
    STATUS_WANTS_MEETING, STATUS_MEETING_BOOKED,
}


def deal_to_outreach_status(deal) -> str:
    """Map Deal.state to a per-person Outreach status (auto-managed values only).

    COMPLETED is the daemon's automation-finished state (invite accepted +
    rigid follow-up DM sent), NOT a closed-won deal — it is derived from
    engagement signal (had a meeting > replied > neither). Won is a sales
    outcome and is human-set only; the auto-mapping never produces it.
    """
    from crm.models import Meeting
    from linkedin.enums import ProfileState

    state = deal.state
    if state == ProfileState.FAILED:
        return STATUS_LOST
    if state == ProfileState.COMPLETED:
        has_meeting = getattr(deal, "has_meeting", None)
        if has_meeting is None:
            has_meeting = Meeting.objects.filter(lead_id=deal.lead_id).exists()
        if has_meeting:
            return STATUS_HAD_MEETING
        if deal.last_reply_at is not None:
            return STATUS_REPLIED
        return STATUS_CONNECTED
    if state == ProfileState.CONNECTED:
        return STATUS_REPLIED if deal.last_reply_at is not None else STATUS_CONNECTED
    return STATUS_INVITE_SENT


OUTREACH_RANK = {
    STATUS_INVITE_SENT:          1,
    STATUS_CONNECTED:            2,
    STATUS_WAITING:              2.5,  # human-set: we sent something, awaiting reply
    STATUS_REPLIED:              3,
    STATUS_WANTS_MEETING:        4,
    STATUS_MEETING_BOOKED:       5,
    STATUS_HAD_MEETING:          6,
    STATUS_MANUAL_FOLLOWUP:      6.5,  # human-set: needs out-of-band touch
    STATUS_PROSPECTING_TO_CLOSE: 7,
    STATUS_WON:                  8,
    STATUS_DONT_SEND:            9,    # human-set: stop, sticky against auto-syncs
}


def aggregate_person_outreach_status(statuses: Iterable[str]) -> str:
    """Return the most advanced factual Deal-derived status for one Lead.

    A Lead may have one automation Deal per campaign. Choosing the status of
    whichever Deal happens to be iterated last makes the People row unstable.
    Active progress wins over ``Lost``; ``Lost`` is used only when every
    supplied Deal is failed. Human-only values are not produced here.
    """
    values = [value for value in statuses if value]
    if not values:
        return ""
    active = [value for value in values if value in OUTREACH_RANK]
    if active:
        return max(active, key=lambda value: OUTREACH_RANK[value])
    if STATUS_LOST in values:
        return STATUS_LOST
    return ""


def should_patch_outreach_status(current: str, target: str) -> bool:
    if current == target:
        return False
    if target == STATUS_WON:
        return True
    cur_rank = OUTREACH_RANK.get(current, 0)
    tgt_rank = OUTREACH_RANK.get(target, 0)
    if cur_rank > tgt_rank and current != STATUS_LOST:
        return False
    return True


# ----------------------------------------------------------------------
# Priority. High/Medium/Low — empty Attio priority defaults to Low so the
# column is always populated and filterable.
# ----------------------------------------------------------------------
PRIORITY_HIGH = "High"
PRIORITY_MEDIUM = "Medium"
PRIORITY_LOW = "Low"
PRIORITY_DEFAULT = PRIORITY_LOW

PRIORITY_VALUES = [PRIORITY_HIGH, PRIORITY_MEDIUM, PRIORITY_LOW]


# ----------------------------------------------------------------------
# Schema. Column order matters — it's the literal layout in the Sheet.
# Reordering here = reordering the Sheet. Prefer adding new cols at the end.
# ----------------------------------------------------------------------
COL_NAME = "Name"
COL_FIRST_NAME = "First name"
COL_LAST_NAME = "Last name"
COL_COMPANY = "Company"
COL_TITLE = "Title"
COL_LINKEDIN_URL = "LinkedIn URL"
COL_EMAILS = "Email addresses"
COL_OUTREACH_STATUS = "Outreach status"
COL_STAGE = "Stage"
COL_PRIORITY = "Priority"
COL_PRIMARY_LOCATION = "Primary location"
COL_NOTES = "Notes"
COL_AI_NOTES = "AI Notes"
COL_CREATED_AT = "Created at"
COL_LAST_SYNCED = "Last synced"
COL_LEAD_ID = "Lead ID"

HEADERS = [
    COL_NAME,
    COL_FIRST_NAME,
    COL_LAST_NAME,
    COL_COMPANY,
    COL_TITLE,
    COL_LINKEDIN_URL,
    COL_EMAILS,
    COL_OUTREACH_STATUS,
    COL_STAGE,
    COL_PRIORITY,
    COL_PRIMARY_LOCATION,
    COL_AI_NOTES,
    COL_NOTES,
    COL_CREATED_AT,
    COL_LAST_SYNCED,
    # Added at the end so the live People schema evolves additively.  Never
    # insert managed columns into an operator's existing layout.
    COL_LEAD_ID,
]

# These columns are owned by the operator after a row is first created. The
# publisher may read them to carry values forward, but it never overwrites a
# non-new row. Outreach status and Stage are deliberately not listed: they
# are hybrid fields whose explicit advances are protected by the rank rules.
PEOPLE_HUMAN_OWNED_COLUMNS = frozenset({
    COL_TITLE,
    COL_PRIORITY,
    COL_PRIMARY_LOCATION,
    COL_AI_NOTES,
    COL_NOTES,
})

# Column index in the sheet (1-based for A1 notation, 0-based for list indexing).
HEADER_INDEX_0 = {h: i for i, h in enumerate(HEADERS)}
LINKEDIN_URL_COL_0 = HEADER_INDEX_0[COL_LINKEDIN_URL]


def canonical_linkedin_url(value: str) -> str:
    """Return a stable URL representation for People identity fallback.

    Profile URLs commonly arrive with tracking query strings, locale/mobile
    hosts, mixed-case public identifiers, or without a trailing slash. Those
    variants must resolve to the same legacy People row. Non-LinkedIn values
    are left trimmed rather than being guessed into a new identity.
    """
    raw = str(value or "").strip()
    if not raw:
        return ""
    candidate = raw
    if "://" not in candidate and (
        candidate.casefold().startswith("linkedin.com/")
        or candidate.casefold().startswith("www.linkedin.com/")
        or ".linkedin.com/" in candidate.casefold()
    ):
        candidate = f"https://{candidate}"
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return raw

    host = (parsed.hostname or "").strip(".").casefold()
    if not (host == "linkedin.com" or host.endswith(".linkedin.com")):
        return raw

    segments = [unquote(part).strip() for part in parsed.path.split("/") if part]
    if len(segments) >= 2 and segments[0].casefold() == "in":
        public_id = quote(segments[1].casefold(), safe="-._~")
        return f"https://www.linkedin.com/in/{public_id}/"

    # Sales Navigator and older LinkedIn URL forms can contain identifiers
    # whose case should not be altered. We still canonicalize the host,
    # scheme, duplicate slashes, query string, and fragment.
    path = "/".join(quote(part, safe="-._~,:@()") for part in segments)
    return f"https://www.linkedin.com/{path}/" if path else "https://www.linkedin.com/"


def linkedin_identity_key(value: str) -> str:
    """Canonical, case-stable identity key used only for URL matching."""
    canonical = canonical_linkedin_url(value)
    return canonical.casefold() if canonical else ""


# ----------------------------------------------------------------------
# Client + worksheet handles (lazy, cached per process).
# ----------------------------------------------------------------------
_WORKSHEET: gspread.Worksheet | None = None


def _require_config() -> tuple[str, str, str]:
    if not GOOGLE_SHEETS_ID:
        raise SheetsError("GOOGLE_SHEETS_ID is not set in .env")
    if not GOOGLE_SHEETS_CREDENTIALS_PATH:
        raise SheetsError("GOOGLE_SHEETS_CREDENTIALS_PATH is not set in .env")
    return GOOGLE_SHEETS_ID, GOOGLE_SHEETS_CREDENTIALS_PATH, GOOGLE_SHEETS_TAB_NAME


def _read_header_row(ws: gspread.Worksheet) -> list[str]:
    try:
        return list(ws.row_values(1))
    except APIError as e:
        raise SheetsError(f"failed reading header row: {e}") from e


def _append_missing_headers(
    ws: gspread.Worksheet,
    current: list[str],
    missing: list[str],
) -> list[str]:
    """Append managed headers without moving or rewriting existing columns."""
    if not missing:
        return list(current)

    start_col = len(current) + 1
    required_width = len(current) + len(missing)
    current_width = int(getattr(ws, "col_count", len(current)) or len(current))
    if current_width < required_width:
        try:
            ws.add_cols(required_width - current_width)
        except APIError as e:
            raise SheetsError(
                f"failed extending People tab for managed headers {missing}: {e}"
            ) from e

    start = _col_letter(start_col)
    end = _col_letter(required_width)
    try:
        ws.update(values=[missing], range_name=f"{start}1:{end}1")
    except APIError as e:
        raise SheetsError(f"failed appending People headers {missing}: {e}") from e
    return [*current, *missing]


def get_worksheet(*, apply_schema: bool = True) -> gspread.Worksheet:
    """Return the People worksheet and optionally apply additive schema changes.

    ``apply_schema=False`` is the read-only planning path: it never creates a
    tab, writes headers, or changes formatting.
    """
    global _WORKSHEET
    if _WORKSHEET is None:
        sheet_id, creds_path, tab_name = _require_config()
        scope = (
            "https://www.googleapis.com/auth/spreadsheets"
            if apply_schema
            else "https://www.googleapis.com/auth/spreadsheets.readonly"
        )
        try:
            creds = Credentials.from_service_account_file(
                creds_path,
                scopes=[scope],
            )
            gc = gspread.authorize(creds)
            sh = gc.open_by_key(sheet_id)
        except FileNotFoundError as e:
            raise SheetsError(f"service account JSON not found at {creds_path}") from e
        except SpreadsheetNotFound as e:
            raise SheetsError(
                f"sheet {sheet_id} not found or not shared with the service account"
            ) from e
        except APIError as e:
            raise SheetsError(f"sheets API error opening {sheet_id}: {e}") from e

        try:
            ws = sh.worksheet(tab_name)
        except WorksheetNotFound as e:
            if not apply_schema:
                raise SheetsError(
                    f"People tab '{tab_name}' does not exist; dry-run would create it"
                ) from e
            ws = sh.add_worksheet(title=tab_name, rows=1000, cols=len(HEADERS))
        # Do not cache the read-only dry-run client as the process-wide write
        # client. A later explicit apply in the same process must reauthorize
        # with the write scope.
        if apply_schema:
            _WORKSHEET = ws
    else:
        ws = _WORKSHEET
    first_row = _read_header_row(ws)
    missing = [h for h in HEADERS if h not in first_row]
    if apply_schema:
        if not first_row:
            try:
                ws.update(values=[HEADERS], range_name="A1")
            except APIError as e:
                raise SheetsError(f"failed writing People headers: {e}") from e
            first_row = list(HEADERS)
        else:
            first_row = _append_missing_headers(ws, first_row, missing)

    return ws


def reset_client_cache() -> None:
    """Drop the cached worksheet handle. Useful in tests."""
    global _WORKSHEET
    _WORKSHEET = None


# ----------------------------------------------------------------------
# Row I/O — load all rows once per sync, then upsert by LinkedIn URL.
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class DuplicateSheetKey:
    column: str
    value: str
    row_numbers: tuple[int, ...]


@dataclass(frozen=True)
class PeopleLedgerRowSnapshot:
    """Preservation evidence for one preexisting material People row."""

    row_number: int
    lead_id: str
    linkedin_url: str
    identity: str
    protected_cells: tuple[tuple[str, str], ...]
    formula_cells: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class PeopleLedgerSnapshot:
    """Strong, non-destructive People before/after preservation snapshot."""

    headers: tuple[str, ...]
    row_count: int
    rows: tuple[PeopleLedgerRowSnapshot, ...]
    row_order: tuple[str, ...]
    lead_ids: tuple[str, ...]
    linkedin_urls: tuple[str, ...]
    url_multiplicity: tuple[tuple[str, int], ...]
    operator_headers: tuple[str, ...]
    duplicate_keys: tuple[DuplicateSheetKey, ...]

    def as_dict(self) -> dict[str, Any]:
        """Return aggregate evidence without leaking contact identifiers."""
        return {
            "row_count": self.row_count,
            "headers": list(self.headers),
            "operator_headers": list(self.operator_headers),
            "lead_ids": len(self.lead_ids),
            "linkedin_urls": len(self.linkedin_urls),
            "duplicate_keys": len(self.duplicate_keys),
            "protected_cells": sum(len(row.protected_cells) for row in self.rows),
            "formula_cells": sum(len(row.formula_cells) for row in self.rows),
        }


@dataclass(frozen=True)
class PeoplePreservationVerification:
    rows_before: int
    rows_after: int
    rows_preserved: int
    headers_preserved: int
    protected_cells_preserved: int
    formulas_preserved: int

    def as_dict(self) -> dict[str, int | bool]:
        return {
            "verified": True,
            "rows_before": self.rows_before,
            "rows_after": self.rows_after,
            "rows_preserved": self.rows_preserved,
            "headers_preserved": self.headers_preserved,
            "protected_cells_preserved": self.protected_cells_preserved,
            "formulas_preserved": self.formulas_preserved,
        }


def _snapshot_duplicate_keys(
    rows: Iterable[PeopleLedgerRowSnapshot],
) -> tuple[DuplicateSheetKey, ...]:
    indexes: dict[str, dict[str, list[int]]] = {
        COL_LEAD_ID: defaultdict(list),
        COL_LINKEDIN_URL: defaultdict(list),
    }
    for row in rows:
        if row.lead_id:
            indexes[COL_LEAD_ID][row.lead_id].append(row.row_number)
        if row.linkedin_url:
            indexes[COL_LINKEDIN_URL][row.linkedin_url].append(row.row_number)
    duplicates = [
        DuplicateSheetKey(column, value, tuple(row_numbers))
        for column, values in indexes.items()
        for value, row_numbers in values.items()
        if len(row_numbers) > 1
    ]
    return tuple(sorted(duplicates, key=lambda item: (item.column, item.row_numbers)))


def capture_people_preservation_snapshot(
    ws: gspread.Worksheet | None = None,
    *,
    values: list[list[Any]] | None = None,
) -> PeopleLedgerSnapshot:
    """Capture row order, stable keys, formulas, and operator-owned values.

    Formula rendering is requested explicitly because the default Sheets API
    response contains calculated display values. Comments and formatting are
    not value-grid data; the publisher preserves them structurally by issuing
    only owned-cell value writes and never clearing or rewriting a row.

    ``values`` is an injection point for tests and offline backup verification.
    """
    worksheet = ws
    if values is None:
        worksheet = worksheet or get_worksheet(apply_schema=False)
        values = retry_sheet_read(
            lambda: worksheet.get_all_values(
                value_render_option=ValueRenderOption.formula,
            ),
            context="failed reading People preservation snapshot",
        )

    rows = [list(row) for row in (values or [])]
    headers = tuple(str(value) for value in (rows[0] if rows else []))
    duplicates = [
        header
        for header, count in Counter(header for header in headers if header).items()
        if count > 1
    ]
    if duplicates:
        raise SheetsError(f"People has duplicate headers: {duplicates}")
    header_index = {header: index for index, header in enumerate(headers)}
    if COL_LINKEDIN_URL not in header_index and COL_LEAD_ID not in header_index:
        raise SheetsError(
            f"People must contain {COL_LEAD_ID} or {COL_LINKEDIN_URL}"
        )

    operator_headers = tuple(header for header in headers if header not in HEADERS)
    protected_headers = tuple(
        header
        for header in headers
        if header in PEOPLE_HUMAN_OWNED_COLUMNS or header in operator_headers
    )
    snapshots: list[PeopleLedgerRowSnapshot] = []
    for row_number, raw_row in enumerate(rows[1:], start=2):
        row = [str(value) if value is not None else "" for value in raw_row]
        if not any(value.strip() for value in row):
            continue

        def cell(column: str) -> str:
            index = header_index.get(column)
            return row[index] if index is not None and index < len(row) else ""

        lead_id = cell(COL_LEAD_ID).strip()
        linkedin_url = linkedin_identity_key(cell(COL_LINKEDIN_URL))
        identity = (
            f"lead:{lead_id}"
            if lead_id
            else f"url:{linkedin_url}"
            if linkedin_url
            else f"row:{row_number}"
        )
        protected_cells = tuple(
            (header, cell(header))
            for header in protected_headers
        )
        formula_cells = tuple(
            (header, row[index])
            for index, header in enumerate(headers)
            if index < len(row) and row[index].startswith("=")
        )
        snapshots.append(
            PeopleLedgerRowSnapshot(
                row_number=row_number,
                lead_id=lead_id,
                linkedin_url=linkedin_url,
                identity=identity,
                protected_cells=protected_cells,
                formula_cells=formula_cells,
            )
        )

    snapshot_rows = tuple(snapshots)
    url_counts = Counter(row.linkedin_url for row in snapshot_rows if row.linkedin_url)
    return PeopleLedgerSnapshot(
        headers=headers,
        row_count=len(snapshot_rows),
        rows=snapshot_rows,
        row_order=tuple(row.identity for row in snapshot_rows),
        lead_ids=tuple(row.lead_id for row in snapshot_rows if row.lead_id),
        linkedin_urls=tuple(
            row.linkedin_url for row in snapshot_rows if row.linkedin_url
        ),
        url_multiplicity=tuple(sorted(url_counts.items())),
        operator_headers=operator_headers,
        duplicate_keys=_snapshot_duplicate_keys(snapshot_rows),
    )


def verify_people_preserved(
    before: PeopleLedgerSnapshot,
    after: PeopleLedgerSnapshot,
) -> PeoplePreservationVerification:
    """Fail closed unless every preexisting People row/cell stayed in place."""
    if after.row_count < before.row_count:
        raise SheetsError("People row count decreased")
    if after.headers[:len(before.headers)] != before.headers:
        raise SheetsError("People columns disappeared or were reordered")

    after_by_number = {row.row_number: row for row in after.rows}
    protected_count = formula_count = 0
    for expected in before.rows:
        actual = after_by_number.get(expected.row_number)
        if actual is None:
            raise SheetsError(
                f"People row {expected.row_number} disappeared or moved"
            )
        if expected.lead_id and actual.lead_id != expected.lead_id:
            raise SheetsError(
                f"People Lead ID changed at row {expected.row_number}"
            )
        if expected.linkedin_url and actual.linkedin_url != expected.linkedin_url:
            raise SheetsError(
                f"People LinkedIn URL identity changed at row {expected.row_number}"
            )

        actual_protected = dict(actual.protected_cells)
        for column, value in expected.protected_cells:
            if actual_protected.get(column) != value:
                raise SheetsError(
                    f"People protected cell changed at row {expected.row_number}, "
                    f"column {column}"
                )
            protected_count += 1

        actual_formulas = dict(actual.formula_cells)
        for column, value in expected.formula_cells:
            if actual_formulas.get(column) != value:
                raise SheetsError(
                    f"People formula changed at row {expected.row_number}, "
                    f"column {column}"
                )
            formula_count += 1

    before_ids = Counter(before.lead_ids)
    after_ids = Counter(after.lead_ids)
    if any(after_ids[value] < count for value, count in before_ids.items()):
        raise SheetsError("one or more preexisting People Lead IDs disappeared")
    before_urls = dict(before.url_multiplicity)
    after_urls = dict(after.url_multiplicity)
    if any(after_urls.get(value, 0) < count for value, count in before_urls.items()):
        raise SheetsError("one or more preexisting People URLs disappeared")

    return PeoplePreservationVerification(
        rows_before=before.row_count,
        rows_after=after.row_count,
        rows_preserved=before.row_count,
        headers_preserved=len(before.headers),
        protected_cells_preserved=protected_count,
        formulas_preserved=formula_count,
    )


@dataclass(frozen=True)
class PeopleSheetPlan:
    header_additions: tuple[str, ...]
    duplicate_keys: tuple[DuplicateSheetKey, ...]
    appended_rows: int
    updated_rows: int
    updated_cells: int
    changed_columns: tuple[tuple[str, int], ...]

    def as_dict(self, *, include_key_values: bool = False) -> dict[str, Any]:
        """Return a console/JSON-safe dry-run summary.

        Key values are omitted by default so routine telemetry does not print
        LinkedIn URLs. Row numbers are enough to resolve live duplicates.
        """
        duplicates = []
        for duplicate in self.duplicate_keys:
            item: dict[str, Any] = {
                "column": duplicate.column,
                "rows": list(duplicate.row_numbers),
            }
            if include_key_values:
                item["value"] = duplicate.value
            duplicates.append(item)
        return {
            "header_additions": list(self.header_additions),
            "duplicate_keys": duplicates,
            "appended": self.appended_rows,
            "updated": self.updated_rows,
            "updated_cells": self.updated_cells,
            "changed_columns": dict(self.changed_columns),
        }


class SheetIndex:
    """In-memory People snapshot with duplicate-safe stable identity.

    Existing rows resolve by ``Lead ID`` first and canonical LinkedIn URL as a
    bootstrap fallback. Pending appends are indexed immediately, so two Deals
    for one Lead in the same run update one staged row rather than append two.
    Existing-row writes are cell-owned: only managed cells that changed are
    sent to Sheets, preserving operator columns, formulas, validation, notes,
    and formatting elsewhere in the row.
    """

    def __init__(
        self,
        ws: gspread.Worksheet,
        rows: list[list[str]],
        actual_headers: list[str] | None = None,
        *,
        header_additions: Iterable[str] = (),
    ):
        self.ws = ws
        if actual_headers is not None:
            self.actual_headers = [str(value) for value in actual_headers]
        elif rows:
            self.actual_headers = list(rows[0])
        else:
            self.actual_headers = list(HEADERS)
        duplicate_headers = [
            header
            for header, count in Counter(h for h in self.actual_headers if h).items()
            if count > 1
        ]
        if duplicate_headers:
            raise SheetsError(f"sheet has duplicate headers: {duplicate_headers}")
        self.actual_index_0 = {h: i for i, h in enumerate(self.actual_headers)}
        missing = [h for h in HEADERS if h not in self.actual_index_0]
        if missing:
            raise SheetsError(f"sheet header missing managed columns: {missing}")

        self.rows = (
            [
                ["" if value is None else str(value) for value in row]
                for row in rows
            ]
            if rows
            else [list(self.actual_headers)]
        )
        self.rows[0] = list(self.actual_headers)
        self.url_col_0 = self.actual_index_0[COL_LINKEDIN_URL]
        self.lead_id_col_0 = self.actual_index_0[COL_LEAD_ID]
        self.header_additions = tuple(header_additions)

        self.url_to_row_indices: dict[str, list[int]] = defaultdict(list)
        self.lead_id_to_row_indices: dict[str, list[int]] = defaultdict(list)
        for row_idx, row in enumerate(self.rows[1:], start=2):
            self._index_identity(row_idx, row)
        # Legacy convenience mapping remains available, but only for keys that
        # are unambiguous. Duplicate keys are never silently last-row-wins.
        self.url_to_row_idx: dict[str, int] = {}
        self._refresh_unique_url_index()

        self._pending_appends: list[list[str]] = []
        self._pending_append_index_by_row: dict[int, int] = {}
        self._pending_update_by_cell: dict[tuple[int, str], dict[str, Any]] = {}
        # Keep the first formula-rendered live value seen for each existing
        # cell. Multiple source rows can coalesce into one People update during
        # a run; replacing this expectation with an intermediate staged value
        # would make the final optimistic preflight reject our own plan.
        self._pending_expected_by_cell: dict[tuple[int, str], str] = {}
        self._pending_update_rows: set[int] = set()
        self._changed_columns: Counter[str] = Counter()

    @classmethod
    def load(cls, *, apply_schema: bool = True) -> "SheetIndex":
        ws = get_worksheet(apply_schema=apply_schema)
        try:
            # Preserve formulas as formulas. Cell-owned writes then leave
            # every existing formula byte-for-byte intact.
            values = ws.get_all_values(
                value_render_option=ValueRenderOption.formula,
            )
        except APIError as e:
            raise SheetsError(f"failed loading sheet: {e}") from e

        live_headers = list(values[0]) if values else []
        additions = [h for h in HEADERS if h not in live_headers]
        effective_headers = [*live_headers, *additions]
        if not effective_headers:
            effective_headers = list(HEADERS)
            additions = list(HEADERS)
        effective_rows = [effective_headers]
        if values:
            effective_rows.extend(values[1:])
        return cls(
            ws,
            effective_rows,
            actual_headers=effective_headers,
            header_additions=additions if not apply_schema else (),
        )

    @property
    def _pending_updates(self) -> list[dict[str, Any]]:
        """Compatibility/debug view of the cell-owned pending writes."""
        return list(self._pending_update_by_cell.values())

    @property
    def material_row_count(self) -> int:
        """Count non-empty data rows without treating planned appends as deletes."""
        return sum(
            1
            for row in self.rows[1:]
            if any(str(value).strip() for value in row)
        )

    @property
    def duplicate_keys(self) -> tuple[DuplicateSheetKey, ...]:
        out: list[DuplicateSheetKey] = []
        for column, mapping in (
            (COL_LEAD_ID, self.lead_id_to_row_indices),
            (COL_LINKEDIN_URL, self.url_to_row_indices),
        ):
            for value, row_numbers in mapping.items():
                if len(row_numbers) > 1:
                    out.append(
                        DuplicateSheetKey(column, value, tuple(sorted(row_numbers)))
                    )
        return tuple(sorted(out, key=lambda item: (item.column, item.row_numbers)))

    def plan(self) -> PeopleSheetPlan:
        return PeopleSheetPlan(
            header_additions=self.header_additions,
            duplicate_keys=self.duplicate_keys,
            appended_rows=len(self._pending_appends),
            updated_rows=len(self._pending_update_rows),
            updated_cells=len(self._pending_update_by_cell),
            changed_columns=tuple(sorted(self._changed_columns.items())),
        )

    def _cell(self, row: list[str], column_0: int) -> str:
        value = row[column_0] if column_0 < len(row) else ""
        return ("" if value is None else str(value)).strip()

    def _index_identity(self, row_idx: int, row: list[str]) -> None:
        url = linkedin_identity_key(self._cell(row, self.url_col_0))
        lead_id = self._cell(row, self.lead_id_col_0)
        if url:
            self.url_to_row_indices[url].append(row_idx)
        if lead_id:
            self.lead_id_to_row_indices[lead_id].append(row_idx)

    def _unindex_identity(self, row_idx: int, row: list[str]) -> None:
        for mapping, value in (
            (
                self.url_to_row_indices,
                linkedin_identity_key(self._cell(row, self.url_col_0)),
            ),
            (self.lead_id_to_row_indices, self._cell(row, self.lead_id_col_0)),
        ):
            if not value:
                continue
            remaining = [idx for idx in mapping.get(value, []) if idx != row_idx]
            if remaining:
                mapping[value] = remaining
            else:
                mapping.pop(value, None)

    def _refresh_unique_url_index(self) -> None:
        self.url_to_row_idx = {
            value: row_numbers[0]
            for value, row_numbers in self.url_to_row_indices.items()
            if len(row_numbers) == 1
        }

    def _resolve_row_idx(self, linkedin_url: str, lead_id: str = "") -> int | None:
        url = linkedin_identity_key(linkedin_url)
        stable_id = (lead_id or "").strip()
        url_rows = list(self.url_to_row_indices.get(url, [])) if url else []
        id_rows = list(self.lead_id_to_row_indices.get(stable_id, [])) if stable_id else []

        if len(id_rows) > 1:
            raise SheetsError(
                f"duplicate {COL_LEAD_ID} in People rows {sorted(id_rows)}"
            )
        if id_rows:
            resolved = id_rows[0]
            if len(url_rows) == 1 and url_rows[0] != resolved:
                raise SheetsError(
                    f"People identity conflict: {COL_LEAD_ID} row {resolved} "
                    f"but {COL_LINKEDIN_URL} row {url_rows[0]}"
                )
            # A duplicated legacy URL is still safe when exactly one row has
            # this stable Lead ID; the stable ID is authoritative.
            return resolved

        if len(url_rows) > 1:
            raise SheetsError(
                f"duplicate {COL_LINKEDIN_URL} in People rows {sorted(url_rows)}"
            )
        if not url_rows:
            return None

        resolved = url_rows[0]
        row = self.rows[resolved - 1]
        existing_id = self._cell(row, self.lead_id_col_0)
        if stable_id and existing_id and existing_id != stable_id:
            raise SheetsError(
                f"People identity conflict at row {resolved}: existing "
                f"{COL_LEAD_ID} does not match payload"
            )
        return resolved

    def get_row(
        self,
        linkedin_url: str,
        *,
        lead_id: str | int | None = None,
    ) -> dict[str, str] | None:
        """Return one unambiguous managed row, preferring stable Lead ID."""
        idx = self._resolve_row_idx(linkedin_url, str(lead_id or ""))
        if idx is None:
            return None
        row = self.rows[idx - 1]
        return {
            header: row[pos] if pos < len(row) else ""
            for header, pos in self.actual_index_0.items()
            if header in HEADERS
        }

    def identity_candidate_rows(
        self,
        linkedin_url: str = "",
        *,
        lead_id: str | int | None = None,
    ) -> tuple[int, ...]:
        """Return structural row candidates without resolving ambiguity.

        This is reporting-only: callers can distinguish an already represented
        ambiguous legacy identity from a genuinely omitted Lead without ever
        choosing or mutating one of the candidate rows.
        """
        stable_id = str(lead_id or "").strip()
        url = linkedin_identity_key(linkedin_url)
        rows = set(self.lead_id_to_row_indices.get(stable_id, ())) if stable_id else set()
        if url:
            rows.update(self.url_to_row_indices.get(url, ()))
        return tuple(sorted(rows))

    def is_identity_represented(
        self,
        linkedin_url: str = "",
        *,
        lead_id: str | int | None = None,
    ) -> bool:
        """Return true only for an exact ID or unclaimed legacy URL rows."""
        stable_id = str(lead_id or "").strip()
        rows = self.identity_candidate_rows(
            linkedin_url,
            lead_id=stable_id,
        )
        if not rows:
            return False
        existing_ids = {
            self._cell(self.rows[row_number - 1], self.lead_id_col_0)
            for row_number in rows
        }
        if stable_id and stable_id in existing_ids:
            return True
        # Before Lead IDs were introduced, duplicate canonical URLs could
        # represent one known Lead without a safe row to choose. A row already
        # claimed by another stable ID is a conflict, not representation.
        return bool(stable_id and existing_ids == {""})

    def _schedule_cell_update(
        self,
        *,
        row_idx: int,
        column: str,
        value: str,
        expected_value: str,
    ) -> None:
        col_0 = self.actual_index_0[column]
        letter = _col_letter(col_0 + 1)
        key = (row_idx, column)
        self._pending_expected_by_cell.setdefault(key, expected_value)
        self._pending_update_by_cell[key] = {
            "range": f"{letter}{row_idx}:{letter}{row_idx}",
            "values": [[value]],
        }
        self._pending_update_rows.add(row_idx)

    def upsert_row(self, payload: dict[str, str]) -> tuple[bool, list[str]]:
        """Stage an append or cell-owned update for one People identity."""
        url = canonical_linkedin_url(payload.get(COL_LINKEDIN_URL) or "")
        lead_id = (payload.get(COL_LEAD_ID) or "").strip()
        if not lead_id and not url:
            raise SheetsError(
                f"row payload missing both {COL_LEAD_ID} and {COL_LINKEDIN_URL}"
            )
        if COL_LINKEDIN_URL in payload:
            payload = dict(payload)
            payload[COL_LINKEDIN_URL] = url

        row_idx = self._resolve_row_idx(url, lead_id)
        if row_idx is None:
            if not lead_id:
                raise SheetsError(f"new People row missing stable {COL_LEAD_ID}")
            new_row = ["" for _ in self.actual_headers]
            changed = []
            for column in HEADERS:
                if column not in payload:
                    continue
                new_row[self.actual_index_0[column]] = payload.get(column) or ""
                changed.append(column)
            self.rows.append(new_row)
            row_idx = len(self.rows)
            self._pending_append_index_by_row[row_idx] = len(self._pending_appends)
            self._pending_appends.append(new_row)
            self._index_identity(row_idx, new_row)
            self._refresh_unique_url_index()
            self._changed_columns.update(changed)
            return True, changed

        existing_row = self.rows[row_idx - 1]
        new_row = [
            existing_row[i] if i < len(existing_row) else ""
            for i in range(len(self.actual_headers))
        ]
        existing = {
            header: new_row[pos]
            for header, pos in self.actual_index_0.items()
            if header in HEADERS
        }
        changed: list[str] = []
        is_pending_append = row_idx in self._pending_append_index_by_row

        for column in (header for header in HEADERS if header != COL_LAST_SYNCED):
            pos = self.actual_index_0[column]
            current = existing.get(column, "") or ""
            target = payload.get(column, current) or ""
            should_write = False
            if current.startswith("="):
                # Google formula rendering is used when loading the index, so
                # this protects formulas even inside otherwise managed cells.
                should_write = False
            elif column in PEOPLE_HUMAN_OWNED_COLUMNS:
                # A row staged during this run has never been operator-owned;
                # coalesce duplicate inputs before the one append. Once a row
                # preexists in Sheets these cells are immutable to publishing.
                should_write = is_pending_append and target != current
            elif column == COL_OUTREACH_STATUS:
                should_write = should_patch_outreach_status(current, target)
            elif column == COL_STAGE:
                should_write = should_patch_stage(current, target)
            elif column in payload:
                should_write = target != current
            if should_write:
                new_row[pos] = target
                changed.append(column)

        if not changed:
            return False, []

        write_columns = list(changed)
        if COL_LAST_SYNCED in payload:
            pos = self.actual_index_0[COL_LAST_SYNCED]
            target = payload.get(COL_LAST_SYNCED, "") or ""
            current = existing.get(COL_LAST_SYNCED, "") or ""
            if not current.startswith("=") and target != current:
                new_row[pos] = target
                write_columns.append(COL_LAST_SYNCED)

        self._unindex_identity(row_idx, existing_row)
        self.rows[row_idx - 1] = new_row
        self._index_identity(row_idx, new_row)
        self._refresh_unique_url_index()
        self._changed_columns.update(write_columns)

        pending_append_idx = self._pending_append_index_by_row.get(row_idx)
        if pending_append_idx is not None:
            self._pending_appends[pending_append_idx] = new_row
        else:
            for column in write_columns:
                self._schedule_cell_update(
                    row_idx=row_idx,
                    column=column,
                    value=new_row[self.actual_index_0[column]],
                    expected_value=existing.get(column, "") or "",
                )
        return False, changed

    @staticmethod
    def _preflight_cell_value(values: Any) -> str:
        """Return the exact formula-rendered value for one batch-get range."""
        if not values or not isinstance(values, (list, tuple)):
            return ""
        first_row = values[0]
        if not first_row or not isinstance(first_row, (list, tuple)):
            return ""
        value = first_row[0]
        return str(value) if value is not None else ""

    def _preflight_pending_updates(self) -> None:
        """Fail closed if any existing People cell changed after planning."""
        if not self._pending_update_by_cell:
            return

        # A first migration may update tens of thousands of cells. Supplying
        # each cell as a batchGet range creates dozens of requests and can hit
        # Sheets read quotas before any write. One formula-rendered snapshot is
        # both bounded and stronger: every expectation is checked against the
        # same live revision before publication begins.
        try:
            live_rows = self.ws.get_all_values(
                value_render_option=ValueRenderOption.formula,
            )
        except (APIError, TypeError) as exc:
            raise SheetsError(
                "failed optimistic preflight for People updates; "
                "no writes attempted"
            ) from exc
        if not isinstance(live_rows, (list, tuple)) or not live_rows:
            raise SheetsError(
                "People optimistic preflight returned an incomplete response; "
                "no writes attempted"
            )

        live_headers = live_rows[0]
        for (row_idx, column), _update in sorted(
            self._pending_update_by_cell.items(),
            key=lambda item: (
                item[0][0],
                self.actual_index_0[item[0][1]],
            ),
        ):
            column_0 = self.actual_index_0[column]
            if (
                column_0 >= len(live_headers)
                or str(live_headers[column_0]) != column
            ):
                raise SheetsError(
                    "People columns changed after planning; no writes attempted"
                )
            current = ""
            if row_idx - 1 < len(live_rows):
                live_row = live_rows[row_idx - 1]
                if isinstance(live_row, (list, tuple)) and column_0 < len(live_row):
                    value = live_row[column_0]
                    current = "" if value is None else str(value)
            expected = self._pending_expected_by_cell[(row_idx, column)]
            if current != expected:
                # Report only structural location, never either cell value.
                raise SheetsError(
                    f"People row {row_idx}, column {column!r} changed after "
                    "planning; no writes attempted"
                )

    def _preflight_pending_appends(self) -> None:
        """Fail closed if a staged append's stable identity is now live.

        A second publisher can append a People row after this index was loaded
        but before ``flush``. Re-reading only the two identity columns keeps
        the request bounded while preventing a duplicate append by either
        stable Lead ID or canonical LinkedIn URL. The header cells are part of
        the same read so a concurrent column move cannot turn this check into
        a read of the wrong data.
        """
        if not self._pending_appends:
            return

        pending_lead_ids = {
            self._cell(row, self.lead_id_col_0)
            for row in self._pending_appends
            if self._cell(row, self.lead_id_col_0)
        }
        pending_urls = {
            linkedin_identity_key(self._cell(row, self.url_col_0))
            for row in self._pending_appends
            if linkedin_identity_key(self._cell(row, self.url_col_0))
        }
        identity_columns = (
            (COL_LEAD_ID, self.lead_id_col_0, pending_lead_ids),
            (COL_LINKEDIN_URL, self.url_col_0, pending_urls),
        )
        ranges = [
            f"{_col_letter(column_0 + 1)}1:{_col_letter(column_0 + 1)}"
            for _column, column_0, _pending in identity_columns
        ]
        try:
            live_columns = self.ws.batch_get(
                ranges,
                # Identity is semantic here: an identity produced by a Sheet
                # formula must collide just like a literal value. Existing
                # cell-write preflights separately use formula mode so the
                # publisher still preserves formula text byte-for-byte.
                value_render_option=ValueRenderOption.unformatted,
            )
        except (APIError, TypeError) as exc:
            raise SheetsError(
                "failed optimistic preflight for People appends; "
                "no writes attempted"
            ) from exc
        if (
            not isinstance(live_columns, (list, tuple))
            or len(live_columns) != len(identity_columns)
        ):
            raise SheetsError(
                "People append preflight returned an incomplete response; "
                "no writes attempted"
            )

        for (column, _column_0, pending), values in zip(
            identity_columns,
            live_columns,
        ):
            if not isinstance(values, (list, tuple)) or not values:
                raise SheetsError(
                    "People identity columns changed after planning; "
                    "no writes attempted"
                )
            header = self._preflight_cell_value(values[:1])
            if header != column:
                raise SheetsError(
                    "People identity columns changed after planning; "
                    "no writes attempted"
                )

            for row_idx, row_values in enumerate(values[1:], start=2):
                value = self._preflight_cell_value([row_values])
                if not value:
                    continue
                identity = (
                    linkedin_identity_key(value)
                    if column == COL_LINKEDIN_URL
                    else value.strip()
                )
                if identity and identity in pending:
                    # Report only structural location, never the identifier.
                    raise SheetsError(
                        f"pending People append conflicts with live row "
                        f"{row_idx}, column {column!r}; no writes attempted"
                    )

    def flush(self, *, dry_run: bool = False) -> dict[str, int]:
        """Commit staged changes or return the exact no-write row counts."""
        counts = {
            "appended": len(self._pending_appends),
            "updated": len(self._pending_update_rows),
        }
        if dry_run:
            return counts

        # Both checks must precede appends as well as updates. Otherwise a stale
        # cell plan or a concurrently-created identity could fail only after a
        # partial People publication.
        self._preflight_pending_updates()
        self._preflight_pending_appends()

        if self._pending_appends:
            try:
                self.ws.append_rows(
                    self._pending_appends,
                    value_input_option="RAW",
                    table_range="A1",
                )
            except APIError as e:
                raise SheetsError(
                    f"failed appending {len(self._pending_appends)} rows: {e}"
                ) from e
            self._pending_appends = []
            self._pending_append_index_by_row = {}

        updates = self._pending_updates
        if updates:
            try:
                self.ws.batch_update(updates, value_input_option="RAW")
            except APIError as e:
                raise SheetsError(f"failed batch_update: {e}") from e
            self._pending_update_by_cell = {}
            self._pending_expected_by_cell = {}
            self._pending_update_rows = set()
        self._changed_columns = Counter()
        return counts


# ----------------------------------------------------------------------
# Row-payload builder — assembles the column→value dict for one Lead.
# ----------------------------------------------------------------------


def build_row_payload(
    *,
    lead,
    title: str,
    emails: list[str],
    outreach_status: str,
    stage: str,
    priority: str,
    primary_location: str,
    notes: str,
    ai_notes: str,
    last_synced: str,
) -> dict[str, str]:
    """Assemble the full per-row payload from the supplied data.

    System formatting (newline-joined emails and ISO dates) is applied here.
    Human-owned values are passed through byte-for-byte, including blanks and
    intentional leading/trailing whitespace.
    """
    cleaned_emails: list[str] = []
    seen: set[str] = set()
    for e in emails:
        e = (e or "").strip()
        if e and e not in seen:
            cleaned_emails.append(e)
            seen.add(e)

    full_name = f"{lead.first_name} {lead.last_name}".strip()
    created_at = lead.creation_date.date().isoformat() if lead.creation_date else ""
    return {
        COL_NAME: full_name,
        COL_FIRST_NAME: lead.first_name or "",
        COL_LAST_NAME: lead.last_name or "",
        COL_COMPANY: lead.company_name or "",
        COL_TITLE: title or "",
        COL_LINKEDIN_URL: canonical_linkedin_url(lead.linkedin_url or ""),
        COL_EMAILS: "\n".join(cleaned_emails),
        COL_OUTREACH_STATUS: outreach_status or "",
        COL_STAGE: stage or "",
        COL_PRIORITY: priority or "",
        COL_PRIMARY_LOCATION: primary_location or "",
        COL_NOTES: notes or "",
        COL_AI_NOTES: ai_notes or "",
        COL_CREATED_AT: created_at,
        COL_LAST_SYNCED: last_synced,
        COL_LEAD_ID: str(
            getattr(lead, "pk", None) or getattr(lead, "id", None) or ""
        ),
    }


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _col_letter(n: int) -> str:
    """1 → A, 2 → B, ..., 27 → AA. Keeps batch_update ranges A1-style."""
    letters = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        letters = chr(65 + r) + letters
    return letters


# ======================================================================
# Followups tabs (per-operator)
#
# A second surface in the same spreadsheet for the daily Claude follow-up
# generation workflow (see docs/followup-generation-workflow.md). Two tabs:
# `<Operator> - Followups`. Each tab has:
#   - Frozen header row (14 columns)
#   - Section dividers per workflow lane (Met / Scheduling / Replied /
#     Active in-flight / Sent history)
#   - One row per Lead within each section
#   - Sent? checkbox column the operator ticks after dispatching
#
# `write_followups()` is the entry point used by the Claude task at Phase 6.
# It preserves rows where Sent? = TRUE so history persists across daily runs.
# ======================================================================


# State labels used by the Claude task's classifier. Unlike the section
# headers below, these are outbound-state labels shown in the State column.
# Met / Scheduling / Sent are workflow lanes, not cohorts — the operator
# wanted the cell value to answer "where are we with this outbound?" rather
# than "which section is this row parked in?".
STATE_BALL_ON_US = "Ball on us"
STATE_COLD_THREAD = "Cold thread"
STATE_BALL_ON_THEM = "Ball on them"

# Legacy values kept only for back-compat when reading stale rows / payloads.
STATE_MET_LEGACY = "Met"
STATE_SCHEDULING_LEGACY = "Scheduling"
STATE_SENT_LEGACY = "Sent"
STATE_ACTIVE_IN_FLIGHT_LEGACY = "Active in-flight"

# Section ordering within each tab. Sections are derived from row Status
# plus preserved-sent state, not from the Cohort cell.
#
# Note: the "Connected, no reply" cohort was removed 2026-05-12. The
# daemon now handles those leads programmatically via
# `linkedin/tasks/follow_up.py` + the rigid ICP templates in
# `linkedin/icp_messages.json` — surfacing them in the Followups tab
# for manual drafting was redundant work the operator no longer needs.
# Rows land in:
#   - 🤝 MET when Status is post-meeting
#   - 📅 SCHEDULING when Status is pre-meeting
#   - 💬 REPLIED / 🌊 ACTIVE IN-FLIGHT based on State for everybody else
#   - ✅ SENT when preserved from a prior run with a Sent toggle = Yes
SECTION_MET = "met"
SECTION_SCHEDULING = "scheduling"
SECTION_REPLIED = "replied"
SECTION_ACTIVE_IN_FLIGHT = "active_in_flight"
SECTION_SENT = "sent"

FU_SECTIONS = [
    ("🤝 MET", SECTION_MET),
    ("📅 SCHEDULING", SECTION_SCHEDULING),
    ("💬 REPLIED", SECTION_REPLIED),
    ("🌊 ACTIVE IN-FLIGHT", SECTION_ACTIVE_IN_FLIGHT),
    ("✅ SENT", SECTION_SENT),
]

# ROLE / PRIORITY dropdown vocabulary (mirrors the workflow doc).
FU_ROLES = ["CSP", "3PAO", "Advisor", "Assessor", "Channel"]
FU_PRIORITIES = ["HIGH", "MEDIUM-HIGH", "MEDIUM", "LOW", "HOLD"]
FU_PRIORITY_RANK = {
    "HIGH": 5, "MEDIUM-HIGH": 4, "MEDIUM": 3, "LOW": 2, "HOLD": 1,
}
# Yes/No dropdown for the two Sent columns. Default to "No" so new rows
# never have blank sent state — easier for the operator to scan.
SENT_VALUES = ["Yes", "No"]
DEFAULT_SENT = "No"

# Followups tab schema — 13 columns. Order matters; reordering changes
# the literal sheet layout. The two Sent columns are independent yes/no
# dropdowns the operator toggles by hand after sending; the two Draft
# columns are populated by the drafter (one or both, depending on which
# medium has real engagement). Email Link and LinkedIn Message Url are
# HYPERLINK formulas that open the conversation in Gmail / LinkedIn.
FU_COL_NAME = "Name"
FU_COL_STATUS = "Status"
FU_COL_STATE = "State"
FU_COL_ROLE = "ROLE"
FU_COL_PRIORITY = "PRIORITY"
FU_COL_DAYS_SINCE = "Days since"
FU_COL_DAYS_SINCE_CONNECTION = "Days since connection"
FU_COL_CONVO = "CONVO"
FU_COL_DRAFT_EMAIL = "Draft Email"
FU_COL_EMAIL_LINK = "Email Link"
FU_COL_SENT_EMAIL = "Sent Email (manual toggle)"
FU_COL_DRAFT_LINKEDIN = "Draft LinkedIn"
FU_COL_LINKEDIN_MSG_URL = "LinkedIn Message Url"
FU_COL_SENT_LINKEDIN = "Sent LinkedIn (manual toggle)"
# Operator-driven disqualify toggle. Default "Qualify" on every fresh
# row; operator flips to "Disqualify" by hand. The next run reads the
# tab BEFORE rebuild, sets Lead.disqualified=True for everyone marked
# Disqualify, and the cohort classifier's existing
# `lead__disqualified=False` filter then naturally excludes them from
# the new run's draftable set.
FU_COL_QUALIFY = "Qualify/Disqualify"

FU_HEADERS = [
    FU_COL_NAME, FU_COL_STATUS, FU_COL_STATE, FU_COL_ROLE,
    FU_COL_PRIORITY, FU_COL_DAYS_SINCE, FU_COL_DAYS_SINCE_CONNECTION,
    FU_COL_CONVO,
    FU_COL_DRAFT_EMAIL, FU_COL_EMAIL_LINK, FU_COL_SENT_EMAIL,
    FU_COL_DRAFT_LINKEDIN, FU_COL_LINKEDIN_MSG_URL, FU_COL_SENT_LINKEDIN,
    FU_COL_QUALIFY,
]
QUALIFY_VALUES = ["Qualify", "Disqualify"]
DEFAULT_QUALIFY = "Qualify"
FU_HEADER_INDEX_0 = {h: i for i, h in enumerate(FU_HEADERS)}
FU_PRIORITY_COL_0 = FU_HEADER_INDEX_0[FU_COL_PRIORITY]
FU_STATE_COL_0 = FU_HEADER_INDEX_0[FU_COL_STATE]
FU_SENT_EMAIL_COL_0 = FU_HEADER_INDEX_0[FU_COL_SENT_EMAIL]
FU_SENT_LINKEDIN_COL_0 = FU_HEADER_INDEX_0[FU_COL_SENT_LINKEDIN]
FU_QUALIFY_COL_0 = FU_HEADER_INDEX_0[FU_COL_QUALIFY]
FU_NAME_COL_0 = FU_HEADER_INDEX_0[FU_COL_NAME]

# ROLE → ICP-template bucket mapping. The "ICP Goals" tab keys
# rows by ICP, but the followup row carries a finer-grained ROLE; this maps
# one to the other so the drafter can look up the right Goal cell.
#
# Channel got its own bucket on 2026-05-12 (was previously collapsed into
# Advisors). The partnership/co-sell pitch is structurally different from
# the "deliver to your CSP clients" Advisor pitch, and we now stamp it
# from the CSV `ICP` column at import time so the routing has the signal
# it needs without depending on a runtime classifier that can't tell a
# reseller from an in-house security engineer from the headline alone.
FU_ROLE_TO_ICP = {
    "CSP": "CSPs",
    "3PAO": "3PAOs/Assessors",
    "Assessor": "3PAOs/Assessors",
    "Advisor": "Advisors",
    "Channel": "Channel",
}

# Canonical ICP buckets persisted on `Lead.icp`. Templates in
# `linkedin/icp_messages.json` key on these strings; connect-note +
# follow-up + sheets ROLE column all converge here. Adding a bucket means
# adding a key in the JSON and listing it here.
LEAD_ICP_BUCKETS = (
    "CSPs",
    "3PAOs/Assessors",
    "Advisors",
    "Channel",
    "Investor / Portfolio Ops",
    "Accelerator / Ecosystem",
    "20x Initial Implementation",
    "Rev5 Ready",
    "Active FedRAMP Path",
    "FedRAMP Mature",
    "CSP Stage Verify",
    "White Label Product/Executive",
    "White Label Partnerships",
    "White Label Delivery",
    "White Label Champions",
    "CMMC Buyers",
    "CMMC Advisor/Channel",
)

# Operator's Sales Nav search labels (the values in the merged CSV's
# `ICP` column) normalized to the persisted vocab. `add_seeds` applies
# this map at import so a single Lead.icp value drives all downstream
# template routing. Keys are case-insensitive — see
# `linkedin.setup.seeds._normalize_csv_icp`.
CSV_ICP_TO_LEAD_ICP = {
    "csps":             "CSPs",
    "advisors":         "Advisors",
    "channel":          "Channel",
    "investor / portfolio ops": "Investor / Portfolio Ops",
    "investor/portfolio ops":   "Investor / Portfolio Ops",
    "investor portfolio ops":   "Investor / Portfolio Ops",
    "accelerator / ecosystem":  "Accelerator / Ecosystem",
    "accelerator/ecosystem":    "Accelerator / Ecosystem",
    "accelerator ecosystem":    "Accelerator / Ecosystem",
    "rev5 ready":       "Rev5 Ready",
    "fedramp ready":    "Rev5 Ready",
    "ready":            "Rev5 Ready",
    "legacy ready":     "Rev5 Ready",
    "20x initial implementation": "20x Initial Implementation",
    "initial implementation":     "20x Initial Implementation",
    "active fedramp path":         "Active FedRAMP Path",
    "agency in process":           "Active FedRAMP Path",
    "fedramp in process":          "Active FedRAMP Path",
    "fedramp mature":              "FedRAMP Mature",
    "fedramp certified":           "FedRAMP Mature",
    "fedramp certified or mature": "FedRAMP Mature",
    "certified mature":            "FedRAMP Mature",
    "csp stage verify": (
        "CSP Stage Verify"
    ),
    "stage verify": (
        "CSP Stage Verify"
    ),
    "established federal portfolio, exact path verify": (
        "CSP Stage Verify"
    ),
    "white label product/executive": "White Label Product/Executive",
    "white label partnerships":      "White Label Partnerships",
    "white label delivery":          "White Label Delivery",
    "white label champions":         "White Label Champions",
    "firms-advisors":   "Advisors",
    "grc-advisors":     "Advisors",
    "vciso-advisors":   "Advisors",
    "3paos":            "3PAOs/Assessors",
    "3paos/assessors":  "3PAOs/Assessors",
    "assessors":        "3PAOs/Assessors",
    "cmmc buyers":      "CMMC Buyers",
    "cmmc buyer":       "CMMC Buyers",
    "cmmc-buyer":       "CMMC Buyers",
    "cmmc-buyers":      "CMMC Buyers",
    "cmmc_buyers":      "CMMC Buyers",
    "buyer":            "CMMC Buyers",
    "buyers":           "CMMC Buyers",
    "keep_buyer":       "CMMC Buyers",
    "cmmc advisor/channel":   "CMMC Advisor/Channel",
    "cmmc advisors/channel":  "CMMC Advisor/Channel",
    "cmmc advisor channel":   "CMMC Advisor/Channel",
    "cmmc advisors channels": "CMMC Advisor/Channel",
    "cmmc advisor":           "CMMC Advisor/Channel",
    "cmmc advisors":          "CMMC Advisor/Channel",
    "cmmc channel":           "CMMC Advisor/Channel",
    "cmmc channels":          "CMMC Advisor/Channel",
    "cmmc_advisor_channel":   "CMMC Advisor/Channel",
    "keep_advisor_channel":   "CMMC Advisor/Channel",
}


def _followup_tab_name(operator: str) -> str:
    return f"{operator} - Followups"


def _followup_section_key(row: dict, *, preserved_sent: bool = False) -> str | None:
    """Map a followup row into one of the sheet sections.

    Section placement is primarily driven by Status (meeting-track rows) and
    secondarily by State (reply / active rows). Preserved sent rows always go
    to the history section regardless of their current State cell value.
    """
    if preserved_sent:
        return SECTION_SENT

    status = (row.get(FU_COL_STATUS) or "").strip()
    state = (row.get(FU_COL_STATE) or row.get("Cohort") or "").strip()

    if status in MET_STATUSES or state == STATE_MET_LEGACY:
        return SECTION_MET
    if status in PRE_MEETING_STATUSES or state == STATE_SCHEDULING_LEGACY:
        return SECTION_SCHEDULING
    if state in {STATE_BALL_ON_US, STATE_COLD_THREAD}:
        return SECTION_REPLIED
    if state in {STATE_BALL_ON_THEM, STATE_ACTIVE_IN_FLIGHT_LEGACY}:
        return SECTION_ACTIVE_IN_FLIGHT
    if state == STATE_SENT_LEGACY:
        return SECTION_SENT
    return None


def _gspread_client():
    """Return an authorized gspread client + opened spreadsheet."""
    sheet_id, creds_path, _ = _require_config()
    creds = Credentials.from_service_account_file(
        creds_path, scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    gc = gspread.authorize(creds)
    return gc.open_by_key(sheet_id)


_SENT_TRUE_VALUES = {"YES", "Y", "TRUE", "✓", "X"}


def _is_sent(cell: str) -> bool:
    return (cell or "").strip().upper() in _SENT_TRUE_VALUES


def read_disqualified_names_from_followups(operator: str) -> set[str]:
    """Return the set of Names the operator has marked Disqualify in their
    followups tab. Empty set if the tab doesn't exist yet (first run).

    Phase 1 of the followup workflow calls this for both operators *before*
    classification, then sets `Lead.disqualified=True` on each matching
    Lead. The classifier's existing `lead__disqualified=False` filter then
    drops them from cohort selection — they get no draft and are absent
    from the next tab rebuild. Marking is one-way: re-toggling to Qualify
    in the sheet doesn't reverse `Lead.disqualified` (DB is the source of
    truth; sheet is just an input form).

    Lookup is by Name (case-insensitive, trimmed). The current cohort has
    no duplicate names; if you ever hit a collision, both leads with that
    name get disqualified. Acceptable trade-off for now — the alternative
    is plumbing a hidden LinkedIn URL column.
    """
    sh = _gspread_client()
    try:
        ws = sh.worksheet(_followup_tab_name(operator))
    except WorksheetNotFound:
        return set()

    try:
        rows = ws.get_all_values()
    except APIError as e:
        raise SheetsError(f"failed reading {operator} followups: {e}") from e
    if len(rows) < 2:
        return set()

    out: set[str] = set()
    max_idx = max(FU_QUALIFY_COL_0, FU_NAME_COL_0)
    for row in rows[1:]:
        if len(row) <= max_idx:
            continue
        if (row[FU_QUALIFY_COL_0] or "").strip().lower() != "disqualify":
            continue
        name = (row[FU_NAME_COL_0] or "").strip()
        if name:
            out.add(name.lower())
    return out


def read_followup_sent_rows(operator: str) -> list[dict[str, str]]:
    """Return existing rows in the operator's Followups tab where the operator
    has marked the lead as sent on EITHER channel (`Sent Email` or
    `Sent LinkedIn` toggle = `Yes`).

    Used by the Claude task to (a) preserve history across runs and (b) skip
    already-sent leads when generating new drafts. Returns the full row dict
    keyed by FU_HEADERS column name. A row marked sent on only one channel
    is still preserved as a unit — the operator owns both toggles together
    in the same row.
    """
    sh = _gspread_client()
    try:
        ws = sh.worksheet(_followup_tab_name(operator))
    except WorksheetNotFound:
        return []

    try:
        all_vals = ws.get_all_values()
    except APIError as e:
        raise SheetsError(f"failed reading {operator} followups: {e}") from e
    if len(all_vals) < 2:
        return []

    max_idx = max(FU_SENT_EMAIL_COL_0, FU_SENT_LINKEDIN_COL_0)
    sent_rows: list[dict[str, str]] = []
    for row in all_vals[1:]:
        if len(row) <= max_idx:
            continue
        if not (
            _is_sent(row[FU_SENT_EMAIL_COL_0])
            or _is_sent(row[FU_SENT_LINKEDIN_COL_0])
        ):
            continue
        record = {
            h: (row[i] if i < len(row) else "")
            for i, h in enumerate(FU_HEADERS)
        }
        sent_rows.append(record)
    return sent_rows


def write_followups(rows_by_operator: dict[str, list[dict]]) -> dict[str, int]:
    """Wipe and rebuild each operator's Followups tab with fresh rows.

    Rows from a prior run that had Sent? = TRUE are preserved verbatim and
    surfaced under a `✅ SENT` history section at the bottom — the Claude task
    is expected to also exclude them from `rows_by_operator` so they don't get
    re-drafted on top of themselves. Any Name appearing in BOTH the preserved
    Sent set AND the fresh payload will keep the new payload (caller's data
    wins — the Claude task explicitly chose to redraft).

    `rows_by_operator` is `{"Arian": [row, ...], "Chuka": [row, ...]}`. Each
    row is a dict with the keys in FU_HEADERS (case-sensitive). The two
    `Sent ... (manual toggle)` cells default to `"No"` if the dict omits
    them. `Email Link` and `LinkedIn Message Url` should be HYPERLINK()
    formula strings (e.g. `=HYPERLINK("...","display text")`); the sheet
    write uses USER_ENTERED so they evaluate.

    Hidden-column state from the prior run is preserved: any column the
    operator has hidden (View → Hide column) before the run stays hidden
    after the rewrite, by index.

    Returns `{operator: rows_written}` for logging.
    """
    sh = _gspread_client()
    counts: dict[str, int] = {}

    # Resolve which sheet ids correspond to existing followup tabs once,
    # so we can pull column metadata in a single fetch_sheet_metadata call
    # below (cheaper than per-tab gets).
    operator_titles = {op: _followup_tab_name(op) for op in rows_by_operator}
    hidden_cols_by_operator = _snapshot_hidden_columns(sh, operator_titles.values())

    for operator, fresh_rows in rows_by_operator.items():
        # 1. Pull preserved Sent rows from the existing tab (if any).
        try:
            preserved = read_followup_sent_rows(operator)
        except SheetsError as e:
            logger.warning("could not read preserved Sent rows for %s: %s", operator, e)
            preserved = []

        # The Claude task may also include some of these in its fresh payload
        # (operator unticked a Sent toggle for a redraft). Caller's data wins.
        # We dedupe by Name since LinkedIn URL is no longer a column.
        fresh_names = {(r.get(FU_COL_NAME) or "").strip() for r in fresh_rows}
        preserved = [
            p for p in preserved
            if (p.get(FU_COL_NAME) or "").strip() not in fresh_names
        ]
        # 2. Drop and recreate the tab so we control layout fully.
        title = operator_titles[operator]
        for w in sh.worksheets():
            if w.title == title:
                sh.del_worksheet(w)
                break
        ws = sh.add_worksheet(title=title, rows=400, cols=len(FU_HEADERS))
        sheet_id = ws.id

        # 3. Group all rows (fresh + preserved) by section.
        by_section: dict[str, list[dict]] = {}
        for r in fresh_rows:
            section = _followup_section_key(r)
            if section is None:
                logger.warning("skipping followup row with unknown section: %s", r)
                continue
            by_section.setdefault(section, []).append(r)
        for r in preserved:
            by_section.setdefault(SECTION_SENT, []).append(r)

        # 4. Sort within each cohort: priority desc, then days_since desc.
        def _sort_key(row: dict) -> tuple:
            pr = FU_PRIORITY_RANK.get(row.get(FU_COL_PRIORITY) or "", 0)
            try:
                ds = int(row.get(FU_COL_DAYS_SINCE) or 0)
            except (TypeError, ValueError):
                ds = 0
            return (-pr, -ds)

        # 5. Build the value matrix: header → (divider + rows) per section.
        all_rows: list[list[str]] = [list(FU_HEADERS)]
        section_header_row_idx_0: list[int] = []
        section_total = 0
        for label, section_key in FU_SECTIONS:
            section_rows = by_section.get(section_key, [])
            section_rows.sort(key=_sort_key)
            count = len(section_rows)
            section_total += count
            section_header_row_idx_0.append(len(all_rows))
            all_rows.append([f"{label} — {count} leads"] + [""] * (len(FU_HEADERS) - 1))
            for r in section_rows:
                all_rows.append([_followup_cell(r, h) for h in FU_HEADERS])

        # USER_ENTERED so HYPERLINK() formulas in Email Link / LinkedIn
        # Message Url cells evaluate instead of being stored as literal text.
        ws.update(values=all_rows, range_name="A1", value_input_option="USER_ENTERED")

        # 6. Format requests: header + section dividers + data validation +
        #    conditional formatting + frozen header + CLIP wrap +
        #    re-applied hidden columns.
        n_data_rows = len(all_rows)
        requests: list[dict] = []

        # Bold + bg the header row, freeze it.
        requests.append(_repeat_cell(sheet_id, 0, 1, 0, len(FU_HEADERS), {
            "textFormat": {"bold": True},
            "backgroundColor": _rgb255(220, 230, 241),
        }, "userEnteredFormat.textFormat.bold,userEnteredFormat.backgroundColor"))
        requests.append({
            "updateSheetProperties": {
                "properties": {
                    "sheetId": sheet_id,
                    "gridProperties": {"frozenRowCount": 1},
                },
                "fields": "gridProperties.frozenRowCount",
            },
        })
        # CLIP wrap on the whole sheet.
        requests.append({
            "repeatCell": {
                "range": {"sheetId": sheet_id},
                "cell": {"userEnteredFormat": {"wrapStrategy": "CLIP"}},
                "fields": "userEnteredFormat.wrapStrategy",
            },
        })
        # Section divider styling: bold, bg, merged across columns.
        for ri_0 in section_header_row_idx_0:
            requests.append(_repeat_cell(sheet_id, ri_0, ri_0 + 1, 0, len(FU_HEADERS), {
                "textFormat": {"bold": True},
                "backgroundColor": _rgb255(235, 235, 240),
            }, "userEnteredFormat.textFormat.bold,userEnteredFormat.backgroundColor"))
            requests.append({
                "mergeCells": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": ri_0, "endRowIndex": ri_0 + 1,
                        "startColumnIndex": 0, "endColumnIndex": len(FU_HEADERS),
                    },
                    "mergeType": "MERGE_ALL",
                },
            })
        # Data validation dropdowns on Cohort, ROLE, PRIORITY, plus
        # Yes/No on the two Sent toggles.
        for col0, options, strict in [
            (FU_STATE_COL_0,
             [STATE_BALL_ON_US, STATE_COLD_THREAD, STATE_BALL_ON_THEM], False),
            (FU_HEADER_INDEX_0[FU_COL_ROLE], FU_ROLES, False),
            (FU_PRIORITY_COL_0, FU_PRIORITIES, False),
            (FU_SENT_EMAIL_COL_0, SENT_VALUES, True),
            (FU_SENT_LINKEDIN_COL_0, SENT_VALUES, True),
            (FU_QUALIFY_COL_0, QUALIFY_VALUES, True),
        ]:
            requests.append({
                "setDataValidation": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": 1, "endRowIndex": n_data_rows,
                        "startColumnIndex": col0, "endColumnIndex": col0 + 1,
                    },
                    "rule": {
                        "condition": {
                            "type": "ONE_OF_LIST",
                            "values": [{"userEnteredValue": v} for v in options],
                        },
                        "showCustomUi": True,
                        "strict": strict,
                    },
                },
            })

        # Conditional formatting on PRIORITY column (rank-aware text colors).
        priority_text_colors = [
            ("HIGH",        _rgb255(13, 122, 42),   True),   # green, bold
            ("MEDIUM-HIGH", _rgb255(34, 170, 68),   True),   # mid-green, bold
            ("MEDIUM",      _rgb255(221, 102, 34),  False),  # orange
            ("LOW",         _rgb255(108, 117, 125), False),  # gray
            ("HOLD",        _rgb255(108, 117, 125), False),  # gray
        ]
        for value, fg, bold in priority_text_colors:
            requests.append(_text_cond_rule(
                sheet_id, FU_PRIORITY_COL_0, value, fg, bold,
            ))

        # Conditional formatting on State column.
        state_text_colors = [
            (STATE_BALL_ON_US,    _rgb255(204, 17, 34),   True),    # red — most urgent
            (STATE_COLD_THREAD,   _rgb255(221, 102, 34),  False),   # orange
            (STATE_BALL_ON_THEM,  _rgb255(0, 102, 204),   False),   # blue
        ]
        for value, fg, bold in state_text_colors:
            requests.append(_text_cond_rule(
                sheet_id, FU_STATE_COL_0, value, fg, bold,
            ))

        # Color the Qualify/Disqualify column so scanning is fast.
        requests.append(_text_cond_rule(
            sheet_id, FU_QUALIFY_COL_0, "Disqualify",
            _rgb255(204, 17, 34), True,    # red, bold — clearly intentional
        ))
        requests.append(_text_cond_rule(
            sheet_id, FU_QUALIFY_COL_0, "Qualify",
            _rgb255(108, 117, 125), False,  # gray — quiet default
        ))

        # Re-apply hidden-column state captured before the drop. Coalesce
        # contiguous indices into single dimension ranges to cut request
        # volume — operators commonly hide adjacent columns together.
        for start, end in _coalesce_runs(hidden_cols_by_operator.get(title, [])):
            requests.append({
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "COLUMNS",
                        "startIndex": start,
                        "endIndex": end,
                    },
                    "properties": {"hiddenByUser": True},
                    "fields": "hiddenByUser",
                },
            })

        sh.batch_update({"requests": requests})

        # Auto-resize Name only — Status / Cohort / ROLE / PRIORITY are
        # short, the wider columns (CONVO + Drafts) we want CLIP-wrapped
        # at a sensible default width rather than auto-grown to fit.
        sh.batch_update({"requests": [{
            "autoResizeDimensions": {
                "dimensions": {
                    "sheetId": sheet_id, "dimension": "COLUMNS",
                    "startIndex": 0, "endIndex": 1,
                },
            },
        }]})

        counts[operator] = section_total
        logger.info("wrote %d followup row(s) for %s", section_total, operator)

    return counts


# ----------------------------------------------------------------------
# Followup-row helpers
# ----------------------------------------------------------------------


def _followup_cell(row: dict, header: str) -> str:
    """Render one cell value, applying defaults for Sent and Qualify toggles."""
    v = row.get(header, "")
    if header in (FU_COL_SENT_EMAIL, FU_COL_SENT_LINKEDIN):
        v = _serialize_cell(v).strip() or DEFAULT_SENT
        # Normalize legacy boolean inputs into the dropdown vocabulary.
        if v.upper() in {"TRUE", "YES", "Y"}:
            return "Yes"
        return "No"
    if header == FU_COL_QUALIFY:
        v = _serialize_cell(v).strip() or DEFAULT_QUALIFY
        # Coerce to one of the two dropdown values so a stale "qualified"
        # / "disqualified" lower-cased input from a prior payload still
        # lands as a valid dropdown selection.
        if v.lower().startswith("disq"):
            return "Disqualify"
        return "Qualify"
    return _serialize_cell(v)


def _snapshot_hidden_columns(
    sh, titles: Iterable[str],
) -> dict[str, list[int]]:
    """Return {tab_title: [hidden_col_index_0, ...]} for the given tabs.

    Read once at the top of write_followups so that del_worksheet doesn't
    erase the operator's hide/show state. Tabs that don't exist yet
    contribute nothing.
    """
    titles_set = {t for t in titles if t}
    if not titles_set:
        return {}
    try:
        meta = sh.fetch_sheet_metadata({
            "fields": (
                "sheets(properties(sheetId,title),"
                "data(columnMetadata(hiddenByUser)))"
            ),
        })
    except APIError as e:
        logger.warning("could not snapshot hidden columns: %s", e)
        return {}

    out: dict[str, list[int]] = {}
    for s in meta.get("sheets", []):
        title = s.get("properties", {}).get("title")
        if title not in titles_set:
            continue
        col_meta = (s.get("data") or [{}])[0].get("columnMetadata") or []
        hidden = [i for i, c in enumerate(col_meta) if c.get("hiddenByUser")]
        if hidden:
            out[title] = hidden
    return out


def _coalesce_runs(indices: list[int]) -> list[tuple[int, int]]:
    """Compress a sorted list of column indices into [start, end) runs."""
    if not indices:
        return []
    indices = sorted(set(indices))
    runs: list[tuple[int, int]] = []
    start = prev = indices[0]
    for i in indices[1:]:
        if i == prev + 1:
            prev = i
            continue
        runs.append((start, prev + 1))
        start = prev = i
    runs.append((start, prev + 1))
    return runs


# ----------------------------------------------------------------------
# Cell-content helpers — HYPERLINK formulas + ICP Goal lookup.
# ----------------------------------------------------------------------


_LINKEDIN_THREAD_BASE = "https://www.linkedin.com/messaging/thread/"
_GMAIL_SEARCH_BASE = "https://mail.google.com/mail/u/0/#search/"


def linkedin_thread_url(
    thread_external_id: str | None,
    fallback_profile_url: str = "",
) -> str:
    """Convert a LinkedIn conv URN into a deep-link URL.

    Accepts `urn:li:conv:<id>` or a bare `<id>`. Returns
    `https://www.linkedin.com/messaging/thread/<id>/`. If no usable URN,
    returns `fallback_profile_url` so the column always points somewhere
    sensible.
    """
    raw = (thread_external_id or "").strip()
    if not raw:
        return fallback_profile_url
    # Strip any URN prefix; the path-tail is what LinkedIn uses in the URL.
    if raw.startswith("urn:li:conv:"):
        raw = raw.split(":", 3)[-1]
    return f"{_LINKEDIN_THREAD_BASE}{raw}/"


def hyperlink_formula(url: str, display: str) -> str:
    """Build a Sheets HYPERLINK() formula. Both arguments are escaped so a
    rogue quote in either won't break the formula. Empty url → empty cell."""
    url = (url or "").strip()
    if not url:
        return ""
    safe_url = url.replace('"', '""')
    safe_display = (display or url).replace('"', '""')
    return f'=HYPERLINK("{safe_url}","{safe_display}")'


def email_search_hyperlink(email: str) -> str:
    """=HYPERLINK formula opening a Gmail search for any thread with the
    given email address (matches both from: and to:). Display text is the
    raw email so the cell is still human-readable when copy-pasted."""
    e = (email or "").strip()
    if not e:
        return ""
    from urllib.parse import quote
    encoded = quote(e, safe="")
    url = f"{_GMAIL_SEARCH_BASE}{encoded}"
    return hyperlink_formula(url, e)


def linkedin_message_hyperlink(
    thread_external_id: str | None,
    profile_url: str = "",
    display: str = "Open in LinkedIn",
) -> str:
    """=HYPERLINK formula for the lead's LinkedIn profile.

    `thread_external_id` is accepted but ignored. The thread-URL path used
    to build `https://www.linkedin.com/messaging/thread/<urn>/` from the
    Voyager `urn:li:msg_conversation:(...)` URN, but LinkedIn web treats
    that path as opaque — the parens / colons / equals signs don't reliably
    resolve to the inbox thread, so the operator clicks the cell and lands
    on a broken page. The profile URL always opens the lead's profile,
    where the operator can press the Message button to reach the same DM
    thread. Simpler, durable.
    """
    target = (profile_url or "").strip()
    return hyperlink_formula(target, display)


# ----------------------------------------------------------------------
# ICP Goals tab — read the operator's per-ICP Goal cells.
# ----------------------------------------------------------------------


ICP_GOALS_TAB = "ICP Goals"
ICP_MESSAGES_TAB_SUFFIX = "ICP Messages"


def read_icp_goals() -> dict[str, dict[str, str]]:
    """Return `{ICP: {"goal": str}}` from the operator's `ICP Goals` tab.

    Columns are matched by header name (case-insensitive, trimmed) so the
    operator can reorder columns without breaking the read. Headers
    recognized:
      - "ICP" (or "ICP Name" / "Name")           → bucket key
      - "Goal" (or "Goals")                      → strategic-direction prose

    Missing goal values return as empty strings — the drafter falls back
    to the workflow doc's default ROLE framing when a goal is empty.
    Missing tab returns an empty dict.
    """
    try:
        sh = _gspread_client()
        ws = sh.worksheet(ICP_GOALS_TAB)
    except WorksheetNotFound:
        return {}
    except APIError as e:
        raise SheetsError(f"failed opening ICP Goals: {e}") from e

    try:
        rows = ws.get_all_values()
    except APIError as e:
        raise SheetsError(f"failed reading ICP Goals: {e}") from e

    if len(rows) < 2:
        return {}

    header_lower = [h.strip().lower() for h in rows[0]]

    def col_idx(*candidates: str) -> int | None:
        for cand in candidates:
            try:
                return header_lower.index(cand.lower())
            except ValueError:
                continue
        return None

    icp_idx = col_idx("icp", "icp name", "name")
    if icp_idx is None:
        icp_idx = 0  # legacy layout — assume ICP is column A
    goal_idx = col_idx("goal", "goals")

    def cell(row: list[str], idx: int | None) -> str:
        if idx is None or idx >= len(row):
            return ""
        return row[idx].strip()

    out: dict[str, dict[str, str]] = {}
    for row in rows[1:]:
        icp = cell(row, icp_idx)
        if not icp:
            continue
        out[icp] = {
            "goal": cell(row, goal_idx),
        }
    return out


def icp_messages_tab_name(sender: str) -> str:
    """Human-readable worksheet title for one sender's rigid ICP messages."""
    return f"{sender} {ICP_MESSAGES_TAB_SUFFIX}"


def write_icp_messages_tab(sender: str, rows: list[list[str]]) -> None:
    """Overwrite one sender's ICP-message worksheet with flattened rows."""
    sh = _gspread_client()
    title = icp_messages_tab_name(sender)
    try:
        ws = sh.worksheet(title)
    except WorksheetNotFound:
        ws = sh.add_worksheet(
            title=title,
            rows=max(len(rows) + 10, 50),
            cols=max(len(rows[0]) if rows else 4, 4),
        )
    try:
        ws.clear()
        ws.update(values=rows, range_name="A1", value_input_option="USER_ENTERED")
    except APIError as e:
        raise SheetsError(f"failed writing {title}: {e}") from e


def read_icp_messages_tab(sender: str) -> list[list[str]]:
    """Return raw rows from one sender's ICP-message worksheet."""
    sh = _gspread_client()
    title = icp_messages_tab_name(sender)
    try:
        ws = sh.worksheet(title)
    except WorksheetNotFound as e:
        raise SheetsError(f"{title} tab not found") from e
    try:
        return ws.get_all_values()
    except APIError as e:
        raise SheetsError(f"failed reading {title}: {e}") from e


def _serialize_cell(v) -> str:
    """Render a Python value into a cell-safe string."""
    if v is True:
        return "TRUE"
    if v is False:
        return "FALSE"
    if v is None:
        return ""
    return str(v)


def _rgb255(r: int, g: int, b: int) -> dict:
    return {"red": r / 255, "green": g / 255, "blue": b / 255}


def _repeat_cell(sheet_id: int, r0: int, r1: int, c0: int, c1: int,
                 fmt: dict, fields: str) -> dict:
    return {
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": r0, "endRowIndex": r1,
                "startColumnIndex": c0, "endColumnIndex": c1,
            },
            "cell": {"userEnteredFormat": fmt},
            "fields": fields,
        },
    }


def _text_cond_rule(sheet_id: int, col0: int, value: str,
                    fg: dict, bold: bool) -> dict:
    """Build an addConditionalFormatRule request for TEXT_EQ → text color."""
    return {
        "addConditionalFormatRule": {
            "rule": {
                "ranges": [{
                    "sheetId": sheet_id,
                    "startRowIndex": 1,
                    "startColumnIndex": col0, "endColumnIndex": col0 + 1,
                }],
                "booleanRule": {
                    "condition": {
                        "type": "TEXT_EQ",
                        "values": [{"userEnteredValue": value}],
                    },
                    "format": {
                        "textFormat": {"foregroundColor": fg, "bold": bold},
                    },
                },
            },
            "index": 0,
        },
    }
