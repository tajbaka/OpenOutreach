"""Unit tests for the Google Sheets sync module.

Network-free — all gspread calls are stubbed via a fake worksheet object
that captures pending appends/updates so we can assert the wire payload.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from linkedin.notifications import crm_sheets, sheets


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


def test_deal_mapping_uses_annotated_meeting_flag_without_querying():
    from linkedin.enums import ProfileState

    deal = SimpleNamespace(
        state=ProfileState.COMPLETED,
        lead_id=123,
        last_reply_at=None,
        has_meeting=True,
    )

    assert sheets.deal_to_stage(deal) == sheets.STAGE_MEETING
    assert sheets.deal_to_outreach_status(deal) == sheets.STATUS_HAD_MEETING


# ---------------------------------------------------------------------------
# Schema invariants
# ---------------------------------------------------------------------------


def test_headers_match_expected_schema():
    """If anyone reorders HEADERS, this is the canary."""
    assert sheets.HEADERS == [
        "Name", "First name", "Last name", "Company", "Title",
        "LinkedIn URL", "Email addresses", "Outreach status", "Stage",
        "Priority", "Primary location", "AI Notes", "Notes",
        "Created at", "Last synced", "Lead ID",
    ]


def test_linkedin_url_is_natural_key_column():
    """If column position changes, SheetIndex's URL lookup breaks."""
    assert sheets.HEADERS.index(sheets.COL_LINKEDIN_URL) == sheets.LINKEDIN_URL_COL_0


@pytest.mark.parametrize(
    "variant",
    [
        "https://www.linkedin.com/in/Jane-Doe/",
        "http://linkedin.com/in/jane-doe",
        "https://ca.linkedin.com/in/JANE-DOE/?trk=people-guest#profile",
        "www.linkedin.com/in/jane-doe/details/contact-info/",
    ],
)
def test_canonical_linkedin_url_collapses_profile_variants(variant):
    assert sheets.canonical_linkedin_url(variant) == (
        "https://www.linkedin.com/in/jane-doe/"
    )


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
        pk=123,
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


def test_build_row_payload_preserves_empty_human_priority():
    payload = sheets.build_row_payload(
        lead=_make_lead(),
        title="", emails=[], outreach_status="", stage="",
        priority="", primary_location="", notes="", ai_notes="",
        last_synced="",
    )
    assert payload[sheets.COL_PRIORITY] == ""


def test_build_row_payload_preserves_explicit_priority():
    payload = sheets.build_row_payload(
        lead=_make_lead(),
        title="", emails=[], outreach_status="", stage="",
        priority=sheets.PRIORITY_HIGH, primary_location="", notes="", ai_notes="",
        last_synced="",
    )
    assert payload[sheets.COL_PRIORITY] == sheets.PRIORITY_HIGH


def test_build_row_payload_preserves_human_text_exactly():
    payload = sheets.build_row_payload(
        lead=_make_lead(),
        title="  CTO  ", emails=[], outreach_status="", stage="",
        priority="", primary_location=" Toronto ", notes="  note\n",
        ai_notes="  ai note  ", last_synced="",
    )

    assert payload[sheets.COL_TITLE] == "  CTO  "
    assert payload[sheets.COL_PRIMARY_LOCATION] == " Toronto "
    assert payload[sheets.COL_NOTES] == "  note\n"
    assert payload[sheets.COL_AI_NOTES] == "  ai note  "


def test_build_row_payload_serializes_created_at_as_iso_date():
    payload = sheets.build_row_payload(
        lead=_make_lead(creation_date=datetime(2025, 12, 31, 9, 30, tzinfo=timezone.utc)),
        title="", emails=[], outreach_status="", stage="",
        priority="Low", primary_location="", notes="", ai_notes="",
        last_synced="",
    )
    assert payload[sheets.COL_CREATED_AT] == "2025-12-31"


def test_last_synced_alone_does_not_dirty_existing_row():
    ws = _FakeWorksheet()
    existing = {header: "" for header in sheets.HEADERS}
    existing.update({
        sheets.COL_NAME: "Jane Doe",
        sheets.COL_LINKEDIN_URL: "https://www.linkedin.com/in/janedoe/",
        sheets.COL_LAST_SYNCED: "2026-08-08",
    })
    idx = sheets.SheetIndex(
        ws=ws,
        actual_headers=list(sheets.HEADERS),
        rows=[list(sheets.HEADERS), [existing[h] for h in sheets.HEADERS]],
    )
    payload = dict(existing)
    payload[sheets.COL_LAST_SYNCED] = "2026-08-09"

    was_new, changed = idx.upsert_row(payload)

    assert was_new is False
    assert changed == []


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
        self.col_count = len(sheets.HEADERS)
        self.added_cols = 0
        self.formats: list[tuple[str, dict]] = []
        self.get_all_values_options: list[dict] = []
        self.batch_get_options: list[dict] = []

    def append_rows(self, rows, value_input_option=None, table_range=None):
        self.appended_batches.append(list(rows))

    def batch_update(self, updates, value_input_option=None):
        self.batch_updates.append(list(updates))

    def batch_get(self, ranges, value_render_option=None):
        self.batch_get_options.append({
            "ranges": list(ranges),
            "value_render_option": value_render_option,
        })
        values = []
        for cell_range in ranges:
            start, end = str(cell_range).split(":", 1)
            start = start.replace("$", "")
            end = end.replace("$", "")
            letters = "".join(
                character for character in start if character.isalpha()
            )
            digits = "".join(
                character for character in start if character.isdigit()
            )
            column = 0
            for character in letters.upper():
                column = column * 26 + ord(character) - ord("A") + 1
            start_row = int(digits)
            end_digits = "".join(
                character for character in end if character.isdigit()
            )
            end_row = int(end_digits) if end_digits else len(self.rows)
            column_values = []
            for row in range(start_row, end_row + 1):
                value = ""
                if (
                    0 < row <= len(self.rows)
                    and 0 < column <= len(self.rows[row - 1])
                ):
                    value = self.rows[row - 1][column - 1]
                column_values.append([value] if value != "" else [])
            while column_values and not column_values[-1]:
                column_values.pop()
            values.append(column_values)
        return values

    def clear(self):
        self.cleared = True

    def update(self, values=None, range_name=None, value_input_option=None):
        self.updated_values = {
            "values": values,
            "range_name": range_name,
            "value_input_option": value_input_option,
        }
        self.rows = list(values or [])

    def get_all_values(self, **kwargs):
        self.get_all_values_options.append(kwargs)
        return self.rows

    def row_values(self, row):
        return list(self.rows[row - 1]) if len(self.rows) >= row else []

    def add_cols(self, count):
        self.added_cols += count
        self.col_count += count

    def format(self, cell_range, rule):
        self.formats.append((cell_range, rule))


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
        sheets.COL_LEAD_ID: "123",
    })
    fields.update(overrides)
    rows = [list(sheets.HEADERS), [fields[h] for h in sheets.HEADERS]]
    worksheet = _FakeWorksheet()
    worksheet.rows = [list(row) for row in rows]
    return sheets.SheetIndex(worksheet, rows)


def _index_matching_people_payload(*, outreach_status=sheets.STATUS_CONNECTED):
    payload = sheets.build_row_payload(
        lead=_make_lead(),
        title="",
        emails=[],
        outreach_status=outreach_status,
        stage=sheets.STAGE_PROSPECTING,
        priority="",
        primary_location="",
        notes="",
        ai_notes="",
        last_synced="2026-08-26",
    )
    rows = [
        list(sheets.HEADERS),
        [payload.get(header, "") for header in sheets.HEADERS],
    ]
    worksheet = _FakeWorksheet()
    worksheet.rows = [list(row) for row in rows]
    return sheets.SheetIndex(worksheet, rows), payload


def test_upsert_row_appends_new_row():
    worksheet = _FakeWorksheet()
    worksheet.rows = [list(sheets.HEADERS)]
    idx = sheets.SheetIndex(worksheet, [list(sheets.HEADERS)])
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
    assert appended_row[sheets.HEADER_INDEX_0[sheets.COL_LEAD_ID]] == "123"


@pytest.mark.parametrize(
    ("concurrent_lead_id", "concurrent_url", "conflict_column"),
    [
        (
            "123",
            "https://www.linkedin.com/in/someone-else/",
            sheets.COL_LEAD_ID,
        ),
        (
            "999",
            "https://ca.linkedin.com/in/JANEDOE/?trk=concurrent",
            sheets.COL_LINKEDIN_URL,
        ),
    ],
)
def test_flush_rejects_pending_append_identity_that_appeared_concurrently(
    concurrent_lead_id,
    concurrent_url,
    conflict_column,
):
    worksheet = _FakeWorksheet()
    worksheet.rows = [list(sheets.HEADERS)]
    idx = sheets.SheetIndex(worksheet, [list(sheets.HEADERS)])
    payload = sheets.build_row_payload(
        lead=_make_lead(),
        title="",
        emails=[],
        outreach_status=sheets.STATUS_CONNECTED,
        stage=sheets.STAGE_PROSPECTING,
        priority="",
        primary_location="",
        notes="",
        ai_notes="",
        last_synced="2026-08-26",
    )
    assert idx.upsert_row(payload)[0] is True

    concurrent = ["" for _header in sheets.HEADERS]
    concurrent[sheets.HEADER_INDEX_0[sheets.COL_NAME]] = "Different Person"
    concurrent[sheets.HEADER_INDEX_0[sheets.COL_LEAD_ID]] = concurrent_lead_id
    concurrent[sheets.HEADER_INDEX_0[sheets.COL_LINKEDIN_URL]] = concurrent_url
    worksheet.rows.append(concurrent)

    with pytest.raises(
        sheets.SheetsError,
        match=(
            "pending People append conflicts with live row 2, column "
            + repr(conflict_column)
        ),
    ):
        idx.flush()

    assert worksheet.batch_get_options == [{
        "ranges": ["P1:P", "F1:F"],
        "value_render_option": sheets.ValueRenderOption.unformatted,
    }]
    assert worksheet.appended_batches == []
    assert worksheet.batch_updates == []


def test_pending_append_identity_preflight_api_failure_prevents_all_writes():
    worksheet = _FakeWorksheet()
    worksheet.rows = [list(sheets.HEADERS)]
    idx = sheets.SheetIndex(worksheet, [list(sheets.HEADERS)])
    payload = sheets.build_row_payload(
        lead=_make_lead(),
        title="",
        emails=[],
        outreach_status=sheets.STATUS_CONNECTED,
        stage=sheets.STAGE_PROSPECTING,
        priority="",
        primary_location="",
        notes="",
        ai_notes="",
        last_synced="2026-08-26",
    )
    idx.upsert_row(payload)
    response = SimpleNamespace(
        json=lambda: {
            "error": {
                "code": 503,
                "message": "service unavailable",
                "status": "UNAVAILABLE",
            },
        },
        text="service unavailable",
    )

    def fail_preflight(*_args, **_kwargs):
        raise sheets.APIError(response)

    worksheet.batch_get = fail_preflight

    with pytest.raises(sheets.SheetsError, match="People appends"):
        idx.flush()

    assert worksheet.appended_batches == []
    assert worksheet.batch_updates == []


def test_pending_append_identity_preflight_does_not_match_by_name():
    worksheet = _FakeWorksheet()
    worksheet.rows = [list(sheets.HEADERS)]
    idx = sheets.SheetIndex(worksheet, [list(sheets.HEADERS)])
    payload = sheets.build_row_payload(
        lead=_make_lead(),
        title="",
        emails=[],
        outreach_status=sheets.STATUS_CONNECTED,
        stage=sheets.STAGE_PROSPECTING,
        priority="",
        primary_location="",
        notes="",
        ai_notes="",
        last_synced="2026-08-26",
    )
    idx.upsert_row(payload)

    concurrent = ["" for _header in sheets.HEADERS]
    concurrent[sheets.HEADER_INDEX_0[sheets.COL_NAME]] = "Jane Doe"
    concurrent[sheets.HEADER_INDEX_0[sheets.COL_LEAD_ID]] = "999"
    concurrent[sheets.HEADER_INDEX_0[sheets.COL_LINKEDIN_URL]] = (
        "https://www.linkedin.com/in/a-different-jane/"
    )
    worksheet.rows.append(concurrent)

    assert idx.flush() == {"appended": 1, "updated": 0}
    assert worksheet.batch_get_options == [{
        "ranges": ["P1:P", "F1:F"],
        "value_render_option": sheets.ValueRenderOption.unformatted,
    }]
    assert len(worksheet.appended_batches) == 1


def test_pending_append_fails_closed_if_identity_header_moved():
    worksheet = _FakeWorksheet()
    worksheet.rows = [list(sheets.HEADERS)]
    idx = sheets.SheetIndex(worksheet, [list(sheets.HEADERS)])
    payload = sheets.build_row_payload(
        lead=_make_lead(),
        title="",
        emails=[],
        outreach_status=sheets.STATUS_CONNECTED,
        stage=sheets.STAGE_PROSPECTING,
        priority="",
        primary_location="",
        notes="",
        ai_notes="",
        last_synced="2026-08-26",
    )
    idx.upsert_row(payload)
    worksheet.rows[0][sheets.HEADER_INDEX_0[sheets.COL_LEAD_ID]] = "Moved"

    with pytest.raises(
        sheets.SheetsError,
        match="People identity columns changed after planning",
    ):
        idx.flush()

    assert worksheet.appended_batches == []
    assert worksheet.batch_updates == []


def test_pending_append_is_indexed_and_deduped_within_same_run():
    idx = sheets.SheetIndex(_FakeWorksheet(), [list(sheets.HEADERS)])
    first = sheets.build_row_payload(
        lead=_make_lead(),
        title="Engineer", emails=[], outreach_status=sheets.STATUS_CONNECTED,
        stage=sheets.STAGE_PROSPECTING, priority="", primary_location="",
        notes="", ai_notes="", last_synced="2026-08-26",
    )
    second = dict(first)
    second[sheets.COL_TITLE] = "CTO"

    assert idx.upsert_row(first)[0] is True
    was_new, changed = idx.upsert_row(second)

    assert was_new is False
    assert changed == [sheets.COL_TITLE]
    assert idx.plan().appended_rows == 1
    assert idx.plan().updated_rows == 0
    assert idx._pending_appends[0][idx.actual_index_0[sheets.COL_TITLE]] == "CTO"


def test_sheet_index_normalizes_numeric_live_cells_before_planning():
    headers = list(sheets.HEADERS)
    row = ["" for _ in headers]
    row[headers.index(sheets.COL_LINKEDIN_URL)] = (
        "https://www.linkedin.com/in/numeric/"
    )
    row[headers.index(sheets.COL_LEAD_ID)] = 123
    row[headers.index(sheets.COL_OUTREACH_STATUS)] = 7
    idx = sheets.SheetIndex(
        _FakeWorksheet(),
        [headers, row],
        actual_headers=headers,
    )

    stored = idx.get_row(
        "https://www.linkedin.com/in/numeric/",
        lead_id=123,
    )
    assert stored[sheets.COL_LEAD_ID] == "123"
    assert stored[sheets.COL_OUTREACH_STATUS] == "7"

    idx.upsert_row(
        {
            sheets.COL_LINKEDIN_URL: "https://www.linkedin.com/in/numeric/",
            sheets.COL_LEAD_ID: "123",
            sheets.COL_OUTREACH_STATUS: sheets.STATUS_REPLIED,
        }
    )
    assert idx.plan().updated_cells == 1


def test_url_variant_fallback_backfills_stable_id_without_append():
    fields = {header: "" for header in sheets.HEADERS}
    fields.update({
        sheets.COL_LINKEDIN_URL: (
            "https://ca.linkedin.com/in/JANE-DOE/?trk=legacy"
        ),
        sheets.COL_NAME: "Jane Doe",
    })
    rows = [
        list(sheets.HEADERS),
        [fields[header] for header in sheets.HEADERS],
    ]
    idx = sheets.SheetIndex(_FakeWorksheet(), rows)
    payload = sheets.build_row_payload(
        lead=_make_lead(linkedin_url="http://linkedin.com/in/jane-doe"),
        title="", emails=[], outreach_status="", stage="", priority="",
        primary_location="", notes="", ai_notes="", last_synced="",
    )

    was_new, changed = idx.upsert_row(payload)

    assert was_new is False
    assert sheets.COL_LEAD_ID in changed
    assert idx.plan().appended_rows == 0
    assert idx.get_row(payload[sheets.COL_LINKEDIN_URL], lead_id=123)[
        sheets.COL_LEAD_ID
    ] == "123"


def test_url_variants_are_reported_as_one_duplicate_identity():
    fields = {header: "" for header in sheets.HEADERS}
    first = dict(fields)
    first[sheets.COL_LINKEDIN_URL] = "https://linkedin.com/in/Jane-Doe"
    second = dict(fields)
    second[sheets.COL_LINKEDIN_URL] = (
        "https://www.linkedin.com/in/jane-doe/?trk=duplicate"
    )
    idx = sheets.SheetIndex(
        _FakeWorksheet(),
        [
            list(sheets.HEADERS),
            [first[header] for header in sheets.HEADERS],
            [second[header] for header in sheets.HEADERS],
        ],
    )

    assert idx.duplicate_keys == (
        sheets.DuplicateSheetKey(
            sheets.COL_LINKEDIN_URL,
            "https://www.linkedin.com/in/jane-doe/",
            (2, 3),
        ),
    )


def test_duplicate_existing_url_is_reported_and_never_last_row_wins():
    fields = {header: "" for header in sheets.HEADERS}
    fields[sheets.COL_LINKEDIN_URL] = "https://www.linkedin.com/in/duplicate/"
    rows = [
        list(sheets.HEADERS),
        [fields[header] for header in sheets.HEADERS],
        [fields[header] for header in sheets.HEADERS],
    ]
    idx = sheets.SheetIndex(_FakeWorksheet(), rows)

    assert idx.duplicate_keys == (
        sheets.DuplicateSheetKey(
            sheets.COL_LINKEDIN_URL,
            "https://www.linkedin.com/in/duplicate/",
            (2, 3),
        ),
    )
    with pytest.raises(sheets.SheetsError, match="duplicate LinkedIn URL"):
        idx.get_row(
            "https://www.linkedin.com/in/duplicate/",
            lead_id=123,
        )


def test_stable_lead_id_can_resolve_one_row_inside_legacy_url_duplicates():
    fields = {header: "" for header in sheets.HEADERS}
    fields[sheets.COL_LINKEDIN_URL] = "https://www.linkedin.com/in/duplicate/"
    canonical = dict(fields)
    canonical[sheets.COL_LEAD_ID] = "123"
    rows = [
        list(sheets.HEADERS),
        [fields[header] for header in sheets.HEADERS],
        [canonical[header] for header in sheets.HEADERS],
    ]
    idx = sheets.SheetIndex(_FakeWorksheet(), rows)

    row = idx.get_row(
        "https://www.linkedin.com/in/duplicate/",
        lead_id=123,
    )

    assert row[sheets.COL_LEAD_ID] == "123"


def test_load_read_only_plans_additive_lead_id_header(monkeypatch):
    ws = _FakeWorksheet()
    ws.rows = [list(sheets.HEADERS[:-1])]
    monkeypatch.setattr(sheets, "get_worksheet", lambda *, apply_schema=True: ws)

    idx = sheets.SheetIndex.load(apply_schema=False)

    assert idx.plan().header_additions == (sheets.COL_LEAD_ID,)
    assert ws.updated_values is None
    assert ws.get_all_values_options == [
        {"value_render_option": sheets.ValueRenderOption.formula}
    ]


def test_get_worksheet_dry_run_authorizes_read_only_and_does_not_cache(monkeypatch):
    ws = _FakeWorksheet()
    ws.rows = [list(sheets.HEADERS)]
    spreadsheet = SimpleNamespace(worksheet=lambda _title: ws)
    client = SimpleNamespace(open_by_key=lambda _sheet_id: spreadsheet)
    captured = {}

    def fake_credentials(_path, *, scopes):
        captured["scopes"] = scopes
        return object()

    monkeypatch.setattr(sheets, "GOOGLE_SHEETS_ID", "sheet-id")
    monkeypatch.setattr(sheets, "GOOGLE_SHEETS_CREDENTIALS_PATH", "creds.json")
    monkeypatch.setattr(sheets.Credentials, "from_service_account_file", fake_credentials)
    monkeypatch.setattr(sheets.gspread, "authorize", lambda _creds: client)
    sheets.reset_client_cache()

    assert sheets.get_worksheet(apply_schema=False) is ws
    assert captured["scopes"] == [
        "https://www.googleapis.com/auth/spreadsheets.readonly"
    ]
    assert sheets._WORKSHEET is None


def test_get_worksheet_preserves_all_existing_column_formatting(monkeypatch):
    operator_position = 5
    headers = [
        *sheets.HEADERS[:operator_position],
        "Operator formula",
        *sheets.HEADERS[operator_position:],
    ]
    ws = _FakeWorksheet()
    ws.rows = [headers]
    ws.col_count = len(headers)
    spreadsheet = SimpleNamespace(worksheet=lambda _title: ws)
    client = SimpleNamespace(open_by_key=lambda _sheet_id: spreadsheet)

    monkeypatch.setattr(sheets, "GOOGLE_SHEETS_ID", "sheet-id")
    monkeypatch.setattr(sheets, "GOOGLE_SHEETS_CREDENTIALS_PATH", "creds.json")
    monkeypatch.setattr(
        sheets.Credentials,
        "from_service_account_file",
        lambda _path, *, scopes: object(),
    )
    monkeypatch.setattr(sheets.gspread, "authorize", lambda _creds: client)
    sheets.reset_client_cache()
    try:
        assert sheets.get_worksheet(apply_schema=True) is ws
    finally:
        sheets.reset_client_cache()

    assert ws.formats == []


def test_sheet_index_rejects_duplicate_headers_before_resolving_rows():
    duplicate_headers = [*sheets.HEADERS, sheets.COL_LEAD_ID]

    with pytest.raises(sheets.SheetsError, match="duplicate headers"):
        sheets.SheetIndex(
            _FakeWorksheet(),
            [duplicate_headers],
            actual_headers=duplicate_headers,
        )


def test_flush_dry_run_returns_exact_counts_without_sheet_writes():
    idx = sheets.SheetIndex(_FakeWorksheet(), [list(sheets.HEADERS)])
    payload = sheets.build_row_payload(
        lead=_make_lead(),
        title="CTO", emails=[], outreach_status=sheets.STATUS_CONNECTED,
        stage=sheets.STAGE_PROSPECTING, priority="", primary_location="",
        notes="", ai_notes="", last_synced="2026-08-26",
    )
    idx.upsert_row(payload)

    assert idx.flush(dry_run=True) == {"appended": 1, "updated": 0}
    assert idx.ws.appended_batches == []
    assert idx.ws.batch_updates == []


@pytest.mark.parametrize(
    "concurrent_value",
    [
        sheets.STATUS_DONT_SEND,
        '=IF(A2="Jane Doe","Don\'t send","")',
    ],
)
def test_flush_rejects_concurrent_dnc_or_formula_before_any_write(
    concurrent_value,
):
    idx, payload = _index_matching_people_payload()
    changed_payload = dict(payload)
    changed_payload[sheets.COL_OUTREACH_STATUS] = sheets.STATUS_REPLIED
    assert idx.upsert_row(changed_payload) == (
        False,
        [sheets.COL_OUTREACH_STATUS],
    )

    appended_payload = sheets.build_row_payload(
        lead=_make_lead(
            pk=456,
            first_name="New",
            last_name="Person",
            linkedin_url="https://www.linkedin.com/in/new-person/",
        ),
        title="",
        emails=[],
        outreach_status=sheets.STATUS_CONNECTED,
        stage=sheets.STAGE_PROSPECTING,
        priority="",
        primary_location="",
        notes="",
        ai_notes="",
        last_synced="2026-08-26",
    )
    assert idx.upsert_row(appended_payload)[0] is True

    status_column = idx.actual_index_0[sheets.COL_OUTREACH_STATUS]
    idx.ws.rows[1][status_column] = concurrent_value

    with pytest.raises(
        sheets.SheetsError,
        match="People row 2, column 'Outreach status' changed after planning",
    ):
        idx.flush()

    assert idx.ws.get_all_values_options[-1] == {
        "value_render_option": sheets.ValueRenderOption.formula,
    }
    assert idx.ws.batch_get_options == []
    assert idx.ws.appended_batches == []
    assert idx.ws.batch_updates == []


def test_flush_api_preflight_failure_prevents_appends_and_updates():
    idx, payload = _index_matching_people_payload()
    changed_payload = dict(payload)
    changed_payload[sheets.COL_OUTREACH_STATUS] = sheets.STATUS_REPLIED
    idx.upsert_row(changed_payload)
    idx.upsert_row(sheets.build_row_payload(
        lead=_make_lead(
            pk=456,
            linkedin_url="https://www.linkedin.com/in/new-person/",
        ),
        title="",
        emails=[],
        outreach_status=sheets.STATUS_CONNECTED,
        stage=sheets.STAGE_PROSPECTING,
        priority="",
        primary_location="",
        notes="",
        ai_notes="",
        last_synced="2026-08-26",
    ))
    response = SimpleNamespace(
        json=lambda: {
            "error": {
                "code": 503,
                "message": "service unavailable",
                "status": "UNAVAILABLE",
            },
        },
        text="service unavailable",
    )

    def fail_preflight(*_args, **_kwargs):
        raise sheets.APIError(response)

    idx.ws.get_all_values = fail_preflight

    with pytest.raises(sheets.SheetsError, match="failed optimistic preflight"):
        idx.flush()

    assert idx.ws.appended_batches == []
    assert idx.ws.batch_updates == []


def test_same_run_coalescing_keeps_original_live_preflight_expectation():
    idx, payload = _index_matching_people_payload()
    first = dict(payload)
    first[sheets.COL_OUTREACH_STATUS] = sheets.STATUS_REPLIED
    second = dict(payload)
    second[sheets.COL_OUTREACH_STATUS] = sheets.STATUS_WANTS_MEETING

    assert idx.upsert_row(first) == (False, [sheets.COL_OUTREACH_STATUS])
    assert idx.upsert_row(second) == (False, [sheets.COL_OUTREACH_STATUS])

    counts = idx.flush()

    assert counts == {"appended": 0, "updated": 1}
    assert idx.ws.get_all_values_options[-1] == {
        "value_render_option": sheets.ValueRenderOption.formula,
    }
    assert idx.ws.batch_get_options == []
    assert idx.ws.batch_updates == [[{
        "range": "H2:H2",
        "values": [[sheets.STATUS_WANTS_MEETING]],
    }]]


def test_people_optimistic_preflight_uses_one_snapshot_for_large_update_sets():
    fields_by_row = []
    for lead_id in range(1, 202):
        fields = {header: "" for header in sheets.HEADERS}
        fields.update({
            sheets.COL_NAME: f"Person {lead_id}",
            sheets.COL_LINKEDIN_URL: (
                f"https://www.linkedin.com/in/person-{lead_id}/"
            ),
            sheets.COL_OUTREACH_STATUS: sheets.STATUS_CONNECTED,
            sheets.COL_STAGE: sheets.STAGE_PROSPECTING,
            sheets.COL_LEAD_ID: str(lead_id),
        })
        fields_by_row.append(fields)

    rows = [
        list(sheets.HEADERS),
        *[
            [fields[header] for header in sheets.HEADERS]
            for fields in fields_by_row
        ],
    ]
    worksheet = _FakeWorksheet()
    worksheet.rows = [list(row) for row in rows]
    idx = sheets.SheetIndex(worksheet, rows)

    for fields in fields_by_row:
        changed = dict(fields)
        changed[sheets.COL_OUTREACH_STATUS] = sheets.STATUS_REPLIED
        assert idx.upsert_row(changed) == (
            False,
            [sheets.COL_OUTREACH_STATUS],
        )

    assert idx.flush() == {"appended": 0, "updated": 201}
    assert worksheet.get_all_values_options == [{
        "value_render_option": sheets.ValueRenderOption.formula,
    }]
    assert worksheet.batch_get_options == []
    assert len(worksheet.batch_updates) == 1
    assert len(worksheet.batch_updates[0]) == 201


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
    assert all(
        not update["range"].startswith("H2")
        for update in idx._pending_updates
    )


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


def test_upsert_row_preserves_human_owned_notes_field():
    """An existing operator note cannot be replaced by publisher input."""
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
    assert sheets.COL_NOTES not in changed
    notes_col = sheets._col_letter(sheets.HEADER_INDEX_0[sheets.COL_NOTES] + 1)
    assert all(
        update["range"] != f"{notes_col}2:{notes_col}2"
        for update in idx._pending_updates
    )


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
        sheets.COL_LEAD_ID: "123",
    })
    existing_row = []
    operator_formula = '=IF(A2="Jane Doe","jane@apollo.example","")'
    for h in actual_headers:
        existing_row.append(operator_formula if h == "Apollo Email" else fields[h])
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

    apollo_letter = sheets._col_letter(actual_headers.index("Apollo Email") + 1)
    assert all(
        update["range"] != f"{apollo_letter}2:{apollo_letter}2"
        for update in idx._pending_updates
    )
    assert idx.rows[1][actual_headers.index("Apollo Email")] == operator_formula


def test_upsert_row_accepts_id_only_contact_without_linkedin_url():
    idx = sheets.SheetIndex(_FakeWorksheet(), [list(sheets.HEADERS)])
    payload = sheets.build_row_payload(
        lead=_make_lead(linkedin_url=""),
        title="", emails=[], outreach_status="", stage="",
        priority="", primary_location="", notes="", ai_notes="",
        last_synced="",
    )
    was_new, _changed = idx.upsert_row(payload)

    assert was_new is True
    assert idx._pending_appends[0][idx.actual_index_0[sheets.COL_LEAD_ID]] == "123"
    assert idx._pending_appends[0][idx.actual_index_0[sheets.COL_LINKEDIN_URL]] == ""


def test_existing_formulas_are_never_overwritten_even_in_managed_columns():
    formula = '=IF(A2="Jane Doe","Human title","")'
    idx = _index_with_existing_row(**{sheets.COL_TITLE: formula})
    payload = sheets.build_row_payload(
        lead=_make_lead(),
        title="Publisher title", emails=[], outreach_status=sheets.STATUS_REPLIED,
        stage=sheets.STAGE_QUALIFICATION, priority="", primary_location="",
        notes="", ai_notes="", last_synced="2026-08-26",
    )

    idx.upsert_row(payload)

    title_letter = sheets._col_letter(idx.actual_index_0[sheets.COL_TITLE] + 1)
    assert idx.rows[1][idx.actual_index_0[sheets.COL_TITLE]] == formula
    assert all(
        update["range"] != f"{title_letter}2:{title_letter}2"
        for update in idx._pending_updates
    )


def test_people_preservation_snapshot_retries_quota_read(monkeypatch):
    calls = []
    sleeps = []
    response = SimpleNamespace(
        status_code=429,
        json=lambda: {
            "error": {
                "code": 429,
                "message": "private People detail",
                "status": "RESOURCE_EXHAUSTED",
            },
        },
        text="private People detail",
    )

    class Worksheet:
        def get_all_values(self, **_kwargs):
            calls.append(True)
            if len(calls) <= 2:
                raise sheets.APIError(response)
            return [list(sheets.HEADERS)]

    monkeypatch.setattr(crm_sheets.time, "sleep", sleeps.append)

    snapshot = sheets.capture_people_preservation_snapshot(Worksheet())

    assert snapshot.headers == tuple(sheets.HEADERS)
    assert sleeps == [5, 10]


def test_people_preservation_snapshot_verifies_order_human_operator_and_formula():
    headers = [*sheets.HEADERS, "Apollo Email", "Operator Formula"]
    first = {header: "" for header in headers}
    first.update({
        sheets.COL_LEAD_ID: "10",
        sheets.COL_LINKEDIN_URL: "https://linkedin.com/in/Jane-Doe?trk=old",
        sheets.COL_NOTES: "  exact human note\n",
        "Apollo Email": "jane@operator.example",
        "Operator Formula": '=IF(A2="Jane Doe","yes","")',
    })
    second = {header: "" for header in headers}
    second.update({
        sheets.COL_LEAD_ID: "11",
        sheets.COL_LINKEDIN_URL: "https://www.linkedin.com/in/john-doe/",
        sheets.COL_PRIORITY: "High",
        "Apollo Email": "john@operator.example",
    })
    before_values = [
        headers,
        [first[header] for header in headers],
        [second[header] for header in headers],
    ]
    after_values = [
        [*headers, "Lead Source"],
        [first[header] for header in headers] + [""],
        [second[header] for header in headers] + [""],
        ["New Person"] + [""] * len(headers),
    ]

    before = sheets.capture_people_preservation_snapshot(values=before_values)
    after = sheets.capture_people_preservation_snapshot(values=after_values)
    result = sheets.verify_people_preserved(before, after)

    assert before.row_order == ("lead:10", "lead:11")
    assert before.url_multiplicity == (
        ("https://www.linkedin.com/in/jane-doe/", 1),
        ("https://www.linkedin.com/in/john-doe/", 1),
    )
    assert result.rows_preserved == 2
    assert result.protected_cells_preserved == 14
    assert result.formulas_preserved == 1


@pytest.mark.parametrize(
    ("mutate", "error"),
    [
        (lambda rows: rows.__setitem__(1, rows[2]), "Lead ID changed"),
        (
            lambda rows: rows[1].__setitem__(
                sheets.HEADERS.index(sheets.COL_NOTES), "changed"
            ),
            "protected cell changed",
        ),
        (
            lambda rows: rows[1].__setitem__(
                sheets.HEADERS.index(sheets.COL_NAME), "=CHANGED()"
            ),
            "formula changed",
        ),
    ],
)
def test_people_preservation_snapshot_rejects_reorder_or_owned_cell_change(
    mutate,
    error,
):
    headers = [*sheets.HEADERS, "Operator Formula"]
    rows = [
        headers,
        [""] * len(headers),
        [""] * len(headers),
    ]
    rows[1][headers.index(sheets.COL_LEAD_ID)] = "1"
    rows[1][headers.index(sheets.COL_LINKEDIN_URL)] = (
        "https://www.linkedin.com/in/one/"
    )
    rows[1][headers.index(sheets.COL_NOTES)] = "human"
    rows[1][headers.index(sheets.COL_NAME)] = "=ORIGINAL_NAME()"
    rows[1][-1] = "=ORIGINAL()"
    rows[2][headers.index(sheets.COL_LEAD_ID)] = "2"
    rows[2][headers.index(sheets.COL_LINKEDIN_URL)] = (
        "https://www.linkedin.com/in/two/"
    )
    before = sheets.capture_people_preservation_snapshot(
        values=[list(row) for row in rows]
    )
    after_rows = [list(row) for row in rows]
    mutate(after_rows)

    with pytest.raises(sheets.SheetsError, match=error):
        sheets.verify_people_preserved(
            before,
            sheets.capture_people_preservation_snapshot(values=after_rows),
        )
