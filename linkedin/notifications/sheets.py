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

Operator-managed columns (anything in the sheet not in HEADERS, e.g.
"Apollo Email") are left untouched on every write — addressing is by
column name against the live sheet header row, not by fixed position.

Failure mode: raises SheetsError on auth / API errors. Callers (sync_sheets)
decide whether to skip-and-continue or abort.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable

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
    COL_AI_NOTES,
    COL_NOTES,
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

    if not first_row:
        ws.update(values=[HEADERS], range_name="A1")
        first_row = list(HEADERS)
    else:
        # Subset check — every managed column must appear somewhere in the
        # sheet header. Position is irrelevant; operator-added columns
        # (e.g. "Apollo Email") are passthrough and live in between.
        missing = [h for h in HEADERS if h not in first_row]
        if missing:
            raise SheetsError(
                f"missing managed columns in tab '{tab_name}': {missing}. "
                f"Got: {first_row}. Add them or recreate the tab."
            )

    # Truncate overflow at the column edge instead of bleeding into adjacent
    # cells. Idempotent — Sheets stores it as the cell's wrap strategy.
    last_col = _col_letter(max(len(first_row), len(HEADERS)))
    try:
        ws.format(f"A:{last_col}", {"wrapStrategy": "CLIP"})
    except APIError as e:
        logger.warning("failed applying CLIP wrap strategy: %s", e)

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

    def __init__(
        self,
        ws: gspread.Worksheet,
        rows: list[list[str]],
        actual_headers: list[str] | None = None,
    ):
        self.ws = ws
        # rows[0] is the header row; data rows are rows[1:].
        self.rows = rows
        # Live sheet headers (may contain operator-added columns like
        # "Apollo Email"). Defaults to rows[0] for back-compat with tests
        # that hand-build a SheetIndex from `[HEADERS, ...]`.
        if actual_headers is not None:
            self.actual_headers = list(actual_headers)
        elif rows:
            self.actual_headers = list(rows[0])
        else:
            self.actual_headers = list(HEADERS)
        self.actual_index_0: dict[str, int] = {
            h: i for i, h in enumerate(self.actual_headers)
        }
        # Sanity: every managed column must be addressable.
        missing = [h for h in HEADERS if h not in self.actual_index_0]
        if missing:
            raise SheetsError(f"sheet header missing managed columns: {missing}")
        self.url_col_0 = self.actual_index_0[COL_LINKEDIN_URL]

        self.url_to_row_idx: dict[str, int] = {}
        for i, row in enumerate(rows[1:], start=2):  # 1-based, skip header
            url = (row[self.url_col_0] if len(row) > self.url_col_0 else "").strip()
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
            values = [list(HEADERS)]
        return cls(ws, values, actual_headers=list(values[0]))

    def get_row(self, linkedin_url: str) -> dict[str, str] | None:
        """Return the row's managed-column → value map, or None if not present."""
        idx = self.url_to_row_idx.get(linkedin_url)
        if idx is None:
            return None
        row = self.rows[idx - 1]
        out: dict[str, str] = {}
        for h in HEADERS:
            pos = self.actual_index_0[h]
            out[h] = row[pos] if pos < len(row) else ""
        return out

    def upsert_row(self, payload: dict[str, str]) -> tuple[bool, list[str]]:
        """Upsert a row by LinkedIn URL. Returns (was_new, changed_columns).

        Don't-downgrade rules apply for Outreach status and Stage. Other
        managed columns are overwritten with the payload value. Columns
        present in the live sheet but absent from HEADERS (operator-added,
        e.g. "Apollo Email") are preserved verbatim. The actual write is
        deferred — call flush() to commit the batch.
        """
        url = (payload.get(COL_LINKEDIN_URL) or "").strip()
        if not url:
            raise SheetsError(f"row payload missing LinkedIn URL: {payload}")

        existing = self.get_row(url)
        new_row: list[str] = ["" for _ in self.actual_headers]
        changed: list[str] = []

        if existing is None:
            # Brand-new row — fill in only managed columns from payload.
            # Operator-added columns stay blank; the operator fills them.
            for col, val in payload.items():
                if col not in self.actual_index_0:
                    continue
                new_row[self.actual_index_0[col]] = val or ""
            self._pending_appends.append(new_row)
            return True, list(payload.keys())

        # Existing row — start from the live row so unknown columns
        # (Apollo Email etc.) pass through untouched, then apply rules
        # per managed column.
        row_idx = self.url_to_row_idx[url]
        existing_row = self.rows[row_idx - 1]
        for i in range(len(self.actual_headers)):
            new_row[i] = existing_row[i] if i < len(existing_row) else ""

        for col in HEADERS:
            pos = self.actual_index_0[col]
            current = existing.get(col, "") or ""
            target = payload.get(col, current) or ""

            if col == COL_OUTREACH_STATUS:
                if should_patch_outreach_status(current, target):
                    new_row[pos] = target
                    changed.append(col)
            elif col == COL_STAGE:
                if should_patch_stage(current, target):
                    new_row[pos] = target
                    changed.append(col)
            else:
                if col in payload and target != current:
                    new_row[pos] = target
                    changed.append(col)

        if not changed:
            return False, []

        # Schedule a row-level update — span the full live width so we
        # don't shift columns or truncate the operator-managed tail.
        last_col_letter = _col_letter(len(self.actual_headers))
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
                url = (row[self.url_col_0] if len(row) > self.url_col_0 else "").strip()
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


# ======================================================================
# Followups tabs (per-operator)
#
# A second surface in the same spreadsheet for the daily Claude follow-up
# generation workflow (see docs/followup-generation-workflow.md). Two tabs:
# `<Operator> - Followups`. Each tab has:
#   - Frozen header row (14 columns)
#   - Section dividers per cohort (Met / Replied / Connected, no reply /
#     Active in-flight / Sent)
#   - One row per Lead within each section
#   - Sent? checkbox column the operator ticks after dispatching
#
# `write_followups()` is the entry point used by the Claude task at Phase 6.
# It preserves rows where Sent? = TRUE so history persists across daily runs.
# ======================================================================


# Cohort labels used by the Claude task's classifier. Stored verbatim in
# the Cohort column so per-cohort filtering / sorting works in the sheet.
COHORT_MET = "Met"
COHORT_SCHEDULING = "Scheduling"  # pre-meeting — Wants Meeting / Meeting Booked
COHORT_BALL_ON_US = "Ball on us"
COHORT_COLD_THREAD = "Cold thread"
COHORT_ACTIVE_IN_FLIGHT = "Active in-flight"
COHORT_SENT = "Sent"

# Section ordering within each tab. Each section bundles one or more
# cohorts; the Cohort column distinguishes them within a section.
# Order = operator scan priority (top = most time-sensitive revenue).
#
# Note: the "Connected, no reply" cohort was removed 2026-05-12. The
# daemon now handles those leads programmatically via
# `linkedin/tasks/follow_up.py` + the rigid ICP templates in
# `linkedin/icp_messages.json` — surfacing them in the Followups tab
# for manual drafting was redundant work the operator no longer needs.
# Cohorts left in this list are conversation-driven (Met / Scheduling /
# Replied / Active in-flight / Sent).
FU_SECTIONS = [
    ("🤝 MET",                  [COHORT_MET]),
    ("📅 SCHEDULING",           [COHORT_SCHEDULING]),
    ("💬 REPLIED",              [COHORT_BALL_ON_US, COHORT_COLD_THREAD]),
    ("🌊 ACTIVE IN-FLIGHT",     [COHORT_ACTIVE_IN_FLIGHT]),
    ("✅ SENT",                 [COHORT_SENT]),
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
FU_COL_COHORT = "Cohort"
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
    FU_COL_NAME, FU_COL_STATUS, FU_COL_COHORT, FU_COL_ROLE,
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
FU_COHORT_COL_0 = FU_HEADER_INDEX_0[FU_COL_COHORT]
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
LEAD_ICP_BUCKETS = ("CSPs", "3PAOs/Assessors", "Advisors", "Channel")

# Operator's Sales Nav search labels (the values in the merged CSV's
# `ICP` column) normalized to the persisted vocab. `add_seeds` applies
# this map at import so a single Lead.icp value drives all downstream
# template routing. Keys are case-insensitive — see
# `linkedin.setup.seeds._normalize_csv_icp`.
CSV_ICP_TO_LEAD_ICP = {
    "csps":             "CSPs",
    "advisors":         "Advisors",
    "channel":          "Channel",
    "firms-advisors":   "Advisors",
    "grc-advisors":     "Advisors",
    "vciso-advisors":   "Advisors",
    "3paos":            "3PAOs/Assessors",
    "3paos/assessors":  "3PAOs/Assessors",
    "assessors":        "3PAOs/Assessors",
}


def _followup_tab_name(operator: str) -> str:
    return f"{operator} - Followups"


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
    surfaced under a `✅ SENT` section at the bottom — the Claude task is
    expected to also exclude their LinkedIn URLs from `rows_by_operator` so
    they don't get re-drafted on top of themselves. Any LinkedIn URL appearing
    in BOTH the preserved Sent set AND the fresh payload will keep the new
    payload (caller's data wins — the Claude task explicitly chose to redraft).

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
        # Force preserved rows into the Sent cohort.
        for p in preserved:
            p[FU_COL_COHORT] = COHORT_SENT

        # 2. Drop and recreate the tab so we control layout fully.
        title = operator_titles[operator]
        for w in sh.worksheets():
            if w.title == title:
                sh.del_worksheet(w)
                break
        ws = sh.add_worksheet(title=title, rows=400, cols=len(FU_HEADERS))
        sheet_id = ws.id

        # 3. Group all rows (fresh + preserved) by section.
        by_cohort: dict[str, list[dict]] = {}
        for r in fresh_rows:
            by_cohort.setdefault(r.get(FU_COL_COHORT) or "", []).append(r)
        for r in preserved:
            by_cohort.setdefault(COHORT_SENT, []).append(r)

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
        for label, cohorts in FU_SECTIONS:
            section_rows = [r for c in cohorts for r in by_cohort.get(c, [])]
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
            (FU_COHORT_COL_0,
             [c for _, cohs in FU_SECTIONS for c in cohs], False),
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

        # Conditional formatting on Cohort column.
        cohort_text_colors = [
            (COHORT_MET,              _rgb255(13, 122, 42),   True),
            (COHORT_BALL_ON_US,       _rgb255(204, 17, 34),   True),    # red — most urgent
            (COHORT_COLD_THREAD,      _rgb255(221, 102, 34),  False),   # orange
            (COHORT_ACTIVE_IN_FLIGHT, _rgb255(0, 102, 204),   False),   # blue
            (COHORT_SENT,             _rgb255(108, 117, 125), False),   # gray
        ]
        for value, fg, bold in cohort_text_colors:
            requests.append(_text_cond_rule(
                sheet_id, FU_COHORT_COL_0, value, fg, bold,
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
