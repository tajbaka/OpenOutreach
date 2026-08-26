"""Network-free safety tests for the canonical CRM Sheets adapters."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from gspread.exceptions import APIError, WorksheetNotFound

from linkedin.exceptions import SheetsError
from linkedin.notifications import crm_sheets as crm


def _api_error(status: int, message: str = "provider detail") -> APIError:
    response = SimpleNamespace(
        status_code=status,
        json=lambda: {
            "error": {
                "code": status,
                "message": message,
                "status": "RESOURCE_EXHAUSTED",
            },
        },
        text=message,
    )
    return APIError(response)


def test_formula_values_retries_quota_with_bounded_backoff(monkeypatch):
    calls = []
    sleeps = []

    class QuotaWorksheet:
        title = "Opportunities"

        def get_all_values(self, **_kwargs):
            calls.append(True)
            if len(calls) <= 2:
                raise _api_error(429, "private provider identifier")
            return [["Opportunity ID"], ["safe-id"]]

    monkeypatch.setattr(crm.time, "sleep", sleeps.append)

    assert crm._formula_values(QuotaWorksheet()) == [
        ["Opportunity ID"],
        ["safe-id"],
    ]
    assert sleeps == [5, 10]


def test_formula_values_nonretryable_error_is_sanitized(monkeypatch):
    class BadWorksheet:
        title = "Opportunities"

        def get_all_values(self, **_kwargs):
            raise _api_error(400, "private provider identifier")

    monkeypatch.setattr(crm.time, "sleep", lambda _delay: None)

    with pytest.raises(SheetsError) as captured:
        crm._formula_values(BadWorksheet())

    assert "private provider identifier" not in str(captured.value)


def test_inventory_retries_metadata_and_worksheet_listing(monkeypatch):
    worksheet = FakeWorksheet("People", [["Lead ID"], ["1"]])

    class RetryingSpreadsheet(FakeSpreadsheet):
        def __init__(self):
            super().__init__([worksheet])
            self.metadata_calls = 0
            self.worksheet_calls = 0

        def fetch_sheet_metadata(self, params=None):
            self.metadata_calls += 1
            if self.metadata_calls == 1:
                raise _api_error(429, "private metadata detail")
            return {"sheets": []}

        def worksheets(self):
            self.worksheet_calls += 1
            if self.worksheet_calls == 1:
                raise _api_error(503, "private worksheet-list detail")
            return [worksheet]

    spreadsheet = RetryingSpreadsheet()
    sleeps = []
    monkeypatch.setattr(crm.time, "sleep", sleeps.append)

    inventory = crm.inventory_spreadsheet(spreadsheet)

    assert inventory["tab_count"] == 1
    assert spreadsheet.metadata_calls == 2
    assert spreadsheet.worksheet_calls == 2
    assert sleeps == [5, 5]


def _column_number(letters: str) -> int:
    value = 0
    for character in letters:
        value = value * 26 + ord(character) - 64
    return value


def _split_cell(reference: str) -> tuple[int, int]:
    letters = "".join(character for character in reference if character.isalpha())
    digits = "".join(character for character in reference if character.isdigit())
    return int(digits), _column_number(letters)


class FakeWorksheet:
    def __init__(
        self,
        title: str,
        rows: list[list[str]] | None = None,
        *,
        displayed_rows: list[list[str]] | None = None,
        sheet_id: int = 1,
    ):
        self.title = title
        self.id = sheet_id
        self.rows = [list(row) for row in (rows or [])]
        self.displayed_rows = (
            [list(row) for row in displayed_rows]
            if displayed_rows is not None
            else None
        )
        self.row_count = max(1000, len(self.rows))
        self.col_count = max(1, max((len(row) for row in self.rows), default=1))
        self.added_cols: list[int] = []
        self.updates: list[dict] = []
        self.batch_updates: list[dict] = []
        self.appended: list[list[str]] = []
        self._properties = {
            "hidden": False,
            "gridProperties": {"frozenRowCount": 1},
        }

    def get_all_values(self, value_render_option=None):
        if value_render_option is None and self.displayed_rows is not None:
            return [list(row) for row in self.displayed_rows]
        return [list(row) for row in self.rows]

    def add_cols(self, count):
        self.added_cols.append(count)
        self.col_count += count

    def update(self, *, values, range_name, value_input_option=None):
        self.updates.append({"range": range_name, "values": values})
        start = range_name.split(":", 1)[0]
        row_number, column_number = _split_cell(start)
        for row_offset, values_row in enumerate(values):
            target_row = row_number + row_offset
            while len(self.rows) < target_row:
                self.rows.append([])
            row = self.rows[target_row - 1]
            while len(row) < column_number - 1:
                row.append("")
            for column_offset, value in enumerate(values_row):
                index = column_number - 1 + column_offset
                while len(row) <= index:
                    row.append("")
                row[index] = value
        start, _ = range_name.split(":") if ":" in range_name else (range_name, range_name)
        row_number, column_number = _split_cell(start)
        while len(self.rows) < row_number:
            self.rows.append([])
        row = self.rows[row_number - 1]
        while len(row) < column_number + len(values[0]) - 1:
            row.append("")
        for offset, value in enumerate(values[0]):
            row[column_number - 1 + offset] = value

    def batch_update(self, updates, value_input_option=None):
        self.batch_updates.extend(updates)
        for update in updates:
            start = update["range"].split(":", 1)[0]
            row_number, column_number = _split_cell(start)
            while len(self.rows) < row_number:
                self.rows.append([])
            row = self.rows[row_number - 1]
            while len(row) < column_number:
                row.append("")
            row[column_number - 1] = update["values"][0][0]

    def append_rows(self, rows, value_input_option=None, table_range=None):
        self.appended.extend([list(row) for row in rows])
        self.rows.extend([list(row) for row in rows])


class FakeSpreadsheet:
    def __init__(self, worksheets: list[FakeWorksheet] | None = None):
        self.title = "CRM Test"
        self.id = "sheet-id"
        self.tabs = {worksheet.title: worksheet for worksheet in (worksheets or [])}
        self.created: list[str] = []

    def worksheets(self):
        return list(self.tabs.values())

    def worksheet(self, title):
        try:
            return self.tabs[title]
        except KeyError as exc:
            raise WorksheetNotFound(title) from exc

    def add_worksheet(self, *, title, rows, cols):
        self.created.append(title)
        worksheet = FakeWorksheet(title)
        worksheet.row_count = rows
        worksheet.col_count = cols
        self.tabs[title] = worksheet
        return worksheet

    def fetch_sheet_metadata(self, params=None):
        return {
            "sheets": [
                {
                    "properties": {
                        "sheetId": worksheet.id,
                        "title": worksheet.title,
                        "hidden": worksheet.title == "Hidden",
                        "gridProperties": {
                            "frozenRowCount": 1,
                            "frozenColumnCount": 2,
                        },
                    },
                    "protectedRanges": [{"range": {}}],
                    "merges": [{"startRowIndex": 0}],
                }
                for worksheet in self.worksheets()
            ]
        }


def _row(headers, **values):
    return [str(values.get(header, "")) for header in headers]


def _baseline(**values):
    return json.dumps(values, sort_keys=True, separators=(",", ":"))


def test_additive_headers_append_after_operator_columns_and_dry_run_is_read_only():
    worksheet = FakeWorksheet(
        crm.OPPORTUNITIES_TAB,
        [[crm.COL_OPPORTUNITY_ID, "Operator formula"]],
    )

    missing = crm.ensure_additive_headers(
        worksheet,
        [crm.COL_OPPORTUNITY_ID, crm.COL_ACCOUNT],
        dry_run=True,
    )

    assert missing == (crm.COL_ACCOUNT,)
    assert worksheet.updates == []

    crm.ensure_additive_headers(
        worksheet,
        [crm.COL_OPPORTUNITY_ID, crm.COL_ACCOUNT],
    )

    assert worksheet.rows[0] == [
        crm.COL_OPPORTUNITY_ID,
        "Operator formula",
        crm.COL_ACCOUNT,
    ]
    assert worksheet.updates == [{"range": "C1:C1", "values": [[crm.COL_ACCOUNT]]}]


def test_missing_managed_tab_dry_run_reports_creation_without_writing():
    spreadsheet = FakeSpreadsheet()

    worksheet, result = crm.ensure_managed_tab(
        spreadsheet,
        title=crm.PIPELINE_TAB,
        required_headers=crm.PIPELINE_HEADERS,
        dry_run=True,
    )

    assert worksheet is None
    assert result.would_create is True
    assert result.header_additions == crm.PIPELINE_HEADERS
    assert spreadsheet.created == []


def test_missing_managed_tab_apply_creates_additively():
    spreadsheet = FakeSpreadsheet()

    worksheet, result = crm.ensure_managed_tab(
        spreadsheet,
        title=crm.PIPELINE_TAB,
        required_headers=crm.PIPELINE_HEADERS,
    )

    assert result.would_create is True
    assert spreadsheet.created == [crm.PIPELINE_TAB]
    assert worksheet.rows[0] == list(crm.PIPELINE_HEADERS)


def test_pipeline_schema_is_stable_id_plus_canonical_stage_columns():
    assert crm.PIPELINE_HEADERS == (
        crm.COL_OPPORTUNITY_ID,
        "Prospecting",
        "Discovery",
        "Demo Planning",
        "Evaluation",
        "Sandbox/Pilot",
        "Commercial",
        "Procurement/Legal",
        "Closed Won",
        "Expansion",
        "Closed Lost",
    )


def test_inactivity_age_is_managed_on_opportunities_and_recovery():
    assert crm.COL_INACTIVITY_AGE in crm.OPPORTUNITY_HEADERS
    assert crm.COL_INACTIVITY_AGE in crm.RECOVERY_HEADERS

    row = crm.opportunity_to_sheet_row(
        SimpleNamespace(
            id="opp-1",
            account=SimpleNamespace(name="Acme"),
            owner_id=None,
            owner=None,
            stage="discovery",
            sales_motion_step=2,
            contacts=SimpleNamespace(all=lambda: []),
            motion_key="primary",
            name="Acme opportunity",
            manual_pin=False,
            value=None,
            currency="USD",
            probability=None,
            closed_lost_reason="",
            last_meaningful_activity_at=None,
        ),
        derived={crm.COL_INACTIVITY_AGE: 30},
    )

    assert row[crm.COL_INACTIVITY_AGE] == "30"


def test_pipeline_stage_row_rejects_unknown_stage_instead_of_inferring_it():
    with pytest.raises(SheetsError, match="unknown canonical stage"):
        crm.pipeline_stage_row(
            opportunity_id="opp-1",
            stage="maybe later",
            card_summary="Acme",
        )


def test_pipeline_adapter_builds_a_readable_card_from_canonical_fields():
    worksheet = FakeWorksheet(crm.PIPELINE_TAB, [list(crm.PIPELINE_HEADERS)])

    plan = crm.pipeline_adapter(worksheet).plan([
        {
            crm.COL_OPPORTUNITY_ID: "opp-1",
            crm.COL_STAGE: "discovery",
            crm.COL_ACCOUNT: "Acme",
            crm.COL_OWNER: "Arian",
            crm.COL_NEXT_ACTION: "Confirm stakeholders",
        }
    ])

    assert plan.appends[0][crm.PIPELINE_STAGE_COLUMNS["discovery"]] == (
        "Acme\nOwner: Arian\nNext: Confirm stakeholders"
    )


def test_pipeline_stage_row_round_trips_through_pipeline_adapter_without_inference():
    worksheet = FakeWorksheet(crm.PIPELINE_TAB, [list(crm.PIPELINE_HEADERS)])
    source = crm.pipeline_stage_row(
        opportunity_id="opp-1",
        stage="sandbox_pilot",
        card_summary="Acme sandbox",
    )

    plan = crm.pipeline_adapter(worksheet).plan([source])

    assert source[crm.COL_STAGE] == "sandbox_pilot"
    assert plan.appends[0][crm.PIPELINE_STAGE_COLUMNS["sandbox_pilot"]] == (
        "Acme sandbox"
    )


def test_three_way_merge_routes_sheet_only_database_only_and_conflicts():
    sheet_only = crm.merge_human_fields(
        sheet_values={crm.COL_OWNER: "Human"},
        database_values={crm.COL_OWNER: "Old"},
        baseline_values={crm.COL_OWNER: "Old"},
        human_fields=[crm.COL_OWNER],
    )
    assert sheet_only.imports == ((crm.COL_OWNER, "Human"),)
    assert sheet_only.sheet_updates == {}

    database_only = crm.merge_human_fields(
        sheet_values={crm.COL_OWNER: "Old"},
        database_values={crm.COL_OWNER: "Database"},
        baseline_values={crm.COL_OWNER: "Old"},
        human_fields=[crm.COL_OWNER],
    )
    assert database_only.sheet_updates == {crm.COL_OWNER: "Database"}
    assert database_only.imports == ()

    conflict = crm.merge_human_fields(
        sheet_values={crm.COL_OWNER: "Human"},
        database_values={crm.COL_OWNER: "Database"},
        baseline_values={crm.COL_OWNER: "Old"},
        human_fields=[crm.COL_OWNER],
    )
    assert conflict.conflicts == (
        (crm.COL_OWNER, "Old", "Human", "Database"),
    )
    assert conflict.sheet_updates == {}


def test_three_way_merge_never_blanks_a_nonblank_sheet_value_from_empty_database():
    result = crm.merge_human_fields(
        sheet_values={crm.COL_NEXT_ACTION: "Call tomorrow"},
        database_values={crm.COL_NEXT_ACTION: ""},
        baseline_values={crm.COL_NEXT_ACTION: "Call tomorrow"},
        human_fields=[crm.COL_NEXT_ACTION],
    )

    assert result.merged_values[crm.COL_NEXT_ACTION] == "Call tomorrow"
    assert result.imports == ((crm.COL_NEXT_ACTION, "Call tomorrow"),)
    assert result.sheet_updates == {}


@pytest.mark.parametrize(
    ("field", "before", "sheet_value", "database_value"),
    [
        (crm.COL_STAGE, "prospecting", "Discovery", "discovery"),
        (crm.COL_MANUAL_PIN, "FALSE", "yes", "TRUE"),
        (crm.COL_CURRENCY, "CAD", "usd", "USD"),
        (crm.COL_VALUE, "", "100", "100.00"),
        (crm.COL_DISPOSITION, "", "Polite decline", "polite_decline"),
        (crm.COL_WAITING_UNTIL, "", "2026-08-27", "2026-08-27"),
    ],
)
def test_three_way_merge_accepts_equivalent_human_aliases_after_import(
    field,
    before,
    sheet_value,
    database_value,
):
    result = crm.merge_human_fields(
        sheet_values={field: sheet_value},
        database_values={field: database_value},
        baseline_values={field: before},
        human_fields=[field],
    )

    assert result.conflicts == ()
    assert result.imports == ()
    if sheet_value != database_value:
        assert result.sheet_updates == {field: database_value}


def test_opportunities_are_incremental_cell_owned_and_do_not_prune_omitted_ids():
    headers = [*crm.OPPORTUNITY_HEADERS, "Operator formula"]
    baseline = _baseline(**{crm.COL_OWNER: "Arian"})
    worksheet = FakeWorksheet(
        crm.OPPORTUNITIES_TAB,
        [
            headers,
            _row(
                headers,
                **{
                    crm.COL_OPPORTUNITY_ID: "opp-1",
                    crm.COL_ACCOUNT: "Old account",
                    crm.COL_OWNER: "Arian",
                    crm.COL_HUMAN_BASELINE: baseline,
                    "Operator formula": "=ROW()",
                },
            ),
            _row(
                headers,
                **{
                    crm.COL_OPPORTUNITY_ID: "opp-2",
                    crm.COL_ACCOUNT: "Retain me",
                    "Operator formula": "=ROW()",
                },
            ),
        ],
    )
    adapter = crm.OpportunitySheetAdapter(worksheet)

    plan = adapter.plan([
        {
            crm.COL_OPPORTUNITY_ID: "opp-1",
            crm.COL_ACCOUNT: "New account",
            crm.COL_OWNER: "Arian",
        }
    ])

    assert plan.retained_missing_keys == ("opp-2",)
    assert not any(change.row == 3 for change in plan.changes)
    assert any(change.column == crm.COL_ACCOUNT for change in plan.changes)
    adapter.apply(plan)

    formula_index = headers.index("Operator formula")
    assert worksheet.rows[1][formula_index] == "=ROW()"
    assert worksheet.rows[2][formula_index] == "=ROW()"
    assert worksheet.rows[2][headers.index(crm.COL_ACCOUNT)] == "Retain me"
    written_ranges = {item["range"] for item in worksheet.batch_updates}
    formula_letter = crm._column_letter(formula_index + 1)
    assert all(not cell_range.startswith(formula_letter) for cell_range in written_ranges)


def test_opportunity_last_synced_timestamp_does_not_dirty_identical_row():
    headers = list(crm.OPPORTUNITY_HEADERS)
    human_baseline = {field: "" for field in crm.OPPORTUNITY_HUMAN_FIELDS}
    worksheet = FakeWorksheet(
        crm.OPPORTUNITIES_TAB,
        [
            headers,
            _row(
                headers,
                **{
                    crm.COL_OPPORTUNITY_ID: "opp-1",
                    crm.COL_LAST_SYNCED: "2026-08-25T10:00:00+00:00",
                    crm.COL_HUMAN_BASELINE: _baseline(**human_baseline),
                },
            ),
        ],
    )

    plan = crm.OpportunitySheetAdapter(worksheet).plan(
        [
            {
                crm.COL_OPPORTUNITY_ID: "opp-1",
                crm.COL_LAST_SYNCED: "2026-08-26T10:00:00+00:00",
            }
        ],
        baseline_by_id={"opp-1": human_baseline},
    )

    assert plan.changes == []


def test_opportunity_sheet_import_requires_database_ack_and_uses_db_baseline():
    headers = list(crm.OPPORTUNITY_HEADERS)
    worksheet = FakeWorksheet(
        crm.OPPORTUNITIES_TAB,
        [
            headers,
            _row(
                headers,
                **{
                    crm.COL_OPPORTUNITY_ID: "opp-1",
                    crm.COL_OWNER: "Human owner",
                    crm.COL_HUMAN_BASELINE: "malformed fallback is ignored",
                },
            ),
        ],
    )
    adapter = crm.OpportunitySheetAdapter(worksheet)

    plan = adapter.plan(
        [{crm.COL_OPPORTUNITY_ID: "opp-1", crm.COL_OWNER: "Old owner"}],
        baseline_by_id={"opp-1": {crm.COL_OWNER: "Old owner"}},
    )

    assert plan.imports == [
        crm.HumanFieldImport("opp-1", crm.COL_OWNER, "Human owner")
    ]
    with pytest.raises(SheetsError, match="rebuild and re-plan"):
        adapter.apply(plan)
    assert worksheet.batch_updates == []

    refreshed_plan = adapter.plan(
        [{crm.COL_OPPORTUNITY_ID: "opp-1", crm.COL_OWNER: "Human owner"}],
        baseline_by_id={"opp-1": {crm.COL_OWNER: "Old owner"}},
    )
    assert refreshed_plan.imports == []
    adapter.apply(refreshed_plan)
    assert worksheet.rows[1][headers.index(crm.COL_OWNER)] == "Human owner"
    assert refreshed_plan.baseline_updates[0].values[crm.COL_OWNER] == "Human owner"


def test_opportunity_conflict_blocks_all_writes():
    headers = list(crm.OPPORTUNITY_HEADERS)
    worksheet = FakeWorksheet(
        crm.OPPORTUNITIES_TAB,
        [
            headers,
            _row(
                headers,
                **{
                    crm.COL_OPPORTUNITY_ID: "opp-1",
                    crm.COL_OWNER: "Sheet edit",
                    crm.COL_HUMAN_BASELINE: _baseline(**{crm.COL_OWNER: "Old"}),
                },
            ),
        ],
    )
    adapter = crm.OpportunitySheetAdapter(worksheet)
    plan = adapter.plan([
        {crm.COL_OPPORTUNITY_ID: "opp-1", crm.COL_OWNER: "Database edit"}
    ])

    assert len(plan.conflicts) == 1
    with pytest.raises(SheetsError, match="conflict"):
        adapter.apply(plan)
    assert worksheet.batch_updates == []
    assert worksheet.appended == []


@pytest.mark.parametrize("replacement_key", ["", "opp-mutated"])
def test_opportunity_apply_rejects_retained_stable_key_deletion_or_mutation(
    replacement_key,
):
    headers = list(crm.OPPORTUNITY_HEADERS)
    worksheet = FakeWorksheet(
        crm.OPPORTUNITIES_TAB,
        [
            headers,
            _row(headers, **{crm.COL_OPPORTUNITY_ID: "opp-retained"}),
        ],
    )
    adapter = crm.OpportunitySheetAdapter(worksheet)
    plan = adapter.plan([])
    assert plan.changes == []
    assert plan.appends == []
    assert plan.retained_missing_keys == ("opp-retained",)

    worksheet.rows[1][headers.index(crm.COL_OPPORTUNITY_ID)] = replacement_key

    with pytest.raises(SheetsError, match="durable stable key set changed"):
        adapter.apply(plan)

    assert worksheet.updates == []
    assert worksheet.batch_updates == []
    assert worksheet.appended == []


def test_opportunity_apply_rejects_unknown_key_that_appeared_after_planning():
    headers = list(crm.OPPORTUNITY_HEADERS)
    worksheet = FakeWorksheet(
        crm.OPPORTUNITIES_TAB,
        [
            headers,
            _row(headers, **{crm.COL_OPPORTUNITY_ID: "opp-known"}),
        ],
    )
    adapter = crm.OpportunitySheetAdapter(worksheet)
    plan = adapter.plan([])
    worksheet.rows.append(
        _row(headers, **{crm.COL_OPPORTUNITY_ID: "opp-concurrent"})
    )

    with pytest.raises(SheetsError, match=r"0 missing, 1 unknown"):
        adapter.apply(plan)

    assert worksheet.updates == []
    assert worksheet.batch_updates == []
    assert worksheet.appended == []


def test_duplicate_opportunity_id_is_reported_and_rejected():
    headers = list(crm.OPPORTUNITY_HEADERS)
    duplicate = _row(headers, **{crm.COL_OPPORTUNITY_ID: "opp-1"})
    worksheet = FakeWorksheet(
        crm.OPPORTUNITIES_TAB,
        [headers, duplicate, duplicate],
    )

    with pytest.raises(SheetsError, match="duplicate Opportunity ID"):
        crm.OpportunitySheetAdapter(worksheet).plan([])


def test_pipeline_stage_movement_clears_old_column_and_preserves_identity_and_formula():
    headers = [*crm.PIPELINE_HEADERS, "Operator formula"]
    worksheet = FakeWorksheet(
        crm.PIPELINE_TAB,
        [
            headers,
            _row(
                headers,
                **{
                    crm.COL_OPPORTUNITY_ID: "opp-1",
                    crm.PIPELINE_STAGE_COLUMNS["discovery"]: "Acme card",
                    "Operator formula": "=ROW()",
                },
            ),
            _row(
                headers,
                **{
                    crm.COL_OPPORTUNITY_ID: "opp-stale",
                    crm.PIPELINE_STAGE_COLUMNS["prospecting"]: "Stale card",
                    "Operator formula": "=ROW()",
                },
            ),
        ],
    )
    adapter = crm.pipeline_adapter(worksheet)
    plan = adapter.plan([
        {
            crm.COL_OPPORTUNITY_ID: "opp-1",
            crm.COL_STAGE: "evaluation",
            crm.COL_PIPELINE_CARD: "Acme card",
        }
    ])

    assert not any(
        change.column == crm.COL_OPPORTUNITY_ID and change.kind == "clear"
        for change in plan.changes
    )
    adapter.apply(plan)

    key_index = headers.index(crm.COL_OPPORTUNITY_ID)
    discovery_index = headers.index(crm.PIPELINE_STAGE_COLUMNS["discovery"])
    evaluation_index = headers.index(crm.PIPELINE_STAGE_COLUMNS["evaluation"])
    prospecting_index = headers.index(crm.PIPELINE_STAGE_COLUMNS["prospecting"])
    formula_index = headers.index("Operator formula")
    assert worksheet.rows[1][key_index] == "opp-1"
    assert worksheet.rows[1][discovery_index] == ""
    assert worksheet.rows[1][evaluation_index] == "Acme card"
    assert worksheet.rows[2][key_index] == "opp-stale"
    assert worksheet.rows[2][prospecting_index] == ""
    assert worksheet.rows[2][formula_index] == "=ROW()"


def test_followup_human_edits_are_read_and_imported_before_regeneration():
    headers = list(crm.FOLLOWUP_HEADERS)
    baseline = _baseline(**{
        column: "Old draft" if column == crm.COL_DRAFT else ""
        for column in crm.FOLLOWUP_HUMAN_FIELDS
    })
    worksheet = FakeWorksheet(
        "Arian - Followups",
        [
            headers,
            _row(
                headers,
                **{
                    crm.COL_ACTION_ID: "action-1",
                    crm.COL_DRAFT: "Human-edited draft",
                    crm.COL_HUMAN_BASELINE: baseline,
                },
            ),
        ],
    )
    adapter = crm.followups_adapter(worksheet)

    human_rows = adapter.read_human_fields()
    plan = adapter.plan([
        {
            crm.COL_ACTION_ID: "action-1",
            crm.COL_DRAFT: "Old draft",
        }
    ])

    assert human_rows[0][crm.COL_ACTION_ID] == "action-1"
    assert human_rows[0][crm.COL_DRAFT] == "Human-edited draft"
    assert plan.imports == [
        crm.HumanFieldImport(
            "action-1",
            crm.COL_DRAFT,
            "Human-edited draft",
        )
    ]
    with pytest.raises(SheetsError, match="rebuild and re-plan"):
        adapter.apply(plan)
    refreshed_plan = adapter.plan(
        [
            {
                crm.COL_ACTION_ID: "action-1",
                crm.COL_DRAFT: "Human-edited draft",
            }
        ],
        baseline_by_id={
            "action-1": {crm.COL_DRAFT: "Old draft"},
        },
    )
    assert refreshed_plan.imports == []
    adapter.apply(refreshed_plan)
    assert worksheet.rows[1][headers.index(crm.COL_DRAFT)] == "Human-edited draft"


def test_missing_followups_preserve_human_cells_and_handled_history():
    headers = list(crm.FOLLOWUP_HEADERS)
    worksheet = FakeWorksheet(
        "Arian - Followups",
        [
            headers,
            _row(
                headers,
                **{
                    crm.COL_ACTION_ID: "action-open",
                    crm.COL_ACCOUNT: "Clear system context",
                    crm.COL_DRAFT: "Keep unsent human draft",
                },
            ),
            _row(
                headers,
                **{
                    crm.COL_ACTION_ID: "action-handled",
                    crm.COL_ACCOUNT: "Keep sent history context",
                    crm.COL_DRAFT: "Sent draft",
                    crm.COL_HANDLED: "TRUE",
                },
            ),
        ],
    )
    adapter = crm.followups_adapter(worksheet)

    plan = adapter.plan([])
    adapter.apply(plan)

    account_index = headers.index(crm.COL_ACCOUNT)
    draft_index = headers.index(crm.COL_DRAFT)
    handled_index = headers.index(crm.COL_HANDLED)
    assert worksheet.rows[1][account_index] == ""
    assert worksheet.rows[1][draft_index] == "Keep unsent human draft"
    assert worksheet.rows[2][account_index] == "Keep sent history context"
    assert worksheet.rows[2][draft_index] == "Sent draft"
    assert worksheet.rows[2][handled_index] == "TRUE"
    assert plan.retained_missing_keys == ("action-handled",)


@pytest.mark.parametrize("replacement_key", ["", "action-mutated"])
def test_followup_apply_rejects_baseline_key_deletion_or_mutation(
    replacement_key,
):
    headers = list(crm.FOLLOWUP_HEADERS)
    human_baseline = {
        field: "" for field in crm.FOLLOWUP_HUMAN_FIELDS
    }
    worksheet = FakeWorksheet(
        "Arian - Followups",
        [
            headers,
            _row(
                headers,
                **{
                    crm.COL_ACTION_ID: "action-1",
                    crm.COL_HUMAN_BASELINE: _baseline(**human_baseline),
                },
            ),
        ],
    )
    adapter = crm.followups_adapter(worksheet)
    plan = adapter.plan(
        [{crm.COL_ACTION_ID: "action-1"}],
        baseline_by_id={"action-1": human_baseline},
    )
    assert plan.changes == []
    assert len(plan.baseline_updates) == 1

    worksheet.rows[1][headers.index(crm.COL_ACTION_ID)] = replacement_key

    with pytest.raises(SheetsError, match="baseline stable row"):
        adapter.apply(plan)

    assert worksheet.updates == []
    assert worksheet.batch_updates == []
    assert worksheet.appended == []


def test_apply_rejects_a_plan_when_an_owned_cell_changed_after_planning():
    headers = list(crm.PIPELINE_HEADERS)
    worksheet = FakeWorksheet(
        crm.PIPELINE_TAB,
        [
            headers,
            _row(
                headers,
                **{
                    crm.COL_OPPORTUNITY_ID: "opp-1",
                    crm.PIPELINE_STAGE_COLUMNS["discovery"]: "Old",
                },
            ),
        ],
    )
    adapter = crm.pipeline_adapter(worksheet)
    plan = adapter.plan([
        {
            crm.COL_OPPORTUNITY_ID: "opp-1",
            crm.COL_STAGE: "discovery",
            crm.COL_PIPELINE_CARD: "Planned",
        }
    ])
    worksheet.rows[1][
        headers.index(crm.PIPELINE_STAGE_COLUMNS["discovery"])
    ] = "Operator changed it"

    with pytest.raises(SheetsError, match="changed after planning"):
        adapter.apply(plan)
    assert worksheet.batch_updates == []


def test_dry_run_returns_exact_plan_without_any_writes():
    worksheet = FakeWorksheet(crm.PIPELINE_TAB, [list(crm.PIPELINE_HEADERS)])
    adapter = crm.pipeline_adapter(worksheet)
    plan = adapter.plan([
        {
            crm.COL_OPPORTUNITY_ID: "opp-1",
            crm.COL_STAGE: "prospecting",
            crm.COL_PIPELINE_CARD: "Acme",
        }
    ])

    summary = adapter.apply(plan, dry_run=True)

    assert summary["appended"] == 1
    assert summary["updated_cells"] == 0
    assert worksheet.appended == []
    assert worksheet.batch_updates == []
    assert worksheet.updates == []


def test_inventory_is_structural_only_and_reports_formulas_protection_and_hidden():
    worksheet = FakeWorksheet(
        "Hidden",
        [
            [crm.COL_OPPORTUNITY_ID, crm.COL_ACCOUNT, "Calc"],
            ["opp-1", "Secret Person", "=ROW()"],
            ["opp-1", "Another Person", ""],
        ],
    )
    spreadsheet = FakeSpreadsheet([worksheet])

    inventory = crm.inventory_spreadsheet(
        spreadsheet,
        stable_keys={"Hidden": crm.COL_OPPORTUNITY_ID},
    )

    assert "Secret Person" not in repr(inventory)
    tab = inventory["tabs"][0]
    assert tab["formula_count"] == 1
    assert tab["duplicate_key_groups"] == 1
    assert tab["duplicate_key_extra_rows"] == 1
    assert tab["hidden"] is True
    assert tab["protected_range_count"] == 1
    assert tab["merged_range_count"] == 1


def test_backup_captures_formula_and_displayed_values(tmp_path):
    worksheet = FakeWorksheet(
        crm.PIPELINE_TAB,
        [["Calc"], ["=1+1"]],
        displayed_rows=[["Calc"], ["2"]],
    )
    spreadsheet = FakeSpreadsheet([worksheet])

    path = crm.backup_spreadsheet(spreadsheet, tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["tabs"][0]["formula_values"][1][0] == "=1+1"
    assert payload["tabs"][0]["displayed_values"][1][0] == "2"


def test_backup_retries_display_read_and_reuses_formula_capture(
    tmp_path,
    monkeypatch,
):
    class CountingWorksheet(FakeWorksheet):
        def __init__(self):
            super().__init__(
                crm.PIPELINE_TAB,
                [["Calc"], ["=1+1"]],
                displayed_rows=[["Calc"], ["2"]],
            )
            self.formula_reads = 0
            self.display_reads = 0

        def get_all_values(self, value_render_option=None):
            if value_render_option is not None:
                self.formula_reads += 1
            else:
                self.display_reads += 1
                if self.display_reads == 1:
                    raise _api_error(429, "private displayed-value detail")
            return super().get_all_values(
                value_render_option=value_render_option,
            )

    worksheet = CountingWorksheet()
    spreadsheet = FakeSpreadsheet([worksheet])
    sleeps = []
    monkeypatch.setattr(crm.time, "sleep", sleeps.append)

    path = crm.backup_spreadsheet(spreadsheet, tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert worksheet.formula_reads == 1
    assert worksheet.display_reads == 2
    assert sleeps == [5]
    assert payload["inventory"]["tabs"][0]["formula_count"] == 1


def test_backup_rejects_a_prefix_that_can_escape_the_destination(tmp_path):
    with pytest.raises(ValueError, match="filename component"):
        crm.backup_spreadsheet(
            FakeSpreadsheet(),
            tmp_path,
            prefix="../outside",
        )


def test_model_shape_serializer_uses_stable_contact_ids_and_owner_handle():
    opportunity = SimpleNamespace(
        id="opp-1",
        account=SimpleNamespace(id="account-1", name="Acme", domain="acme.test"),
        owner=SimpleNamespace(handle="arian", display_name="Arian"),
        contacts=[
            SimpleNamespace(lead_id=11, role="champion"),
            SimpleNamespace(lead_id=12, role="decision_maker"),
        ],
        motion_key="acme-main",
        name="Acme opportunity",
        stage="discovery",
    )

    row = crm.opportunity_to_sheet_row(opportunity)

    assert row[crm.COL_OPPORTUNITY_ID] == "opp-1"
    assert row[crm.COL_OWNER] == "arian"
    assert row[crm.COL_CONTACT_LEAD_IDS] == "11, 12"
    assert row[crm.COL_CHAMPION] == "11"
    assert row[crm.COL_DECISION_MAKER] == "12"
