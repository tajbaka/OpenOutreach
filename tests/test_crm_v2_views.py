"""Focused, network-free tests for the concise CRM v2 Sheet projections."""
from __future__ import annotations

import json
from datetime import date, datetime, UTC

import pytest

from linkedin.crm_v2_publish import (
    ActionRecord,
    ActiveAccountRecord,
    action_row,
    build_crm_v2_view_rows,
)
from linkedin.exceptions import SheetsError
from linkedin.notifications import crm_sheets
from linkedin.notifications import crm_v2_sheets as v2


def _column_number(letters: str) -> int:
    value = 0
    for character in letters:
        value = value * 26 + ord(character) - 64
    return value


def _split_cell(reference: str) -> tuple[int, int]:
    letters = "".join(character for character in reference if character.isalpha())
    digits = "".join(character for character in reference if character.isdigit())
    return int(digits), _column_number(letters)


def _row(headers, **values):
    return [str(values.get(header, "")) for header in headers]


def _baseline(fields, **values):
    return json.dumps(
        {field: str(values.get(field, "")) for field in fields},
        sort_keys=True,
        separators=(",", ":"),
    )


class FakeWorksheet:
    def __init__(self, title: str, rows: list[list[str]]):
        self.title = title
        self.rows = [list(row) for row in rows]
        self.row_count = max(1000, len(rows))
        self.col_count = max(len(row) for row in rows)
        self.updates = []
        self.batch_updates = []
        self.appended = []

    def get_all_values(self, value_render_option=None):
        return [list(row) for row in self.rows]

    def add_cols(self, count):
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

    def batch_update(self, updates, value_input_option=None):
        self.batch_updates.extend(updates)
        for update in updates:
            row_number, column_number = _split_cell(update["range"].split(":", 1)[0])
            while len(self.rows) < row_number:
                self.rows.append([])
            row = self.rows[row_number - 1]
            while len(row) < column_number:
                row.append("")
            row[column_number - 1] = update["values"][0][0]

    def append_rows(self, rows, value_input_option=None, table_range=None):
        self.appended.extend([list(row) for row in rows])
        self.rows.extend([list(row) for row in rows])


def _active(**overrides) -> ActiveAccountRecord:
    values = {
        "opportunity_id": "opp-ramp",
        "account_id": "acct-ramp",
        "account": "Ramp",
        "owner": "Arian",
        "stage": "evaluation",
        "attention": "Now",
        "why_active": "Meeting on 2026-08-25",
        "evidence_tier": "T1 Meeting",
        "last_meaningful_touch": datetime(2026, 8, 25, 14, tzinfo=UTC),
        "next_action": "Send sandbox follow-up",
        "next_action_due": date(2026, 8, 27),
        "who_owes_whom": "Us",
        "key_contacts": "Zelia — Champion; Lindsey — Security",
    }
    values.update(overrides)
    return ActiveAccountRecord(**values)


def _action(**overrides) -> ActionRecord:
    values = {
        "action_id": "action-ramp",
        "opportunity_id": "opp-ramp",
        "account_id": "acct-ramp",
        "account": "Ramp",
        "owner": "Arian",
        "lead_id": "10452",
        "contact": "Zelia Pantani",
        "why_now": "Post-meeting commitment",
        "next_action": "Send sandbox follow-up",
        "next_action_due": date(2026, 8, 27),
        "who_owes_whom": "Us",
        "channel": "Email",
    }
    values.update(overrides)
    return ActionRecord(**values)


def test_public_schemas_are_two_compact_surfaces_with_stable_ids():
    assert v2.ACTIVE_ACCOUNT_HEADERS == (
        "Account", "Owner", "Stage", "Attention",
        "Why active", "Evidence tier", "Outreach", "Last meaningful touch", "Next action",
        "Next action due date", "Waiting until", "Who owes", "Key contacts",
        "Manual pin", "Opportunity ID", "Account ID", "Human sync baseline",
    )
    assert v2.ACTION_HEADERS == (
        "Account", "Owner", "Contact", "Why now", "Outreach", "Next action", "Next action due date",
        "Waiting until", "Who owes", "Channel", "Draft", "Handled",
        "Disposition", "Action ID", "Opportunity ID", "Account ID", "Lead ID",
        "Human sync baseline",
    )
    assert v2.ACTIVE_ACCOUNT_TECHNICAL_FIELDS == (
        "Opportunity ID", "Account ID", "Human sync baseline",
    )
    assert v2.ACTION_TECHNICAL_FIELDS == (
        "Action ID", "Opportunity ID", "Account ID", "Lead ID", "Human sync baseline",
    )
    assert not hasattr(v2, "pipeline_adapter")
    assert not hasattr(v2, "recovery_adapter")


def test_serializer_is_one_row_per_account_and_actions_cannot_readmit_noise():
    with pytest.raises(SheetsError, match="duplicate active account Account ID"):
        build_crm_v2_view_rows(
            [_active(), _active(opportunity_id="opp-ramp-secondary")],
            [],
        )

    with pytest.raises(SheetsError, match="do not belong to an admitted"):
        build_crm_v2_view_rows([_active()], [_action(account_id="acct-old-lead")])


def test_serializer_keeps_evidence_and_ownership_legible_without_source_threads():
    rows = build_crm_v2_view_rows([_active()], [_action()])

    account = rows.active_accounts[0]
    assert account[v2.COL_ACCOUNT] == "Ramp"
    assert account[v2.COL_WHY_ACTIVE] == "Meeting on 2026-08-25"
    assert account[v2.COL_EVIDENCE_TIER] == "T1 Meeting"
    assert account[v2.COL_ATTENTION] == "Now"
    assert account[v2.COL_OUTREACH] == "Allowed"
    assert account[v2.COL_WHO_OWES] == "Us"
    assert account[v2.COL_LAST_MEANINGFUL_TOUCH] == "2026-08-25T14:00:00+00:00"
    assert account[v2.COL_NEXT_ACTION_DUE] == "2026-08-27"

    action = rows.actions[0]
    assert action[v2.COL_ACTION_ID] == "action-ramp"
    assert action[v2.COL_LEAD_ID] == "10452"
    assert action[v2.COL_WHY_NOW] == "Post-meeting commitment"
    assert action[v2.COL_OUTREACH] == "Allowed"


def test_action_serializer_requires_stable_parents_but_allows_account_level_work():
    with pytest.raises(SheetsError, match="missing Account ID"):
        action_row(_action(account_id=""))
    with pytest.raises(SheetsError, match="missing Owner"):
        action_row(_action(owner=""))
    account_level = action_row(_action(lead_id="", contact=""))
    assert account_level[v2.COL_LEAD_ID] == ""
    assert account_level[v2.COL_CONTACT] == "Account-level"


def test_active_serializer_requires_legible_admission_evidence():
    with pytest.raises(SheetsError, match="missing Why active"):
        build_crm_v2_view_rows([_active(why_active="")], [])
    with pytest.raises(SheetsError, match="missing Evidence tier"):
        build_crm_v2_view_rows([_active(evidence_tier="")], [])
    with pytest.raises(SheetsError, match="invalid Attention"):
        build_crm_v2_view_rows([_active(attention="Later maybe")], [])
    with pytest.raises(SheetsError, match="invalid Outreach"):
        build_crm_v2_view_rows([_active(outreach="Maybe")], [])


def test_active_accounts_three_way_merge_preserves_human_edit_and_unknown_formula():
    headers = [*v2.ACTIVE_ACCOUNT_HEADERS, "Operator formula"]
    worksheet = FakeWorksheet(
        v2.ACTIVE_ACCOUNTS_TAB,
        [
            headers,
            _row(
                headers,
                **{
                    v2.COL_OPPORTUNITY_ID: "opp-ramp",
                    v2.COL_ACCOUNT_ID: "acct-ramp",
                    v2.COL_ACCOUNT: "Ramp",
                    v2.COL_OWNER: "Human owner",
                    v2.COL_HUMAN_BASELINE: _baseline(
                        v2.ACTIVE_ACCOUNT_HUMAN_FIELDS,
                        **{v2.COL_OWNER: "Arian"},
                    ),
                    "Operator formula": "=ROW()",
                },
            ),
        ],
    )

    plan = v2.active_accounts_adapter(worksheet).plan([
        {
            v2.COL_OPPORTUNITY_ID: "opp-ramp",
            v2.COL_ACCOUNT_ID: "acct-ramp",
            v2.COL_ACCOUNT: "Ramp",
            v2.COL_OWNER: "Arian",
            v2.COL_WHY_ACTIVE: "Email reply on 2026-08-26",
        }
    ])

    assert plan.imports == [
        crm_sheets.HumanFieldImport("opp-ramp", v2.COL_OWNER, "Human owner")
    ]
    with pytest.raises(SheetsError, match="human-field import"):
        v2.active_accounts_adapter(worksheet).apply(plan)
    assert worksheet.rows[1][-1] == "=ROW()"


def test_active_accounts_pipeline_stage_is_a_system_projection_not_a_sheet_import():
    headers = list(v2.ACTIVE_ACCOUNT_HEADERS)
    worksheet = FakeWorksheet(
        v2.ACTIVE_ACCOUNTS_TAB,
        [
            headers,
            _row(
                headers,
                **{
                    v2.COL_OPPORTUNITY_ID: "opp-ramp",
                    v2.COL_ACCOUNT_ID: "acct-ramp",
                    v2.COL_ACCOUNT: "Ramp",
                    v2.COL_STAGE: "Discovery",
                    v2.COL_HUMAN_BASELINE: _baseline(
                        v2.ACTIVE_ACCOUNT_HUMAN_FIELDS,
                    ),
                },
            ),
        ],
    )

    plan = v2.active_accounts_adapter(worksheet).plan([
        {
            v2.COL_OPPORTUNITY_ID: "opp-ramp",
            v2.COL_ACCOUNT_ID: "acct-ramp",
            v2.COL_ACCOUNT: "Ramp",
            v2.COL_STAGE: "Potential / Triage",
        }
    ])

    assert plan.imports == []
    assert any(change.column == v2.COL_STAGE for change in plan.changes)


def test_missing_active_account_clears_managed_cells_but_not_key_or_formula():
    headers = [*v2.ACTIVE_ACCOUNT_HEADERS, "Operator formula"]
    worksheet = FakeWorksheet(
        v2.ACTIVE_ACCOUNTS_TAB,
        [
            headers,
            _row(
                headers,
                **{
                    v2.COL_OPPORTUNITY_ID: "opp-old",
                    v2.COL_ACCOUNT_ID: "acct-old",
                    v2.COL_ACCOUNT: "Old LinkedIn-only account",
                    v2.COL_OWNER: "Arian",
                    v2.COL_WHY_ACTIVE: "Legacy reply",
                    v2.COL_HUMAN_BASELINE: _baseline(
                        v2.ACTIVE_ACCOUNT_HUMAN_FIELDS,
                        **{v2.COL_OWNER: "Arian"},
                    ),
                    "Operator formula": "=ROW()",
                },
            ),
        ],
    )
    adapter = v2.active_accounts_adapter(worksheet)

    plan = adapter.plan([])
    adapter.apply(plan)

    row = worksheet.rows[1]
    assert row[headers.index(v2.COL_OPPORTUNITY_ID)] == "opp-old"
    assert row[headers.index(v2.COL_ACCOUNT)] == ""
    assert row[headers.index(v2.COL_WHY_ACTIVE)] == ""
    assert row[headers.index("Operator formula")] == "=ROW()"


def test_new_action_writes_after_hidden_stable_history_instead_of_table_append():
    headers = list(v2.ACTION_HEADERS)
    worksheet = FakeWorksheet(
        v2.ACTIONS_TAB,
        [
            headers,
            _row(
                headers,
                **{
                    v2.COL_ACTION_ID: "action-current",
                    v2.COL_OPPORTUNITY_ID: "opp-current",
                    v2.COL_ACCOUNT_ID: "account-current",
                    v2.COL_ACCOUNT: "Current",
                },
            ),
            _row(
                headers,
                **{
                    v2.COL_ACTION_ID: "action-history",
                    v2.COL_OPPORTUNITY_ID: "opp-history",
                    v2.COL_ACCOUNT_ID: "account-history",
                    v2.COL_ACCOUNT: "Old visible work",
                },
            ),
        ],
    )
    adapter = v2.actions_adapter(worksheet)
    plan = adapter.plan([
        {
            v2.COL_ACTION_ID: "action-current",
            v2.COL_OPPORTUNITY_ID: "opp-current",
            v2.COL_ACCOUNT_ID: "account-current",
            v2.COL_ACCOUNT: "Current",
        },
        {
            v2.COL_ACTION_ID: "action-new",
            v2.COL_OPPORTUNITY_ID: "opp-new",
            v2.COL_ACCOUNT_ID: "account-new",
            v2.COL_ACCOUNT: "New work",
        },
    ])

    adapter.apply(plan)

    action_id_index = headers.index(v2.COL_ACTION_ID)
    account_index = headers.index(v2.COL_ACCOUNT)
    assert worksheet.rows[2][action_id_index] == "action-history"
    assert worksheet.rows[2][account_index] == ""
    assert worksheet.rows[3][action_id_index] == "action-new"
    assert worksheet.rows[3][account_index] == "New work"
    assert worksheet.appended == []
    assert worksheet.updates[-1]["range"].startswith("A4:")


def test_actions_are_one_filterable_sheet_and_completed_history_does_not_clutter_it():
    headers = [*v2.ACTION_HEADERS, "Operator formula"]
    worksheet = FakeWorksheet(
        v2.ACTIONS_TAB,
        [
            headers,
            _row(
                headers,
                **{
                    v2.COL_ACTION_ID: "action-old",
                    v2.COL_ACCOUNT: "Old account",
                    v2.COL_OWNER: "Arian",
                    v2.COL_HANDLED: "TRUE",
                    v2.COL_HUMAN_BASELINE: _baseline(
                        v2.ACTION_HUMAN_FIELDS,
                        **{v2.COL_HANDLED: "TRUE"},
                    ),
                    "Operator formula": "=ROW()",
                },
            ),
        ],
    )
    adapter = v2.actions_adapter(worksheet)

    plan = adapter.plan([])
    adapter.apply(plan)

    row = worksheet.rows[1]
    assert row[headers.index(v2.COL_ACTION_ID)] == "action-old"
    assert row[headers.index(v2.COL_ACCOUNT)] == ""
    assert row[headers.index(v2.COL_HANDLED)] == ""
    assert row[headers.index("Operator formula")] == "=ROW()"
