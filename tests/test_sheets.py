"""Unit tests for the Google Sheets sync module.

Network-free — all gspread calls are stubbed via a fake worksheet object
that captures pending appends/updates so we can assert the wire payload.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from linkedin.notifications import sheets


# ---------------------------------------------------------------------------
# Outreach status + Stage rank tables — same semantics as Airtable.
# ---------------------------------------------------------------------------


def test_outreach_rank_includes_all_active_statuses():
    expected = {
        sheets.STATUS_INVITE_SENT: 1,
        sheets.STATUS_CONNECTED: 2,
        sheets.STATUS_WAITING: 2.5,
        sheets.STATUS_REPLIED: 3,
        sheets.STATUS_WANTS_MEETING: 4,
        sheets.STATUS_MEETING_BOOKED: 5,
        sheets.STATUS_HAD_MEETING: 6,
        sheets.STATUS_MANUAL_FOLLOWUP: 6.5,
        sheets.STATUS_PROSPECTING_TO_CLOSE: 7,
        sheets.STATUS_WON: 8,
        sheets.STATUS_DONT_SEND: 9,
    }
    assert sheets.OUTREACH_RANK == expected


def test_should_patch_outreach_status_blocks_demotion():
    assert sheets.should_patch_outreach_status(
        sheets.STATUS_HAD_MEETING, sheets.STATUS_REPLIED,
    ) is False


def test_should_patch_outreach_status_won_overrides():
    assert sheets.should_patch_outreach_status(
        sheets.STATUS_HAD_MEETING, sheets.STATUS_WON,
    ) is True


def test_should_patch_outreach_status_dont_send_is_sticky():
    assert sheets.should_patch_outreach_status(
        sheets.STATUS_DONT_SEND, sheets.STATUS_REPLIED,
    ) is False


def test_should_patch_stage_blocks_downgrade_from_meeting():
    assert sheets.should_patch_stage(
        sheets.STAGE_MEETING, sheets.STAGE_QUALIFICATION,
    ) is False


def test_aggregate_company_stage_won_wins():
    assert sheets.aggregate_company_stage(
        [sheets.STAGE_PROSPECTING, sheets.STAGE_WON, sheets.STAGE_LOST],
    ) == sheets.STAGE_WON


def test_aggregate_company_stage_all_lost_is_lost():
    assert sheets.aggregate_company_stage(
        [sheets.STAGE_LOST, sheets.STAGE_LOST],
    ) == sheets.STAGE_LOST


# ---------------------------------------------------------------------------
# Schema invariants
# ---------------------------------------------------------------------------


def test_headers_match_expected_schema():
    """If anyone reorders HEADERS, this is the canary."""
    assert sheets.HEADERS == [
        "Name", "First name", "Last name", "Company", "Title",
        "LinkedIn URL", "Email addresses", "Outreach status", "Stage",
        "Priority", "Primary location", "AI Notes", "Notes",
        "Created at", "Last synced",
    ]


def test_linkedin_url_is_natural_key_column():
    """If column position changes, SheetIndex's URL lookup breaks."""
    assert sheets.HEADERS.index(sheets.COL_LINKEDIN_URL) == sheets.LINKEDIN_URL_COL_0


def test_col_letter_handles_aa_overflow():
    assert sheets._col_letter(1) == "A"
    assert sheets._col_letter(15) == "O"
    assert sheets._col_letter(26) == "Z"
    assert sheets._col_letter(27) == "AA"


# ---------------------------------------------------------------------------
# Followups schema invariants
# ---------------------------------------------------------------------------


def test_fu_headers_match_expected_schema():
    """Locks the followups tab layout. Reordering breaks every saved
    operator-edit and downstream column-index lookups."""
    assert sheets.FU_HEADERS == [
        "Name", "Status", "State", "ROLE", "PRIORITY",
        "Days since", "Days since connection", "CONVO",
        "Draft Email", "Email Link", "Sent Email (manual toggle)",
        "Draft LinkedIn", "LinkedIn Message Url", "Sent LinkedIn (manual toggle)",
        "Qualify/Disqualify",
    ]


def test_qualify_column_is_last():
    """Operator-facing rule: the Qualify/Disqualify dropdown is on the
    far right so it doesn't compete with draft-review activity."""
    assert sheets.FU_HEADERS[-1] == sheets.FU_COL_QUALIFY


def test_followup_cell_defaults_qualify_to_qualify():
    """Fresh rows default Qualify so the operator only has to flip the
    rare Disqualify ones — not click into every cell."""
    assert sheets._followup_cell({}, sheets.FU_COL_QUALIFY) == "Qualify"


def test_followup_cell_normalizes_disqualify_variants():
    """Stale payloads might carry 'disqualified' (past-tense) or other
    casings; coerce to the dropdown vocabulary so data validation accepts."""
    for v in ["Disqualify", "disqualify", "DISQUALIFIED", "disqualified"]:
        assert sheets._followup_cell(
            {sheets.FU_COL_QUALIFY: v}, sheets.FU_COL_QUALIFY,
        ) == "Disqualify"


def test_followup_cell_unknown_qualify_value_falls_back_to_qualify():
    """Anything that doesn't start with 'disq' lands as Qualify — safer
    default; a typo doesn't accidentally suppress a lead from drafts."""
    assert sheets._followup_cell(
        {sheets.FU_COL_QUALIFY: "active"}, sheets.FU_COL_QUALIFY,
    ) == "Qualify"


def test_fu_role_to_icp_covers_every_role():
    """Every workflow ROLE must map to an ICP — the drafter assumes total."""
    for r in sheets.FU_ROLES:
        assert r in sheets.FU_ROLE_TO_ICP, f"ROLE {r} missing ICP mapping"


def test_followup_cell_defaults_sent_to_no():
    assert sheets._followup_cell({}, sheets.FU_COL_SENT_EMAIL) == "No"
    assert sheets._followup_cell({}, sheets.FU_COL_SENT_LINKEDIN) == "No"


def test_followup_cell_normalizes_legacy_truthy_to_yes():
    """A row payload that uses TRUE/True/Yes (any of the legacy boolean
    flavors) should land as the 'Yes' dropdown value."""
    for v in ["TRUE", True, "Yes", "y"]:
        assert sheets._followup_cell({sheets.FU_COL_SENT_EMAIL: v},
                                     sheets.FU_COL_SENT_EMAIL) == "Yes"


def test_followup_cell_passthrough_for_other_columns():
    """Non-Sent columns just stringify the value — including HYPERLINK formulas."""
    assert sheets._followup_cell({sheets.FU_COL_DRAFT_EMAIL: "Hi Sarah"},
                                 sheets.FU_COL_DRAFT_EMAIL) == "Hi Sarah"
    assert sheets._followup_cell({sheets.FU_COL_EMAIL_LINK: '=HYPERLINK("u","d")'},
                                 sheets.FU_COL_EMAIL_LINK) == '=HYPERLINK("u","d")'


def test_followup_section_key_routes_post_meeting_by_status():
    row = {
        sheets.FU_COL_STATUS: sheets.STATUS_HAD_MEETING,
        sheets.FU_COL_STATE: sheets.STATE_BALL_ON_US,
    }
    assert sheets._followup_section_key(row) == sheets.SECTION_MET


def test_followup_section_key_routes_pre_meeting_by_status():
    row = {
        sheets.FU_COL_STATUS: sheets.STATUS_MEETING_BOOKED,
        sheets.FU_COL_STATE: sheets.STATE_BALL_ON_THEM,
    }
    assert sheets._followup_section_key(row) == sheets.SECTION_SCHEDULING


def test_followup_section_key_routes_reply_lane_by_cohort():
    row = {
        sheets.FU_COL_STATUS: sheets.STATUS_REPLIED,
        sheets.FU_COL_STATE: sheets.STATE_COLD_THREAD,
    }
    assert sheets._followup_section_key(row) == sheets.SECTION_REPLIED


def test_followup_section_key_routes_active_lane_by_cohort():
    row = {
        sheets.FU_COL_STATUS: sheets.STATUS_CONNECTED,
        sheets.FU_COL_STATE: sheets.STATE_ACTIVE_IN_FLIGHT_LEGACY,
    }
    assert sheets._followup_section_key(row) == sheets.SECTION_ACTIVE_IN_FLIGHT


def test_followup_section_key_routes_preserved_rows_to_sent_history():
    row = {
        sheets.FU_COL_STATUS: sheets.STATUS_REPLIED,
        sheets.FU_COL_STATE: sheets.STATE_BALL_ON_US,
    }
    assert sheets._followup_section_key(row, preserved_sent=True) == sheets.SECTION_SENT


# ---------------------------------------------------------------------------
# HYPERLINK formula construction
# ---------------------------------------------------------------------------


def test_linkedin_thread_url_strips_urn_prefix():
    out = sheets.linkedin_thread_url("urn:li:conv:2-XYZABC=")
    assert out == "https://www.linkedin.com/messaging/thread/2-XYZABC=/"


def test_linkedin_thread_url_accepts_bare_id():
    out = sheets.linkedin_thread_url("2-rawid")
    assert out == "https://www.linkedin.com/messaging/thread/2-rawid/"


def test_linkedin_thread_url_falls_back_to_profile():
    """No thread URN → return the profile URL the caller passed in so the
    column always resolves to *somewhere* sensible."""
    out = sheets.linkedin_thread_url(
        "", fallback_profile_url="https://www.linkedin.com/in/janedoe/",
    )
    assert out == "https://www.linkedin.com/in/janedoe/"


def test_hyperlink_formula_escapes_quotes():
    out = sheets.hyperlink_formula('https://x.com/?q="a"', 'go')
    # Both arguments are double-quote-escaped (Sheets formula rules) so a
    # rogue quote in either doesn't break the formula.
    assert out == '=HYPERLINK("https://x.com/?q=""a""","go")'


def test_hyperlink_formula_empty_url_yields_empty_cell():
    assert sheets.hyperlink_formula("", "click me") == ""


def test_email_search_hyperlink_url_encodes_email():
    out = sheets.email_search_hyperlink("Sarah.Lange@prescient.com")
    assert "https://mail.google.com/mail/u/0/#search/" in out
    # @ and . get percent-encoded; result reads back as searchable email.
    assert "Sarah.Lange%40prescient.com" in out
    # Display text is the raw email (case preserved).
    assert ',"Sarah.Lange@prescient.com")' in out


def test_email_search_hyperlink_empty_email_yields_empty_cell():
    assert sheets.email_search_hyperlink("") == ""


def test_linkedin_message_hyperlink_uses_profile_even_with_thread_id():
    """thread_external_id is accepted (back-compat) but ignored — the
    thread-URL path produced broken links for the Voyager URN format, so
    the column always resolves to the profile now."""
    out = sheets.linkedin_message_hyperlink(
        "urn:li:msg_conversation:(urn:li:fsd_profile:ABC,2-XYZ==)",
        profile_url="https://www.linkedin.com/in/jane/",
    )
    assert out == '=HYPERLINK("https://www.linkedin.com/in/jane/","Open in LinkedIn")'


def test_linkedin_message_hyperlink_falls_back_to_profile():
    out = sheets.linkedin_message_hyperlink(
        None, profile_url="https://www.linkedin.com/in/jane/",
    )
    assert out == '=HYPERLINK("https://www.linkedin.com/in/jane/","Open in LinkedIn")'


# ---------------------------------------------------------------------------
# Hidden-column run-coalescing
# ---------------------------------------------------------------------------


def test_coalesce_runs_collapses_contiguous_indices():
    """Operator hiding columns 1, 2, 3, 7 should compress to two ranges
    (cuts API request volume) — not four single-cell updateDimension ops."""
    assert sheets._coalesce_runs([1, 2, 3, 7]) == [(1, 4), (7, 8)]


def test_coalesce_runs_handles_empty_and_unsorted():
    assert sheets._coalesce_runs([]) == []
    # Out-of-order input should still group correctly.
    assert sheets._coalesce_runs([4, 2, 5, 1]) == [(1, 3), (4, 6)]


def test_write_icp_messages_tab_creates_and_overwrites(monkeypatch):
    sh = _FakeSpreadsheet()
    monkeypatch.setattr(sheets, "_gspread_client", lambda: sh)

    rows = [
        ["ICP", "Connect Message", "Followup Message"],
        ["CSPs", "hello", "follow up"],
    ]
    sheets.write_icp_messages_tab("Leili", rows)

    ws = sh.tabs["Leili ICP Messages"]
    assert ws.cleared is True
    assert ws.updated_values["range_name"] == "A1"
    assert ws.updated_values["values"] == rows


def test_read_icp_messages_tab_returns_raw_rows(monkeypatch):
    sh = _FakeSpreadsheet()
    ws = _FakeWorksheet()
    ws.rows = [
        ["ICP", "Connect Message", "Followup Message"],
        ["Advisors", "connect", "body"],
    ]
    sh.tabs["Leili ICP Messages"] = ws
    monkeypatch.setattr(sheets, "_gspread_client", lambda: sh)

    assert sheets.read_icp_messages_tab("Leili") == ws.rows


# ---------------------------------------------------------------------------
# Sent-row preservation across runs
# ---------------------------------------------------------------------------


def test_is_sent_recognizes_yes_flavors():
    assert sheets._is_sent("Yes")
    assert sheets._is_sent("YES")
    assert sheets._is_sent("y")
    # Legacy boolean flavor still recognized so a half-migrated tab works.
    assert sheets._is_sent("TRUE")
    assert sheets._is_sent("✓")


def test_is_sent_rejects_no_and_blank():
    assert not sheets._is_sent("No")
    assert not sheets._is_sent("")
    assert not sheets._is_sent("MAYBE")


# ---------------------------------------------------------------------------
# build_row_payload — formatting + defaults
# ---------------------------------------------------------------------------


def _make_lead(**kw):
    defaults = dict(
        first_name="Jane",
        last_name="Doe",
        company_name="Acme Inc",
        linkedin_url="https://www.linkedin.com/in/janedoe/",
        email="",
        creation_date=datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc),
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def test_build_row_payload_dedupes_emails_and_joins_with_newline():
    payload = sheets.build_row_payload(
        lead=_make_lead(),
        title="CTO",
        emails=["a@x.com", "a@x.com", " ", "b@y.com"],
        outreach_status=sheets.STATUS_REPLIED,
        stage=sheets.STAGE_QUALIFICATION,
        priority="",
        primary_location="",
        notes="",
        ai_notes="",
        last_synced="2026-05-05",
    )
    assert payload[sheets.COL_EMAILS] == "a@x.com\nb@y.com"


def test_build_row_payload_defaults_empty_priority_to_low():
    payload = sheets.build_row_payload(
        lead=_make_lead(),
        title="", emails=[], outreach_status="", stage="",
        priority="", primary_location="", notes="", ai_notes="",
        last_synced="",
    )
    assert payload[sheets.COL_PRIORITY] == sheets.PRIORITY_LOW


def test_build_row_payload_preserves_explicit_priority():
    payload = sheets.build_row_payload(
        lead=_make_lead(),
        title="", emails=[], outreach_status="", stage="",
        priority=sheets.PRIORITY_HIGH, primary_location="", notes="", ai_notes="",
        last_synced="",
    )
    assert payload[sheets.COL_PRIORITY] == sheets.PRIORITY_HIGH


def test_build_row_payload_serializes_created_at_as_iso_date():
    payload = sheets.build_row_payload(
        lead=_make_lead(creation_date=datetime(2025, 12, 31, 9, 30, tzinfo=timezone.utc)),
        title="", emails=[], outreach_status="", stage="",
        priority="Low", primary_location="", notes="", ai_notes="",
        last_synced="",
    )
    assert payload[sheets.COL_CREATED_AT] == "2025-12-31"


# ---------------------------------------------------------------------------
# SheetIndex — append + update + don't-downgrade enforcement
# ---------------------------------------------------------------------------


class _FakeWorksheet:
    """Minimal stand-in for a gspread Worksheet — captures writes."""
    def __init__(self):
        self.appended_batches: list[list[list[str]]] = []
        self.batch_updates: list[list[dict]] = []
        self.cleared = False
        self.updated_values = None
        self.rows: list[list[str]] = []

    def append_rows(self, rows, value_input_option=None, table_range=None):
        self.appended_batches.append(list(rows))

    def batch_update(self, updates, value_input_option=None):
        self.batch_updates.append(list(updates))

    def clear(self):
        self.cleared = True

    def update(self, values=None, range_name=None, value_input_option=None):
        self.updated_values = {
            "values": values,
            "range_name": range_name,
            "value_input_option": value_input_option,
        }
        self.rows = list(values or [])

    def get_all_values(self):
        return self.rows


class _FakeSpreadsheet:
    def __init__(self):
        self.tabs: dict[str, _FakeWorksheet] = {}

    def worksheet(self, title):
        if title not in self.tabs:
            raise sheets.WorksheetNotFound(title)
        return self.tabs[title]

    def add_worksheet(self, title, rows, cols):
        ws = _FakeWorksheet()
        self.tabs[title] = ws
        return ws


def _index_with_existing_row(**overrides):
    """Build a SheetIndex pre-loaded with one existing row for jane@."""
    fields = {h: "" for h in sheets.HEADERS}
    fields.update({
        sheets.COL_NAME: "Jane Doe",
        sheets.COL_FIRST_NAME: "Jane",
        sheets.COL_LAST_NAME: "Doe",
        sheets.COL_COMPANY: "Acme Inc",
        sheets.COL_LINKEDIN_URL: "https://www.linkedin.com/in/janedoe/",
        sheets.COL_OUTREACH_STATUS: sheets.STATUS_HAD_MEETING,
        sheets.COL_STAGE: sheets.STAGE_MEETING,
        sheets.COL_PRIORITY: sheets.PRIORITY_LOW,
    })
    fields.update(overrides)
    rows = [list(sheets.HEADERS), [fields[h] for h in sheets.HEADERS]]
    return sheets.SheetIndex(_FakeWorksheet(), rows)


def test_upsert_row_appends_new_row():
    idx = sheets.SheetIndex(_FakeWorksheet(), [list(sheets.HEADERS)])
    payload = sheets.build_row_payload(
        lead=_make_lead(),
        title="CTO", emails=["a@x.com"], outreach_status=sheets.STATUS_REPLIED,
        stage=sheets.STAGE_QUALIFICATION, priority="", primary_location="",
        notes="", ai_notes="", last_synced="2026-05-05",
    )
    was_new, changed = idx.upsert_row(payload)
    assert was_new is True
    assert sheets.COL_OUTREACH_STATUS in changed

    counts = idx.flush()
    assert counts == {"appended": 1, "updated": 0}
    assert len(idx.ws.appended_batches) == 1
    appended_row = idx.ws.appended_batches[0][0]
    assert appended_row[sheets.HEADER_INDEX_0[sheets.COL_LINKEDIN_URL]] == \
        "https://www.linkedin.com/in/janedoe/"


def test_upsert_row_blocks_outreach_status_downgrade():
    """Existing 'Had Meeting' must NOT be overwritten by auto-derived 'Replied'."""
    idx = _index_with_existing_row()
    payload = sheets.build_row_payload(
        lead=_make_lead(),
        title="", emails=[], outreach_status=sheets.STATUS_REPLIED,
        stage=sheets.STAGE_QUALIFICATION, priority="", primary_location="",
        notes="", ai_notes="", last_synced="2026-05-05",
    )
    was_new, changed = idx.upsert_row(payload)
    assert was_new is False
    assert sheets.COL_OUTREACH_STATUS not in changed
    # The status cell in the staged update should be preserved as Had Meeting.
    if idx._pending_updates:
        new_row = idx._pending_updates[0]["values"][0]
        assert new_row[sheets.HEADER_INDEX_0[sheets.COL_OUTREACH_STATUS]] == \
            sheets.STATUS_HAD_MEETING


def test_upsert_row_blocks_stage_downgrade_from_meeting_to_qualification():
    idx = _index_with_existing_row()
    payload = sheets.build_row_payload(
        lead=_make_lead(),
        title="", emails=[], outreach_status=sheets.STATUS_HAD_MEETING,
        stage=sheets.STAGE_QUALIFICATION, priority="", primary_location="",
        notes="", ai_notes="", last_synced="2026-05-05",
    )
    was_new, changed = idx.upsert_row(payload)
    assert was_new is False
    assert sheets.COL_STAGE not in changed


def test_upsert_row_overwrites_notes_field():
    """Notes is sheet-wins on subsequent syncs but a fresh payload still writes."""
    idx = _index_with_existing_row(**{sheets.COL_NOTES: "old notes"})
    payload = sheets.build_row_payload(
        lead=_make_lead(),
        title="", emails=[], outreach_status=sheets.STATUS_HAD_MEETING,
        stage=sheets.STAGE_MEETING, priority=sheets.PRIORITY_LOW,
        primary_location="", notes="new notes from attio", ai_notes="",
        last_synced="2026-05-05",
    )
    was_new, changed = idx.upsert_row(payload)
    assert was_new is False
    assert sheets.COL_NOTES in changed
    new_row = idx._pending_updates[0]["values"][0]
    assert new_row[sheets.HEADER_INDEX_0[sheets.COL_NOTES]] == "new notes from attio"


def test_upsert_row_no_op_when_payload_matches_existing():
    idx = _index_with_existing_row(**{
        sheets.COL_EMAILS: "a@x.com",
        sheets.COL_TITLE: "CTO",
        sheets.COL_NOTES: "n",
        sheets.COL_AI_NOTES: "ai",
        sheets.COL_PRIMARY_LOCATION: "Toronto",
        sheets.COL_CREATED_AT: "2026-04-01",
        sheets.COL_LAST_SYNCED: "2026-05-05",
    })
    payload = sheets.build_row_payload(
        lead=_make_lead(),
        title="CTO", emails=["a@x.com"], outreach_status=sheets.STATUS_HAD_MEETING,
        stage=sheets.STAGE_MEETING, priority=sheets.PRIORITY_LOW,
        primary_location="Toronto", notes="n", ai_notes="ai",
        last_synced="2026-05-05",
    )
    was_new, changed = idx.upsert_row(payload)
    assert was_new is False
    assert changed == []
    counts = idx.flush()
    assert counts == {"appended": 0, "updated": 0}


def test_upsert_row_preserves_operator_added_columns():
    """An unknown column inserted into the sheet (e.g. 'Apollo Email') is
    addressed by name — its value passes through untouched on update."""
    apollo_pos = sheets.HEADERS.index(sheets.COL_AI_NOTES)  # insert before AI Notes
    actual_headers = (
        list(sheets.HEADERS[:apollo_pos])
        + ["Apollo Email"]
        + list(sheets.HEADERS[apollo_pos:])
    )
    fields = {h: "" for h in sheets.HEADERS}
    fields.update({
        sheets.COL_LINKEDIN_URL: "https://www.linkedin.com/in/janedoe/",
        sheets.COL_OUTREACH_STATUS: sheets.STATUS_CONNECTED,
        sheets.COL_STAGE: sheets.STAGE_PROSPECTING,
    })
    existing_row = []
    for h in actual_headers:
        existing_row.append("jane@apollo.example" if h == "Apollo Email" else fields[h])
    rows = [list(actual_headers), existing_row]
    idx = sheets.SheetIndex(_FakeWorksheet(), rows, actual_headers=actual_headers)

    payload = sheets.build_row_payload(
        lead=_make_lead(),
        title="CTO", emails=[], outreach_status=sheets.STATUS_REPLIED,
        stage=sheets.STAGE_QUALIFICATION, priority="", primary_location="",
        notes="", ai_notes="", last_synced="2026-05-05",
    )
    was_new, changed = idx.upsert_row(payload)
    assert was_new is False
    assert sheets.COL_OUTREACH_STATUS in changed

    new_row = idx._pending_updates[0]["values"][0]
    apollo_col_0 = actual_headers.index("Apollo Email")
    assert new_row[apollo_col_0] == "jane@apollo.example"
    # Update range must span the full live width, not just managed cols.
    expected_last = sheets._col_letter(len(actual_headers))
    assert idx._pending_updates[0]["range"] == f"A2:{expected_last}2"


def test_upsert_row_raises_when_linkedin_url_missing():
    idx = sheets.SheetIndex(_FakeWorksheet(), [list(sheets.HEADERS)])
    payload = sheets.build_row_payload(
        lead=_make_lead(linkedin_url=""),
        title="", emails=[], outreach_status="", stage="",
        priority="", primary_location="", notes="", ai_notes="",
        last_synced="",
    )
    with pytest.raises(sheets.SheetsError):
        idx.upsert_row(payload)
