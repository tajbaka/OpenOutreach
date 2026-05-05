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
        "Priority", "Primary location", "Notes", "AI Notes",
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

    def append_rows(self, rows, value_input_option=None, table_range=None):
        self.appended_batches.append(list(rows))

    def batch_update(self, updates, value_input_option=None):
        self.batch_updates.append(list(updates))


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
