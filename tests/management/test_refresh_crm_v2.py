"""Focused safety tests for the CRM v2 refresh/cutover orchestrator."""
from __future__ import annotations

import io
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone
from gspread.exceptions import WorksheetNotFound

from crm.models import (
    Account,
    Lead,
    Opportunity,
    OpportunityAction,
    OpportunityContact,
    SalesOwner,
)
from linkedin.crm_v2_publish import (
    ActionRecord,
    ActiveAccountRecord,
    build_crm_v2_view_rows,
)
from linkedin.exceptions import SheetsError
from linkedin.management.commands.refresh_crm_v2 import (
    _apply_first_cutover,
    _assert_preview_inputs_match,
    _assert_preview_summary_matches,
    _build_plans,
    _evidence_counts,
    _import_legacy_human_state,
    _load_reviewed_preview,
)
from linkedin.notifications import crm_sheets
from linkedin.notifications import crm_v2_sheets as v2
from linkedin.notifications.crm_v2_layout import build_layout_requests


pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _stub_people_publisher(monkeypatch):
    from linkedin.management.commands import sync_sheets

    monkeypatch.setattr(
        sync_sheets,
        "run_people_sync",
        lambda *, dry_run, stdout, stderr, lock_held: {
            "status": "planned" if dry_run else "published",
            "source_leads": Lead.objects.count(),
            "rows_before": Lead.objects.count(),
            "rows_after": Lead.objects.count(),
            "errored": 0,
            "duplicate_lead_ids": 0,
        },
    )


def _column_number(letters: str) -> int:
    value = 0
    for character in letters:
        value = value * 26 + ord(character) - 64
    return value


def _split_cell(reference: str) -> tuple[int, int]:
    letters = "".join(character for character in reference if character.isalpha())
    digits = "".join(character for character in reference if character.isdigit())
    return int(digits), _column_number(letters)


class MemoryWorksheet:
    def __init__(self, title, sheet_id, rows=None, *, fail_append=False):
        self.title = title
        self.id = sheet_id
        self.rows = [list(row) for row in (rows or [])]
        self.row_count = max(1000, len(self.rows) + 10)
        self.col_count = max((len(row) for row in self.rows), default=0)
        self.fail_append = fail_append

    def get_all_values(self, value_render_option=None):
        return [list(row) for row in self.rows]

    def add_cols(self, count):
        self.col_count += count

    def update(self, *, values, range_name, value_input_option=None):
        if self.fail_append:
            raise SheetsError("test append failure")
        start = range_name.split(":", 1)[0]
        row_number, column_number = _split_cell(start)
        for row_offset, values_row in enumerate(values):
            target_row = row_number + row_offset
            while len(self.rows) < target_row:
                self.rows.append([])
            row = self.rows[target_row - 1]
            while len(row) < column_number - 1:
                row.append("")
            for offset, value in enumerate(values_row):
                index = column_number - 1 + offset
                while len(row) <= index:
                    row.append("")
                row[index] = str(value)

    def batch_update(self, updates, value_input_option=None):
        for update in updates:
            row_number, column_number = _split_cell(
                update["range"].split(":", 1)[0]
            )
            while len(self.rows) < row_number:
                self.rows.append([])
            row = self.rows[row_number - 1]
            while len(row) < column_number:
                row.append("")
            row[column_number - 1] = str(update["values"][0][0])

    def append_rows(self, rows, value_input_option=None, table_range=None):
        if self.fail_append:
            raise SheetsError("test append failure")
        self.rows.extend([list(row) for row in rows])


class MemorySpreadsheet:
    def __init__(self, worksheets, *, spreadsheet_id="workbook-v2"):
        self.id = spreadsheet_id
        self._worksheets = list(worksheets)
        self.batch_calls = []
        self.added = []
        self.fail_second_stage_append = False

    def worksheets(self):
        return list(self._worksheets)

    def worksheet(self, title):
        for worksheet in self._worksheets:
            if worksheet.title == title:
                return worksheet
        raise WorksheetNotFound(title)

    def add_worksheet(self, *, title, rows, cols):
        worksheet = MemoryWorksheet(
            title,
            1000 + len(self._worksheets),
            fail_append=(self.fail_second_stage_append and len(self.added) == 1),
        )
        worksheet.row_count = rows
        worksheet.col_count = cols
        self._worksheets.append(worksheet)
        self.added.append(worksheet)
        return worksheet

    def duplicate_sheet(self, source_sheet_id, *, new_sheet_name):
        source = next(
            worksheet for worksheet in self._worksheets
            if worksheet.id == source_sheet_id
        )
        duplicate = MemoryWorksheet(
            new_sheet_name,
            1000 + len(self._worksheets),
            rows=source.rows,
        )
        duplicate.row_count = source.row_count
        duplicate.col_count = source.col_count
        self._worksheets.append(duplicate)
        self.added.append(duplicate)
        return duplicate

    def batch_update(self, body):
        self.batch_calls.append(body)
        for request in body.get("requests", []):
            if "updateSheetProperties" in request:
                properties = request["updateSheetProperties"]["properties"]
                worksheet = next(
                    item for item in self._worksheets
                    if item.id == properties["sheetId"]
                )
                worksheet.title = properties["title"]
            elif "deleteSheet" in request:
                sheet_id = request["deleteSheet"]["sheetId"]
                self._worksheets = [
                    item for item in self._worksheets if item.id != sheet_id
                ]


def _people_worksheet(*leads, dont_send_ids=()):
    rows = [["Lead ID", "LinkedIn URL", "Outreach status"]]
    stopped = set(dont_send_ids)
    rows.extend([
        [
            str(lead.id),
            lead.linkedin_url or "",
            "Don't send" if lead.id in stopped else "",
        ]
        for lead in leads
    ])
    return MemoryWorksheet("People", 900, rows)


def _database_view():
    rows = build_crm_v2_view_rows(
        [
            ActiveAccountRecord(
                opportunity_id="opp-1",
                account_id="account-1",
                account="Example account",
                owner="Arian",
                stage="Prospecting",
                attention="Needs contact",
                why_active="Pinned by you",
                evidence_tier="Authoritative",
                next_action="Define the next step",
                manual_pin=True,
            )
        ],
        [
            ActionRecord(
                action_id="action-1",
                opportunity_id="opp-1",
                account_id="account-1",
                account="Example account",
                owner="Arian",
                why_now="No next step defined",
                next_action="Define and schedule the next step",
            )
        ],
    )
    return SimpleNamespace(rows=rows, active_baselines={}, action_baselines={})


def test_default_command_executes_exact_db_path_then_rolls_everything_back(monkeypatch):
    from linkedin import conf
    from linkedin.notifications import sheets

    lead = Lead.objects.create(
        company_name="Rollback Account",
        email="person@rollback-account.example",
    )
    SalesOwner.objects.get_or_create(handle="Arian")
    spreadsheet = MemorySpreadsheet(
        [_people_worksheet(lead)],
        spreadsheet_id="dry-workbook",
    )
    monkeypatch.setattr(conf, "GOOGLE_SHEETS_ID", "dry-workbook")
    monkeypatch.setattr(sheets, "_gspread_client", lambda: spreadsheet)

    stdout = io.StringIO()
    call_command(
        "refresh_crm_v2",
        "--skip-sales-motion",
        "--manual-pin",
        "Rollback Account",
        "--owner-override",
        "Rollback Account=Arian",
        stdout=stdout,
    )

    payload = json.loads(stdout.getvalue())
    assert payload["mode"] == "dry-run"
    assert payload["publication"]["mode"] == "first_cutover"
    assert payload["sends_performed"] == 0
    assert payload["evidence"]["active"] == 1
    assert not Account.objects.filter(name="Rollback Account").exists()
    assert spreadsheet.added == []
    assert spreadsheet.batch_calls == []


def test_people_prerequisite_blocks_before_v2_work_on_errors_or_duplicate_ids(
    monkeypatch,
):
    from linkedin import conf
    from linkedin.management.commands import sync_sheets
    from linkedin.notifications import sheets

    lead = Lead.objects.create(
        company_name="People Gate Account",
        email="person@people-gate.example",
    )
    spreadsheet = MemorySpreadsheet(
        [_people_worksheet(lead)],
        spreadsheet_id="people-gate-workbook",
    )
    monkeypatch.setattr(conf, "GOOGLE_SHEETS_ID", "people-gate-workbook")
    monkeypatch.setattr(sheets, "_gspread_client", lambda: spreadsheet)
    monkeypatch.setattr(
        sync_sheets,
        "run_people_sync",
        lambda **_kwargs: {"errored": 1, "duplicate_lead_ids": 1},
    )

    with pytest.raises(CommandError, match="People publisher reported"):
        call_command(
            "refresh_crm_v2",
            "--skip-sales-motion",
            stdout=io.StringIO(),
        )

    assert Account.objects.count() == 0
    assert spreadsheet.added == []
    assert spreadsheet.batch_calls == []


def test_existing_dry_run_imports_reconciles_and_replans_inside_rollback(monkeypatch):
    from linkedin import conf
    from linkedin.crm_v2_evidence import collect_account_evidence
    from linkedin.crm_v2_view_builder import build_crm_v2_database_view
    from linkedin.notifications import sheets

    arian, _ = SalesOwner.objects.get_or_create(handle="Arian")
    athena, _ = SalesOwner.objects.get_or_create(handle="Athena")
    lead = Lead.objects.create(
        first_name="Person",
        company_name="Existing Account",
        email="person@existing-account.example",
    )
    account = Account.objects.create(name="Existing Account")
    opportunity = Opportunity.objects.create(
        account=account,
        owner=arian,
        source=Opportunity.Source.MANUAL,
        manual_pin=True,
    )
    OpportunityContact.objects.create(
        opportunity=opportunity,
        lead=lead,
        is_primary=True,
    )
    OpportunityAction.objects.create(
        opportunity=opportunity,
        target_lead=lead,
        description="Call today",
        due_on=timezone.localdate(),
        idempotency_key="human:existing-account",
    )
    view = build_crm_v2_database_view(
        collect_account_evidence(now=timezone.now())
    )
    active = MemoryWorksheet("Active Accounts", 1)
    actions = MemoryWorksheet("Actions", 2)
    active_plan, action_plan = _build_plans(
        active,
        actions,
        view,
        crm_v2_sheets=v2,
    )
    v2.active_accounts_adapter(active).apply(active_plan)
    v2.actions_adapter(actions).apply(action_plan)
    owner_column = list(v2.ACTIVE_ACCOUNT_HEADERS).index(v2.COL_OWNER)
    active.rows[1][owner_column] = athena.handle

    spreadsheet = MemorySpreadsheet(
        [_people_worksheet(lead), active, actions],
        spreadsheet_id="existing-workbook",
    )
    monkeypatch.setattr(conf, "GOOGLE_SHEETS_ID", "existing-workbook")
    monkeypatch.setattr(sheets, "_gspread_client", lambda: spreadsheet)
    stdout = io.StringIO()

    call_command(
        "refresh_crm_v2",
        "--skip-sales-motion",
        stdout=stdout,
    )

    payload = json.loads(stdout.getvalue())
    assert payload["publication"]["mode"] == "in_place"
    assert payload["human_imports"]["active_account_edits"] == 1
    assert payload["sheet_plan"]["imports"] == 0
    opportunity.refresh_from_db()
    assert opportunity.owner_id == arian.id
    assert spreadsheet.batch_calls == []


def test_existing_dry_run_reports_first_pass_pipeline_and_action_mutations(monkeypatch):
    from linkedin import conf
    from linkedin.notifications import sheets

    lead = Lead.objects.create(
        first_name="Champion",
        company_name="Pipeline Candidate",
        email="champion@pipeline-candidate.example",
    )
    account = Account.objects.create(name="Pipeline Candidate")
    opportunity = Opportunity.objects.create(
        account=account,
        source=Opportunity.Source.MANUAL,
        manual_pin=True,
    )
    OpportunityContact.objects.create(
        opportunity=opportunity,
        lead=lead,
        is_primary=True,
    )
    spreadsheet = MemorySpreadsheet(
        [
            _people_worksheet(lead),
            MemoryWorksheet("Active Accounts", 1),
            MemoryWorksheet("Actions", 2),
        ],
        spreadsheet_id="pipeline-mutation-workbook",
    )
    monkeypatch.setattr(conf, "GOOGLE_SHEETS_ID", "pipeline-mutation-workbook")
    monkeypatch.setattr(sheets, "_gspread_client", lambda: spreadsheet)
    stdout = io.StringIO()

    call_command(
        "refresh_crm_v2",
        "--skip-sales-motion",
        stdout=stdout,
    )

    payload = json.loads(stdout.getvalue())
    assert payload["pipeline_triage"]["promoted"] == 1
    assert payload["actions"]["actions_created"] == 1
    assert payload["sheet_plan"]["imports"] == 0
    opportunity.refresh_from_db()
    assert opportunity.pipeline_stage == ""
    assert OpportunityAction.objects.filter(opportunity=opportunity).count() == 0
    assert spreadsheet.batch_calls == []


def test_apply_requires_recent_private_reviewed_preview(tmp_path):
    with pytest.raises(CommandError, match="requires --reviewed-preview"):
        call_command("refresh_crm_v2", "--apply", "--skip-sales-motion")

    path = tmp_path / "preview.json"
    payload = {
        "schema": "openoutreach.crm-v2-preview.v1",
        "generated_at": (timezone.now() - timedelta(days=2)).isoformat(),
        "summary": {"active_accounts": 0},
        "inputs": {},
        "active_accounts": [],
    }
    path.write_text(json.dumps(payload))
    path.chmod(0o600)
    with pytest.raises(CommandError, match="not recent"):
        _load_reviewed_preview(path, now=timezone.now())

    payload["generated_at"] = timezone.now().isoformat()
    path.write_text(json.dumps(payload))
    path.chmod(0o644)
    with pytest.raises(CommandError, match="private regular file"):
        _load_reviewed_preview(path, now=timezone.now())


def test_routine_flag_requires_apply_and_rejects_reviewed_preview():
    with pytest.raises(CommandError, match="only valid with --apply"):
        call_command("refresh_crm_v2", "--routine", "--skip-sales-motion")

    with pytest.raises(CommandError, match="cannot be combined"):
        call_command(
            "refresh_crm_v2",
            "--apply",
            "--routine",
            "--reviewed-preview",
            "/private/review.json",
            "--skip-sales-motion",
        )


@pytest.mark.parametrize(
    ("titles", "message"),
    [
        (["People"], "both canonical CRM v2 tabs"),
        (
            ["People", "Active Accounts", "Actions", "Pipeline"],
            "legacy canonical CRM tabs",
        ),
    ],
)
def test_routine_apply_fails_before_people_or_writes_unless_cutover_is_clean(
    monkeypatch,
    titles,
    message,
):
    from linkedin import conf
    from linkedin.management.commands import sync_sheets
    from linkedin.notifications import sheets

    spreadsheet = MemorySpreadsheet(
        [MemoryWorksheet(title, index + 1) for index, title in enumerate(titles)],
        spreadsheet_id="routine-gate-workbook",
    )
    monkeypatch.setattr(conf, "GOOGLE_SHEETS_ID", "routine-gate-workbook")
    monkeypatch.setattr(sheets, "_gspread_client", lambda: spreadsheet)
    people_calls = []
    monkeypatch.setattr(
        sync_sheets,
        "run_people_sync",
        lambda **kwargs: people_calls.append(kwargs),
    )

    with pytest.raises(CommandError, match=message):
        call_command(
            "refresh_crm_v2",
            "--apply",
            "--routine",
            "--skip-sales-motion",
            stdout=io.StringIO(),
        )

    assert people_calls == []
    assert spreadsheet.added == []
    assert spreadsheet.batch_calls == []


def test_reviewed_preview_fails_closed_on_input_or_recomputed_count_mismatch():
    preview = {
        "inputs": {
            "sales_motion_accounts": ["Ramp"],
            "manual_pins": ["StackArmor"],
            "owner_overrides": {"Ramp": "Arian"},
        },
        "summary": {
            "account_groups_evaluated": 2,
            "active_accounts": 2,
            "people_only_accounts": 0,
            "admission_reasons": {"manual_pin": 1, "sales_motion_active": 1},
        },
    }
    with pytest.raises(CommandError, match="inputs do not match"):
        _assert_preview_inputs_match(
            preview,
            {
                "sales_motion_accounts": ["Ramp"],
                "manual_pins": [],
                "owner_overrides": {"Ramp": "Arian"},
            },
        )
    with pytest.raises(CommandError, match="no longer matches"):
        _assert_preview_summary_matches(preview, [])


def test_reviewed_preview_rejects_identity_swap_with_identical_aggregates():
    from linkedin.crm_v2_evidence import collect_account_evidence
    from linkedin.management.commands.preview_crm_v2 import _serialize_row

    Lead.objects.create(
        company_name="Identity Account",
        email="person@identity-account.example",
    )
    rows = collect_account_evidence(
        manual_account_pins=["Identity Account"],
        now=timezone.now(),
    )
    active = [row for row in rows if row.decision.admitted]
    preview = {
        "summary": {
            "account_groups_evaluated": len(rows),
            "active_accounts": len(active),
            "people_only_accounts": len(rows) - len(active),
            "admission_reasons": {"manual_pin": 1},
        },
        "active_accounts": [_serialize_row(row) for row in active],
    }
    preview["active_accounts"][0]["account_key"] = "domain:swapped.example"

    with pytest.raises(CommandError, match="exact active-account or reminder identity"):
        _assert_preview_summary_matches(preview, rows)


def test_first_cutover_uses_one_exact_title_delete_batch_and_protects_other_tabs():
    old_titles = [
        "People",
        "GTM",
        "Sales Motion",
        "Opportunities",
        "Pipeline",
        "Recovery",
        "Arian - Followups",
        "Athena - Followups",
        "Chuka - Followups",
        "Leili - Followups",
        "Arian - Followups Legacy 2026-08-26",
    ]
    old = [MemoryWorksheet(title, index + 1) for index, title in enumerate(old_titles)]
    spreadsheet = MemorySpreadsheet(old)
    inventory = {worksheet.title: worksheet for worksheet in old}

    active_plan, action_plan, cutover_state = _apply_first_cutover(
        spreadsheet,
        worksheets=inventory,
        database_view=_database_view(),
        crm_sheets=crm_sheets,
        crm_v2_sheets=v2,
        apply_layout=lambda *_args, **_kwargs: 0,
        owner_values=("Arian",),
    )

    assert len(spreadsheet.batch_calls) == 1
    requests = spreadsheet.batch_calls[0]["requests"]
    renamed = [
        request["updateSheetProperties"]["properties"]["title"]
        for request in requests
        if "updateSheetProperties" in request
    ]
    assert renamed[:2] == ["Active Accounts", "Actions"]
    assert [
        request["updateSheetProperties"]["properties"].get("index")
        for request in requests[:2]
    ] == [0, 1]
    assert len(renamed) == 9
    assert not any("deleteSheet" in request for request in requests)
    archived_ids = set(cutover_state.archived_sheet_ids.values())
    exact_archived_titles = {
        old_titles[index] for index, worksheet in enumerate(old)
        if worksheet.id in archived_ids
    }
    assert exact_archived_titles == {
        "Opportunities", "Pipeline", "Recovery",
        "Arian - Followups", "Athena - Followups",
        "Chuka - Followups", "Leili - Followups",
    }
    assert "People" not in exact_archived_titles
    assert "Arian - Followups Legacy 2026-08-26" not in exact_archived_titles
    assert len(active_plan.baseline_updates) == 1
    assert len(action_plan.baseline_updates) == 1


def test_failure_before_cutover_preserves_old_titles_and_has_no_atomic_batch():
    old = [MemoryWorksheet("Opportunities", 1), MemoryWorksheet("People", 2)]
    spreadsheet = MemorySpreadsheet(old)
    spreadsheet.fail_second_stage_append = True

    with pytest.raises(SheetsError, match="append failure"):
        _apply_first_cutover(
            spreadsheet,
            worksheets={worksheet.title: worksheet for worksheet in old},
            database_view=_database_view(),
            crm_sheets=crm_sheets,
            crm_v2_sheets=v2,
            apply_layout=lambda *_args, **_kwargs: 0,
            owner_values=("Arian",),
        )

    assert spreadsheet.batch_calls == []
    assert [worksheet.title for worksheet in old] == ["Opportunities", "People"]


def test_apply_failure_before_cutover_rolls_back_db_and_never_commits_baselines(
    monkeypatch,
    tmp_path,
):
    from linkedin import conf
    from linkedin import crm_sheet_import
    from linkedin.management.commands import refresh_crm_v2 as command_module
    from linkedin.notifications import sheets

    lead = Lead.objects.create(
        company_name="Failure Account",
        email="person@failure-account.example",
    )
    SalesOwner.objects.get_or_create(handle="Arian")
    old = [_people_worksheet(lead)]
    spreadsheet = MemorySpreadsheet(old, spreadsheet_id="failure-workbook")
    monkeypatch.setattr(conf, "GOOGLE_SHEETS_ID", "failure-workbook")
    monkeypatch.setattr(sheets, "_gspread_client", lambda: spreadsheet)
    preview = tmp_path / "reviewed.json"
    call_command(
        "preview_crm_v2",
        "--skip-sales-motion",
        "--manual-pin",
        "Failure Account",
        "--owner-override",
        "Failure Account=Arian",
        "--output",
        str(preview),
        stdout=io.StringIO(),
    )
    monkeypatch.setattr(
        crm_sheets,
        "backup_spreadsheet",
        lambda *_args, **_kwargs: tmp_path / "backup.json",
    )
    monkeypatch.setattr(
        command_module,
        "_apply_first_cutover",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            SheetsError("pre-cutover verification failed")
        ),
    )
    commits = []
    monkeypatch.setattr(
        crm_sheet_import,
        "commit_sheet_baselines",
        lambda updates, **_kwargs: commits.append(tuple(updates)),
    )
    monkeypatch.setattr(
        crm_sheet_import,
        "commit_followup_baselines",
        lambda updates, **_kwargs: commits.append(tuple(updates)),
    )

    with pytest.raises(CommandError, match="pre-cutover verification failed"):
        call_command(
            "refresh_crm_v2",
            "--apply",
            "--reviewed-preview",
            str(preview),
            "--skip-sales-motion",
            "--manual-pin",
            "Failure Account",
            "--owner-override",
            "Failure Account=Arian",
            stdout=io.StringIO(),
        )

    assert commits == []
    assert not Account.objects.filter(name="Failure Account").exists()
    assert spreadsheet.batch_calls == []
    assert [worksheet.title for worksheet in old] == ["People"]


def test_reviewed_first_cutover_applies_db_and_commits_after_atomic_batch(
    monkeypatch,
    tmp_path,
):
    from crm.models import OpportunitySheetState
    from linkedin import conf
    from linkedin.crm_v2_evidence import collect_account_evidence
    from linkedin.crm_v2_view_builder import build_crm_v2_database_view
    from linkedin.notifications import crm_v2_layout, sheets

    lead = Lead.objects.create(
        company_name="Reviewed Account",
        email="person@reviewed-account.example",
    )
    SalesOwner.objects.get_or_create(handle="Arian")
    old = [_people_worksheet(lead)]
    spreadsheet = MemorySpreadsheet(old, spreadsheet_id="apply-workbook")
    monkeypatch.setattr(conf, "GOOGLE_SHEETS_ID", "apply-workbook")
    monkeypatch.setattr(sheets, "_gspread_client", lambda: spreadsheet)
    preview = tmp_path / "reviewed.json"
    call_command(
        "preview_crm_v2",
        "--skip-sales-motion",
        "--manual-pin",
        "Reviewed Account",
        "--owner-override",
        "Reviewed Account=Arian",
        "--output",
        str(preview),
        stdout=io.StringIO(),
    )
    backups = []
    monkeypatch.setattr(
        crm_sheets,
        "backup_spreadsheet",
        lambda *_args, **_kwargs: backups.append(True) or tmp_path / "backup.json",
    )
    monkeypatch.setattr(crm_v2_layout, "apply_layout", lambda *_args, **_kwargs: 0)
    stdout = io.StringIO()

    call_command(
        "refresh_crm_v2",
        "--apply",
        "--reviewed-preview",
        str(preview),
        "--skip-sales-motion",
        "--manual-pin",
        "Reviewed Account",
        "--owner-override",
        "Reviewed Account=Arian",
        stdout=stdout,
    )

    payload = json.loads(stdout.getvalue())
    assert payload["status"] == "applied"
    assert payload["publication"]["atomic_cutover"] is True
    assert payload["baselines"]["committed"] is True
    assert backups == [True]
    assert len(spreadsheet.batch_calls) == 1
    account = Account.objects.get(name="Reviewed Account")
    opportunity = account.opportunities.get()
    assert OpportunitySheetState.objects.filter(opportunity=opportunity).exists()
    assert opportunity.actions.filter(sheet_published_at__isnull=False).exists()
    current_view = build_crm_v2_database_view(
        collect_account_evidence(
            manual_account_pins=["Reviewed Account"],
            owner_overrides={"Reviewed Account": "Arian"},
            now=timezone.now(),
        )
    )
    rerun_active, rerun_action = _build_plans(
        spreadsheet.added[0],
        spreadsheet.added[1],
        current_view,
        crm_v2_sheets=v2,
    )
    assert rerun_active.appends == []
    assert rerun_active.changes == []
    assert rerun_active.imports == []
    assert rerun_action.appends == []
    assert rerun_action.changes == []
    assert rerun_action.imports == []


def test_routine_apply_stages_both_tabs_then_swaps_and_cleans_after_db_commit(
    monkeypatch,
    tmp_path,
):
    from linkedin import conf
    from linkedin.crm_v2_evidence import collect_account_evidence
    from linkedin.crm_v2_view_builder import build_crm_v2_database_view
    from linkedin.notifications import crm_v2_layout, sheets

    arian, _ = SalesOwner.objects.get_or_create(handle="Arian")
    lead = Lead.objects.create(
        first_name="Person",
        company_name="Routine Account",
        email="person@routine-account.example",
    )
    account = Account.objects.create(name="Routine Account")
    opportunity = Opportunity.objects.create(
        account=account,
        owner=arian,
        source=Opportunity.Source.MANUAL,
        manual_pin=True,
    )
    OpportunityContact.objects.create(
        opportunity=opportunity,
        lead=lead,
        is_primary=True,
    )
    OpportunityAction.objects.create(
        opportunity=opportunity,
        target_lead=lead,
        description="Call today",
        due_on=timezone.localdate(),
        idempotency_key="human:routine-account",
    )
    current_view = build_crm_v2_database_view(
        collect_account_evidence(now=timezone.now())
    )
    active = MemoryWorksheet("Active Accounts", 1)
    actions = MemoryWorksheet("Actions", 2)
    active_plan, action_plan = _build_plans(
        active,
        actions,
        current_view,
        crm_v2_sheets=v2,
    )
    v2.active_accounts_adapter(active).apply(active_plan)
    v2.actions_adapter(actions).apply(action_plan)
    spreadsheet = MemorySpreadsheet(
        [_people_worksheet(lead), active, actions],
        spreadsheet_id="routine-workbook",
    )
    monkeypatch.setattr(conf, "GOOGLE_SHEETS_ID", "routine-workbook")
    monkeypatch.setattr(sheets, "_gspread_client", lambda: spreadsheet)
    monkeypatch.setattr(
        crm_sheets,
        "backup_spreadsheet",
        lambda *_args, **_kwargs: tmp_path / "backup.json",
    )
    monkeypatch.setattr(crm_v2_layout, "apply_layout", lambda *_args, **_kwargs: 0)
    stdout = io.StringIO()

    call_command(
        "refresh_crm_v2",
        "--apply",
        "--routine",
        "--skip-sales-motion",
        stdout=stdout,
    )

    payload = json.loads(stdout.getvalue())
    assert payload["publication"]["mode"] == "in_place"
    assert payload["publication"]["gate"] == "routine"
    assert payload["publication"]["atomic_cutover"] is True
    assert payload["publication"]["archive_cleanup"] == {
        "attempted": True,
        "deleted": 2,
        "retained": 0,
    }
    assert len(spreadsheet.batch_calls) == 2
    first_requests = spreadsheet.batch_calls[0]["requests"]
    assert len(first_requests) == 4
    assert all("updateSheetProperties" in request for request in first_requests)
    assert [
        request["updateSheetProperties"]["properties"].get("index")
        for request in first_requests[2:]
    ] == [0, 1]
    second_requests = spreadsheet.batch_calls[1]["requests"]
    assert len(second_requests) == 2
    assert all("deleteSheet" in request for request in second_requests)
    live_titles = {worksheet.title for worksheet in spreadsheet.worksheets()}
    assert "Active Accounts" in live_titles
    assert "Actions" in live_titles
    assert not any("archived" in title for title in live_titles)


def test_post_swap_baseline_failure_compensates_titles_and_rolls_back_db(
    monkeypatch,
    tmp_path,
):
    from linkedin import conf
    from linkedin import crm_sheet_import
    from linkedin.notifications import crm_v2_layout, sheets

    lead = Lead.objects.create(
        company_name="Compensation Account",
        email="person@compensation-account.example",
    )
    SalesOwner.objects.get_or_create(handle="Arian")
    people = _people_worksheet(lead)
    pipeline = MemoryWorksheet("Pipeline", 10)
    spreadsheet = MemorySpreadsheet(
        [people, pipeline],
        spreadsheet_id="compensation-workbook",
    )
    monkeypatch.setattr(conf, "GOOGLE_SHEETS_ID", "compensation-workbook")
    monkeypatch.setattr(sheets, "_gspread_client", lambda: spreadsheet)
    monkeypatch.setattr(
        crm_sheets,
        "backup_spreadsheet",
        lambda *_args, **_kwargs: tmp_path / "backup.json",
    )
    monkeypatch.setattr(crm_v2_layout, "apply_layout", lambda *_args, **_kwargs: 0)
    preview = tmp_path / "compensation-preview.json"
    call_command(
        "preview_crm_v2",
        "--skip-sales-motion",
        "--manual-pin",
        "Compensation Account",
        "--owner-override",
        "Compensation Account=Arian",
        "--output",
        str(preview),
        stdout=io.StringIO(),
    )
    monkeypatch.setattr(
        crm_sheet_import,
        "commit_sheet_baselines",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("test baseline failure")
        ),
    )

    with pytest.raises(RuntimeError, match="test baseline failure"):
        call_command(
            "refresh_crm_v2",
            "--apply",
            "--reviewed-preview",
            str(preview),
            "--skip-sales-motion",
            "--manual-pin",
            "Compensation Account",
            "--owner-override",
            "Compensation Account=Arian",
            stdout=io.StringIO(),
        )

    assert not Account.objects.filter(name="Compensation Account").exists()
    assert len(spreadsheet.batch_calls) == 2
    assert all(
        "updateSheetProperties" in request
        for request in spreadsheet.batch_calls[1]["requests"]
    )
    live_titles = {worksheet.title for worksheet in spreadsheet.worksheets()}
    assert "Pipeline" in live_titles
    assert "Active Accounts" not in live_titles
    assert "Actions" not in live_titles
    assert any("failed" in title for title in live_titles)


def test_existing_tabs_three_way_import_and_replan_are_idempotent():
    view = _database_view()
    active = MemoryWorksheet("Active Accounts", 1)
    actions = MemoryWorksheet("Actions", 2)
    # Seed with the exact first-plan payload, including portable baselines.
    first_active, first_action = _build_plans(
        active,
        actions,
        view,
        crm_v2_sheets=v2,
    )
    v2.active_accounts_adapter(active).apply(first_active)
    v2.actions_adapter(actions).apply(first_action)
    baseline_active = {
        item.stable_id: item.values for item in first_active.baseline_updates
    }
    baseline_action = {
        item.stable_id: item.values for item in first_action.baseline_updates
    }
    same_view = SimpleNamespace(
        rows=view.rows,
        active_baselines=baseline_active,
        action_baselines=baseline_action,
    )

    second_active, second_action = _build_plans(
        active,
        actions,
        same_view,
        crm_v2_sheets=v2,
    )
    assert second_active.appends == []
    assert second_active.changes == []
    assert second_active.imports == []
    assert second_action.appends == []
    assert second_action.changes == []
    assert second_action.imports == []

    owner_column = list(v2.ACTIVE_ACCOUNT_HEADERS).index(v2.COL_OWNER)
    active.rows[1][owner_column] = "Athena"
    imported_active, _ = _build_plans(
        active,
        actions,
        same_view,
        crm_v2_sheets=v2,
    )
    assert [(item.field, item.value) for item in imported_active.imports] == [
        (v2.COL_OWNER, "Athena")
    ]


def test_first_cutover_imports_safe_legacy_human_edits_and_retains_unresolved():
    from linkedin.crm_publish import followup_db_human_values
    from linkedin.crm_sheet_import import (
        apply_followup_imports,
        apply_opportunity_imports,
    )

    arian, _ = SalesOwner.objects.get_or_create(handle="Arian")
    athena, _ = SalesOwner.objects.get_or_create(handle="Athena")
    lead = Lead.objects.create(
        company_name="Legacy Account",
        email="person@legacy-account.example",
    )
    account = Account.objects.create(name="Legacy Account")
    opportunity = Opportunity.objects.create(
        account=account,
        owner=arian,
        source=Opportunity.Source.MANUAL,
        manual_pin=True,
    )
    OpportunityContact.objects.create(opportunity=opportunity, lead=lead)
    action = OpportunityAction.objects.create(
        opportunity=opportunity,
        target_lead=lead,
        description="Legacy next step",
        due_on=timezone.localdate(),
        idempotency_key="human:legacy-account",
    )

    opportunity_ws = MemoryWorksheet("Opportunities", 31)
    opportunity_adapter = crm_sheets.OpportunitySheetAdapter(opportunity_ws)
    opportunity_seed = opportunity_adapter.plan([
        crm_sheets.opportunity_to_sheet_row(
            opportunity,
            action=action,
            synced_at=timezone.now(),
        )
    ])
    opportunity_adapter.apply(opportunity_seed)
    owner_column = list(crm_sheets.OPPORTUNITY_HEADERS).index(crm_sheets.COL_OWNER)
    opportunity_ws.rows[1][owner_column] = athena.handle
    unknown = ["" for _header in crm_sheets.OPPORTUNITY_HEADERS]
    unknown[list(crm_sheets.OPPORTUNITY_HEADERS).index(
        crm_sheets.COL_OPPORTUNITY_ID
    )] = "11111111-1111-1111-1111-111111111111"
    unknown[list(crm_sheets.OPPORTUNITY_HEADERS).index(
        crm_sheets.COL_ACCOUNT
    )] = "Unresolved legacy row"
    opportunity_ws.rows.append(unknown)

    followup_title = crm_sheets.sender_followups_tab("Arian")
    followup_ws = MemoryWorksheet(followup_title, 32)
    followup_adapter = crm_sheets.followups_adapter(followup_ws)
    followup_seed = followup_adapter.plan([
        {
            crm_sheets.COL_ACTION_ID: str(action.id),
            **followup_db_human_values(action),
        }
    ], baseline_by_id={})
    followup_adapter.apply(followup_seed)
    action.sheet_human_snapshot = dict(followup_seed.baseline_updates[0].values)
    action.save(update_fields={"sheet_human_snapshot", "updated_at"})
    draft_column = list(crm_sheets.FOLLOWUP_HEADERS).index(crm_sheets.COL_DRAFT)
    followup_ws.rows[1][draft_column] = "Human legacy draft"

    result = _import_legacy_human_state(
        {
            "Opportunities": opportunity_ws,
            followup_title: followup_ws,
        },
        crm_sheets=crm_sheets,
        apply_opportunity_imports=apply_opportunity_imports,
        apply_followup_imports=apply_followup_imports,
        evaluated_at=timezone.now(),
    )

    opportunity.refresh_from_db()
    action.refresh_from_db()
    assert opportunity.owner_id == athena.id
    assert action.draft == "Human legacy draft"
    assert result["opportunity_edits"] == 1
    assert result["followup_edits"] == 1
    assert result["unresolved_rows"] == 1
    assert result["retain_archive_titles"] == ("Opportunities",)


def test_layout_is_restrained_filterable_and_hides_only_technical_columns():
    worksheet = MemoryWorksheet("Active Accounts", 99)
    requests = build_layout_requests(
        worksheet,
        headers=v2.ACTIVE_ACCOUNT_HEADERS,
        technical_fields=v2.ACTIVE_ACCOUNT_TECHNICAL_FIELDS,
        owner_values=("Arian", "Athena"),
    )
    assert sum("setBasicFilter" in request for request in requests) == 1
    assert sum("updateSheetProperties" in request for request in requests) == 1
    hidden = [
        request for request in requests
        if request.get("updateDimensionProperties", {})
        .get("properties", {}).get("hiddenByUser") is True
    ]
    assert len(hidden) == len(v2.ACTIVE_ACCOUNT_TECHNICAL_FIELDS)
    validations = [request for request in requests if "setDataValidation" in request]
    assert len(validations) == 3


def test_aggregate_telemetry_contains_no_account_or_contact_identity():
    decision = SimpleNamespace(
        admitted=True,
        primary_reason_code=SimpleNamespace(value="manual_pin"),
        reminder=SimpleNamespace(should_create_reminder=True),
    )
    evidence = SimpleNamespace(
        account_name="Secret Account",
        account_key="domain:secret.example",
        owner="Arian",
        facts=SimpleNamespace(do_not_outreach=False),
        decision=decision,
    )
    telemetry = _evidence_counts([evidence])
    rendered = json.dumps(telemetry)
    assert "Secret Account" not in rendered
    assert "secret.example" not in rendered
    assert "Arian" not in rendered
