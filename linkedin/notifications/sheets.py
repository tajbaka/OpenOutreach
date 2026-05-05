"""Google Sheets CRM sync via the Sheets API (gspread).

Mirrors Deal state into a single People tab. Each Lead with an active Deal
becomes one row, keyed by LinkedIn URL. Per-row payload includes everything
that lives in Attio People + the Lead/Deal-derived rollups (Stage, Outreach
status), in one row write — no separate calls for notes, emails, status etc.

Why a single tab instead of Companies/People/Deals like Airtable? Sheets has
no first-class linked records — multi-tab joins would just be VLOOKUPs. The
operator's actual workflow is "scan a People list and triage" so we
denormalize Company name + aggregate Stage onto each Person row.

Idempotent: rows are upserted by `LinkedIn URL` (column F). The model needs
no per-row pointer — we load the whole sheet once per sync and dict-by-URL.

Don't-downgrade rules from Airtable carry over for Outreach status + Stage:
human edits in the sheet survive the next auto-sync. Notes, AI Notes,
Priority, Primary location, Title — sheet wins on subsequent syncs (they're
not auto-derived; what's in the sheet is more recent than Attio's snapshot).

Failure mode: raises SheetsError on auth / API errors. Callers (sync_sheets)
decide whether to skip-and-continue or abort.
"""
from __future__ import annotations

import logging
from typing import Any

import gspread
from google.oauth2.service_account import Credentials
from gspread.exceptions import APIError, SpreadsheetNotFound, WorksheetNotFound

from linkedin.conf import (
    GOOGLE_SHEETS_CREDENTIALS_PATH,
    GOOGLE_SHEETS_ID,
    GOOGLE_SHEETS_TAB_NAME,
)
from linkedin.exceptions import SheetsError

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
    """Map Deal.state to a Stage."""
    from linkedin.enums import ProfileState

    state = deal.state
    if state == ProfileState.COMPLETED:
        return STAGE_WON
    if state == ProfileState.FAILED:
        return STAGE_LOST
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


def deal_to_outreach_status(deal) -> str:
    """Map Deal.state to a per-person Outreach status (auto-managed values only)."""
    from linkedin.enums import ProfileState

    state = deal.state
    if state == ProfileState.COMPLETED:
        return STATUS_WON
    if state == ProfileState.FAILED:
        return STATUS_LOST
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
    COL_NOTES,
    COL_AI_NOTES,
    COL_CREATED_AT,
    COL_LAST_SYNCED,
]

# Column index in the sheet (1-based for A1 notation, 0-based for list indexing).
HEADER_INDEX_0 = {h: i for i, h in enumerate(HEADERS)}
LINKEDIN_URL_COL_0 = HEADER_INDEX_0[COL_LINKEDIN_URL]


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


def get_worksheet() -> gspread.Worksheet:
    """Lazy-initialize the gspread worksheet. Auto-creates the tab + headers if missing."""
    global _WORKSHEET
    if _WORKSHEET is not None:
        return _WORKSHEET

    sheet_id, creds_path, tab_name = _require_config()
    try:
        creds = Credentials.from_service_account_file(
            creds_path,
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
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
    except WorksheetNotFound:
        ws = sh.add_worksheet(title=tab_name, rows=1000, cols=len(HEADERS))

    # Ensure headers row exists and matches expected schema.
    try:
        first_row = ws.row_values(1)
    except APIError as e:
        raise SheetsError(f"failed reading header row: {e}") from e

    if first_row != HEADERS:
        if not first_row:
            ws.update(values=[HEADERS], range_name="A1")
        else:
            # Header drift — caller should investigate. Don't overwrite silently
            # because that would shuffle the data columns underneath them.
            raise SheetsError(
                f"header row mismatch in tab '{tab_name}'. Expected {HEADERS}, "
                f"got {first_row}. Fix by hand or recreate the tab."
            )

    _WORKSHEET = ws
    return ws


def reset_client_cache() -> None:
    """Drop the cached worksheet handle. Useful in tests."""
    global _WORKSHEET
    _WORKSHEET = None


# ----------------------------------------------------------------------
# Row I/O — load all rows once per sync, then upsert by LinkedIn URL.
# ----------------------------------------------------------------------


class SheetIndex:
    """In-memory snapshot of the sheet, indexed by LinkedIn URL.

    Loaded once per sync (one API call), then reused for don't-downgrade
    lookups + row-id resolution. Any writes through `upsert_row` schedule
    an update — call `flush()` to push them in a single batch_update.
    """

    def __init__(self, ws: gspread.Worksheet, rows: list[list[str]]):
        self.ws = ws
        # rows[0] is the header row; data rows are rows[1:].
        self.rows = rows
        self.url_to_row_idx: dict[str, int] = {}
        for i, row in enumerate(rows[1:], start=2):  # 1-based, skip header
            url = (row[LINKEDIN_URL_COL_0] if len(row) > LINKEDIN_URL_COL_0 else "").strip()
            if url:
                self.url_to_row_idx[url] = i
        self._pending_updates: list[dict[str, Any]] = []
        self._pending_appends: list[list[str]] = []

    @classmethod
    def load(cls) -> "SheetIndex":
        ws = get_worksheet()
        try:
            values = ws.get_all_values()
        except APIError as e:
            raise SheetsError(f"failed loading sheet: {e}") from e
        if not values:
            values = [HEADERS]
        return cls(ws, values)

    def get_row(self, linkedin_url: str) -> dict[str, str] | None:
        """Return the row's column→value map, or None if not present."""
        idx = self.url_to_row_idx.get(linkedin_url)
        if idx is None:
            return None
        row = self.rows[idx - 1]
        return {h: (row[i] if i < len(row) else "") for i, h in enumerate(HEADERS)}

    def upsert_row(self, payload: dict[str, str]) -> tuple[bool, list[str]]:
        """Upsert a row by LinkedIn URL. Returns (was_new, changed_columns).

        Don't-downgrade rules apply for Outreach status and Stage. Other
        columns are overwritten with the payload value. The actual write
        is deferred — call flush() to commit the batch.
        """
        url = (payload.get(COL_LINKEDIN_URL) or "").strip()
        if not url:
            raise SheetsError(f"row payload missing LinkedIn URL: {payload}")

        existing = self.get_row(url)
        new_row: list[str] = ["" for _ in HEADERS]
        changed: list[str] = []

        if existing is None:
            # Brand-new row — fill in everything from payload.
            for col, val in payload.items():
                if col not in HEADER_INDEX_0:
                    continue
                new_row[HEADER_INDEX_0[col]] = val or ""
            self._pending_appends.append(new_row)
            return True, list(payload.keys())

        # Existing row — apply per-column overwrite rules.
        for col in HEADERS:
            current = existing.get(col, "") or ""
            target = payload.get(col, current) or ""

            if col == COL_OUTREACH_STATUS:
                if should_patch_outreach_status(current, target):
                    new_row[HEADER_INDEX_0[col]] = target
                    changed.append(col)
                else:
                    new_row[HEADER_INDEX_0[col]] = current
            elif col == COL_STAGE:
                if should_patch_stage(current, target):
                    new_row[HEADER_INDEX_0[col]] = target
                    changed.append(col)
                else:
                    new_row[HEADER_INDEX_0[col]] = current
            else:
                if col in payload and target != current:
                    new_row[HEADER_INDEX_0[col]] = target
                    changed.append(col)
                else:
                    new_row[HEADER_INDEX_0[col]] = current

        if not changed:
            return False, []

        # Schedule a row-level update (one A1 range per row).
        row_idx = self.url_to_row_idx[url]
        last_col_letter = _col_letter(len(HEADERS))
        self._pending_updates.append({
            "range": f"A{row_idx}:{last_col_letter}{row_idx}",
            "values": [new_row],
        })
        # Reflect locally so subsequent get_row sees the new values.
        self.rows[row_idx - 1] = new_row
        return False, changed

    def flush(self) -> dict[str, int]:
        """Commit all pending appends + updates. Returns counts."""
        ws = self.ws
        n_appended = 0
        n_updated = 0

        if self._pending_appends:
            try:
                ws.append_rows(
                    self._pending_appends,
                    value_input_option="RAW",
                    table_range="A1",
                )
            except APIError as e:
                raise SheetsError(f"failed appending {len(self._pending_appends)} rows: {e}") from e
            n_appended = len(self._pending_appends)
            # Refresh local index — appended rows now occupy row N+1, N+2, ...
            base = len(self.rows)
            for i, row in enumerate(self._pending_appends):
                self.rows.append(row)
                url = (row[LINKEDIN_URL_COL_0] if len(row) > LINKEDIN_URL_COL_0 else "").strip()
                if url:
                    self.url_to_row_idx[url] = base + i + 1
            self._pending_appends = []

        if self._pending_updates:
            try:
                ws.batch_update(self._pending_updates, value_input_option="RAW")
            except APIError as e:
                raise SheetsError(f"failed batch_update: {e}") from e
            n_updated = len(self._pending_updates)
            self._pending_updates = []

        return {"appended": n_appended, "updated": n_updated}


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

    All formatting (newline-joined emails, default Priority, ISO dates) is
    applied here so the caller sites stay simple. Empty Priority defaults
    to "Low" so the column never has blanks.
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
    final_priority = (priority or "").strip() or PRIORITY_DEFAULT

    return {
        COL_NAME: full_name,
        COL_FIRST_NAME: lead.first_name or "",
        COL_LAST_NAME: lead.last_name or "",
        COL_COMPANY: lead.company_name or "",
        COL_TITLE: (title or "").strip(),
        COL_LINKEDIN_URL: lead.linkedin_url or "",
        COL_EMAILS: "\n".join(cleaned_emails),
        COL_OUTREACH_STATUS: outreach_status or "",
        COL_STAGE: stage or "",
        COL_PRIORITY: final_priority,
        COL_PRIMARY_LOCATION: (primary_location or "").strip(),
        COL_NOTES: (notes or "").strip(),
        COL_AI_NOTES: (ai_notes or "").strip(),
        COL_CREATED_AT: created_at,
        COL_LAST_SYNCED: last_synced,
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
