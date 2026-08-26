from __future__ import annotations

import io
import json
import logging
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from django.core.management import CommandError, call_command
from django.utils import timezone
from gspread.exceptions import APIError

from crm.models import Account, Lead, Opportunity, OpportunityAction, SalesOwner
from linkedin.crm_lock import crm_refresh_lock
from linkedin.crm_sheet_import import apply_followup_imports
from linkedin.exceptions import SheetsError
from linkedin.management.commands.refresh_crm import (
    Command,
    _action_counts_for_run,
    _assert_legacy_refresh_not_superseded,
    _assert_people_dnc_headers,
    _blocked_followup_owners,
    _build_granola_client,
    _capture_people_preservation_snapshot,
    _crm_stable_keys,
    _followup_plan_payload,
    _followup_block_summary,
    _followup_identity_blocker_count,
    _inventory_with_stable_keys,
    _legacy_followup_blocked_owners,
    _opportunity_identity_blocker_count,
    _people_gate_then_activate_managed_tabs,
    _people_explicit_stage_lead_ids,
    _publish_legacy_followup_tabs_atomically,
    _propagate_linked_owner_blockers,
    _retire_safe_followup_rows,
    _sanitized_granola_warnings,
    _sender_followup_plan,
    _stable_sender_publication_plan,
    _suppress_google_api_request_logging,
    _swap_legacy_followup_titles,
    _swap_worksheet_titles,
    _verify_workbook_identity,
    recover_failed_crm_sheet_titles,
)
from linkedin.notifications import crm_sheets, sheets


def _sheet_api_error(status: int, message: str = "provider detail") -> APIError:
    return APIError(SimpleNamespace(
        status_code=status,
        json=lambda: {
            "error": {
                "code": status,
                "message": message,
                "status": "RESOURCE_EXHAUSTED",
            },
        },
        text=message,
    ))


def test_google_api_request_logging_is_suppressed_and_restored():
    logger = logging.getLogger("googleapiclient.discovery")
    previous_level = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        with _suppress_google_api_request_logging():
            assert not logger.isEnabledFor(logging.DEBUG)
            assert logger.isEnabledFor(logging.WARNING)
        assert logger.level == logging.DEBUG

        with pytest.raises(RuntimeError):
            with _suppress_google_api_request_logging():
                raise RuntimeError("expected")
        assert logger.level == logging.DEBUG
    finally:
        logger.setLevel(previous_level)


def test_workbook_identity_rejects_mismatch_and_sales_motion_id(monkeypatch):
    monkeypatch.delenv("SALES_MOTION_VERSIONS_GOOGLE_SHEETS_ID", raising=False)
    monkeypatch.delenv("SALES_MOTION_SHEETS_ID", raising=False)

    with pytest.raises(SheetsError, match="does not match GOOGLE_SHEETS_ID"):
        _verify_workbook_identity(
            SimpleNamespace(id="opened-workbook"),
            configured_id="configured-crm-workbook",
        )

    monkeypatch.setenv(
        "SALES_MOTION_VERSIONS_GOOGLE_SHEETS_ID",
        "configured-crm-workbook",
    )
    with pytest.raises(SheetsError, match="Sales Motion workbook"):
        _verify_workbook_identity(
            SimpleNamespace(id="configured-crm-workbook"),
            configured_id="configured-crm-workbook",
        )


@pytest.mark.parametrize("title", ["Active Accounts", "Actions"])
def test_legacy_refresh_refuses_to_recreate_v1_after_v2_cutover(title):
    with pytest.raises(CommandError, match="CRM v2 is active"):
        _assert_legacy_refresh_not_superseded({"tabs": [{"title": title}]})

    _assert_legacy_refresh_not_superseded({"tabs": [{"title": "People"}]})


def test_live_apply_identity_check_requires_sales_motion_guard(monkeypatch):
    monkeypatch.delenv("SALES_MOTION_VERSIONS_GOOGLE_SHEETS_ID", raising=False)
    monkeypatch.delenv("SALES_MOTION_SHEETS_ID", raising=False)

    with pytest.raises(SheetsError, match="identity guard"):
        _verify_workbook_identity(
            SimpleNamespace(id="configured-crm-workbook"),
            configured_id="configured-crm-workbook",
            require_sales_motion_guard=True,
        )


def test_stable_key_inventory_covers_every_managed_sender_surface():
    stable_keys = _crm_stable_keys(crm_sheets=crm_sheets, sheets=sheets)

    assert stable_keys[sheets.GOOGLE_SHEETS_TAB_NAME] == sheets.COL_LINKEDIN_URL
    assert stable_keys[crm_sheets.OPPORTUNITIES_TAB] == crm_sheets.COL_OPPORTUNITY_ID
    assert stable_keys[crm_sheets.PIPELINE_TAB] == crm_sheets.COL_OPPORTUNITY_ID
    assert stable_keys[crm_sheets.RECOVERY_TAB] == crm_sheets.COL_OPPORTUNITY_ID
    assert {
        title for title, key in stable_keys.items()
        if key == crm_sheets.COL_ACTION_ID
    } == {
        crm_sheets.sender_followups_tab(owner)
        for owner in ("Arian", "Athena", "Chuka", "Leili")
    }


def test_inventory_passes_and_labels_stable_keys():
    calls = []

    class FakeCrmSheets:
        @staticmethod
        def inventory_spreadsheet(spreadsheet, *, stable_keys):
            calls.append((spreadsheet, stable_keys))
            return {
                "tabs": [
                    {"title": "People"},
                    {"title": "Arian - Followups"},
                    {"title": "Operator Notes"},
                ],
            }

    spreadsheet = object()
    stable_keys = {
        "People": "LinkedIn URL",
        "Arian - Followups": "Action ID",
    }
    inventory = _inventory_with_stable_keys(
        spreadsheet,
        crm_sheets=FakeCrmSheets,
        stable_keys=stable_keys,
    )

    assert calls == [(spreadsheet, stable_keys)]
    assert [tab["stable_key_header"] for tab in inventory["tabs"]] == [
        "LinkedIn URL",
        "Action ID",
        "",
    ]


def test_people_preservation_tab_resolution_retries_quota(monkeypatch):
    worksheet = object()
    calls = []
    sleeps = []

    class Spreadsheet:
        def worksheet(self, title):
            calls.append(title)
            if len(calls) <= 2:
                raise _sheet_api_error(429, "private People tab detail")
            return worksheet

    fake_sheets = SimpleNamespace(
        GOOGLE_SHEETS_TAB_NAME="People",
        capture_people_preservation_snapshot=lambda ws: ("captured", ws),
    )
    monkeypatch.setattr(crm_sheets.time, "sleep", sleeps.append)

    result = _capture_people_preservation_snapshot(
        Spreadsheet(),
        sheets=fake_sheets,
    )

    assert result == ("captured", worksheet)
    assert calls == ["People", "People", "People"]
    assert sleeps == [5, 10]


def test_granola_client_setup_error_is_returned_for_gemini_fallback():
    from linkedin.exceptions import GranolaError

    class BrokenGranolaClient:
        def __init__(self, **_kwargs):
            raise GranolaError("invalid Granola configuration")

    client, error = _build_granola_client(
        api_key="configured",
        base_url="https://api.granola.example",
        timeout=30,
        GranolaClient=BrokenGranolaClient,
        GranolaError=GranolaError,
    )

    assert client is None
    assert isinstance(error, GranolaError)
    assert str(error) == "invalid Granola configuration"


def test_granola_refresh_warnings_never_include_provider_text_or_note_ids():
    raw_note_id = "note_01JSECRETEXTERNALID"
    raw_exception = "HTTP 503 provider body contains private attendee data"
    result = SimpleNamespace(
        metadata_failures=2,
        transcript_failures=1,
        pending_details=3,
        unavailable=4,
        ambiguous=0,
        unmatched=5,
        warnings=[
            f"Granola note {raw_note_id} failed: {raw_exception}",
            "another provider-controlled warning",
        ],
    )

    warnings = _sanitized_granola_warnings(result)
    rendered = json.dumps(warnings)

    assert raw_note_id not in rendered
    assert raw_exception not in rendered
    assert "provider-controlled warning" not in rendered
    assert "metadata failures: 2" in rendered
    assert "transcript failures: 1" in rendered
    assert "provider warning messages suppressed: 2" in rendered


def test_people_gate_precedes_every_structural_tab_write(monkeypatch):
    from linkedin.management.commands import refresh_crm

    events = []
    people_before = SimpleNamespace(row_count=2)
    people_after = SimpleNamespace(row_count=3)

    def fake_people_sync(**kwargs):
        events.append(
            "people_preflight" if kwargs["dry_run"] else "people_publish"
        )
        return {
            "appended": 1,
            "updated": 2,
            "updated_cells": 3,
            "unchanged": 4,
            "skipped": 0,
            "errored": 0,
            "header_additions": 0,
            "duplicate_keys": 0,
        }

    def fake_snapshot(_spreadsheet, *, sheets):
        events.append("people_verify")
        return people_after

    class FakeVerification:
        @staticmethod
        def as_dict():
            return {
                "verified": True,
                "rows_before": 2,
                "rows_after": 3,
                "rows_preserved": 2,
                "headers_preserved": 17,
                "protected_cells_preserved": 4,
                "formulas_preserved": 1,
            }

    fake_sheets = SimpleNamespace(
        verify_people_preserved=lambda before, after: (
            FakeVerification()
            if (before, after) == (people_before, people_after)
            else (_ for _ in ()).throw(AssertionError("wrong snapshots"))
        ),
    )

    class FakeCrmSheets:
        OPPORTUNITIES_TAB = "Opportunities"
        OPPORTUNITY_HEADERS = ("Opportunity ID",)
        PIPELINE_TAB = "Pipeline"
        PIPELINE_HEADERS = ("Opportunity ID",)
        RECOVERY_TAB = "Recovery"
        RECOVERY_HEADERS = ("Opportunity ID",)

        @staticmethod
        def ensure_managed_tab(_spreadsheet, *, title, required_headers, dry_run):
            events.append(f"structure:{title}")
            return object(), SimpleNamespace(
                title=title,
                exists=True,
                would_create=False,
                header_additions=(),
            )

    monkeypatch.setattr(
        refresh_crm,
        "_capture_people_preservation_snapshot",
        fake_snapshot,
    )

    report, managed, blocked = _people_gate_then_activate_managed_tabs(
        spreadsheet=object(),
        people_before=people_before,
        skip_people=False,
        dry_run=False,
        run_people_sync=fake_people_sync,
        crm_sheets=FakeCrmSheets,
        sheets=fake_sheets,
    )

    assert events == [
        "people_preflight",
        "people_publish",
        "people_verify",
        "structure:Opportunities",
        "structure:Pipeline",
        "structure:Recovery",
    ]
    assert len(managed) == 3
    assert blocked is False
    assert report == {
        "appended": 1,
        "updated": 2,
        "updated_cells": 3,
        "unchanged": 4,
        "skipped": 0,
        "errored": 0,
        "header_additions": 0,
        "duplicate_keys": 0,
        "gate_blocked": False,
        "rows_before": 2,
        "preflight": {
            "appended": 1,
            "updated": 2,
            "updated_cells": 3,
            "unchanged": 4,
            "skipped": 0,
            "errored": 0,
            "header_additions": 0,
            "duplicate_keys": 0,
        },
        "status": "published_and_verified",
        "rows_after": 3,
        "verified": True,
        "rows_preserved": 2,
        "headers_preserved": 17,
        "protected_cells_preserved": 4,
        "formulas_preserved": 1,
    }


def test_people_dont_send_headers_fail_closed():
    with pytest.raises(SheetsError, match="Don't-send safety header"):
        _assert_people_dnc_headers(
            SimpleNamespace(headers=(sheets.COL_LINKEDIN_URL,)),
            sheets=sheets,
        )


@pytest.mark.django_db
def test_people_explicit_stage_bootstrap_uses_stable_id_and_url_not_name():
    eligible = Lead.objects.create(
        first_name="Same",
        last_name="Name",
        linkedin_url="https://www.linkedin.com/in/eligible-stage/",
    )
    prospecting = Lead.objects.create(
        first_name="Same",
        last_name="Name",
        linkedin_url="https://www.linkedin.com/in/ordinary-prospecting/",
    )
    lost = Lead.objects.create(
        first_name="Same",
        last_name="Name",
        linkedin_url="https://www.linkedin.com/in/lost-stage/",
    )
    conflicting_url = Lead.objects.create(
        first_name="Same",
        last_name="Name",
        linkedin_url="https://www.linkedin.com/in/database-identity/",
    )
    headers = [sheets.COL_LEAD_ID, sheets.COL_LINKEDIN_URL, sheets.COL_STAGE]
    values = [
        headers,
        [
            str(eligible.id),
            "https://linkedin.com/in/ELIGIBLE-STAGE?trk=legacy",
            sheets.STAGE_QUALIFICATION,
        ],
        [
            str(prospecting.id),
            prospecting.linkedin_url,
            sheets.STAGE_PROSPECTING,
        ],
        [str(lost.id), lost.linkedin_url, sheets.STAGE_LOST],
        [
            str(conflicting_url.id),
            "https://www.linkedin.com/in/someone-else/",
            sheets.STAGE_CLOSING,
        ],
        [str(eligible.id), eligible.linkedin_url, '=IF(TRUE,"Won","")'],
    ]
    worksheet = SimpleNamespace(get_all_values=lambda **_kwargs: values)
    spreadsheet = SimpleNamespace(
        worksheet=lambda title: worksheet
        if title == sheets.GOOGLE_SHEETS_TAB_NAME
        else (_ for _ in ()).throw(AssertionError("wrong tab")),
    )

    lead_ids, report = _people_explicit_stage_lead_ids(
        spreadsheet,
        sheets=sheets,
    )

    assert lead_ids == {eligible.id}
    assert report["advanced_stage_rows"] == 2
    assert report["eligible_lead_ids"] == 1
    assert report["linkedin_url_conflicts"] == 1
    assert report["identity_ambiguities"] == 1


@pytest.mark.django_db
def test_people_explicit_stage_bootstrap_reports_ambiguous_stable_rows():
    duplicate = Lead.objects.create(
        linkedin_url="https://www.linkedin.com/in/duplicate-stage/",
    )
    disqualified = Lead.objects.create(
        linkedin_url="https://www.linkedin.com/in/disqualified-stage/",
        disqualified=True,
    )
    missing_url = Lead.objects.create(linkedin_url="")
    headers = [sheets.COL_LEAD_ID, sheets.COL_LINKEDIN_URL, sheets.COL_STAGE]
    values = [
        headers,
        [str(duplicate.id), duplicate.linkedin_url, sheets.STAGE_MEETING],
        [str(duplicate.id), duplicate.linkedin_url, sheets.STAGE_MEETING],
        ["not-numeric", "https://linkedin.com/in/invalid/", sheets.STAGE_WON],
        ["999999999", "https://linkedin.com/in/missing/", sheets.STAGE_CLOSING],
        [str(disqualified.id), disqualified.linkedin_url, sheets.STAGE_WON],
        [str(missing_url.id), "", sheets.STAGE_QUALIFICATION],
    ]
    worksheet = SimpleNamespace(get_all_values=lambda **_kwargs: values)
    spreadsheet = SimpleNamespace(worksheet=lambda _title: worksheet)

    lead_ids, report = _people_explicit_stage_lead_ids(
        spreadsheet,
        sheets=sheets,
    )

    assert lead_ids == set()
    assert report == {
        "status": "ready",
        "missing_required_headers": 0,
        "advanced_stage_rows": 6,
        "eligible_lead_ids": 0,
        "invalid_lead_id_rows": 1,
        "duplicate_lead_id_groups": 1,
        "duplicate_lead_id_rows": 2,
        "missing_leads": 1,
        "disqualified_leads": 1,
        "missing_linkedin_urls": 1,
        "linkedin_url_conflicts": 0,
        "identity_ambiguities": 5,
    }


def test_people_gate_failure_prevents_structural_tab_writes(monkeypatch):
    from linkedin.management.commands import refresh_crm

    events = []
    people_before = SimpleNamespace(row_count=2)

    monkeypatch.setattr(
        refresh_crm,
        "_capture_people_preservation_snapshot",
        lambda *_args, **_kwargs: SimpleNamespace(row_count=1),
    )
    fake_crm_sheets = SimpleNamespace(
        OPPORTUNITIES_TAB="Opportunities",
        OPPORTUNITY_HEADERS=(),
        PIPELINE_TAB="Pipeline",
        PIPELINE_HEADERS=(),
        RECOVERY_TAB="Recovery",
        RECOVERY_HEADERS=(),
        ensure_managed_tab=lambda *_args, **_kwargs: events.append("structure"),
    )

    with pytest.raises(SheetsError, match="row count decreased"):
        _people_gate_then_activate_managed_tabs(
            spreadsheet=object(),
            people_before=people_before,
            skip_people=False,
            dry_run=False,
            run_people_sync=lambda **_kwargs: {"appended": 0},
            crm_sheets=fake_crm_sheets,
            sheets=SimpleNamespace(
                verify_people_preserved=lambda *_args: (_ for _ in ()).throw(
                    SheetsError("People row count decreased")
                ),
            ),
        )

    assert events == []


def test_people_publisher_errors_block_structure_even_when_preservation_passes(
    monkeypatch,
):
    from linkedin.management.commands import refresh_crm

    events = []
    before = SimpleNamespace(row_count=2)
    after = SimpleNamespace(row_count=2)
    monkeypatch.setattr(
        refresh_crm,
        "_capture_people_preservation_snapshot",
        lambda *_args, **_kwargs: after,
    )
    fake_sheets = SimpleNamespace(
        verify_people_preserved=lambda *_args: SimpleNamespace(
            as_dict=lambda: {
                "verified": True,
                "rows_before": 2,
                "rows_after": 2,
            },
        ),
    )
    fake_crm_sheets = SimpleNamespace(
        OPPORTUNITIES_TAB="Opportunities",
        OPPORTUNITY_HEADERS=(),
        PIPELINE_TAB="Pipeline",
        PIPELINE_HEADERS=(),
        RECOVERY_TAB="Recovery",
        RECOVERY_HEADERS=(),
        ensure_managed_tab=lambda *_args, **_kwargs: events.append("structure"),
    )

    calls = []

    def unsafe_preflight(**kwargs):
        calls.append(kwargs["dry_run"])
        return {
            "errored": 1,
            "duplicate_lead_ids": 0,
        }

    with pytest.raises(SheetsError, match="no worksheet writes were attempted"):
        _people_gate_then_activate_managed_tabs(
            spreadsheet=object(),
            people_before=before,
            skip_people=False,
            dry_run=False,
            run_people_sync=unsafe_preflight,
            crm_sheets=fake_crm_sheets,
            sheets=fake_sheets,
        )

    assert events == []
    assert calls == [True]


def test_people_duplicate_lead_ids_fail_preflight_before_any_sheet_write(
    monkeypatch,
):
    from linkedin.management.commands import refresh_crm

    events = []
    monkeypatch.setattr(
        refresh_crm,
        "_capture_people_preservation_snapshot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("People was read after unsafe preflight")
        ),
    )
    fake_crm_sheets = SimpleNamespace(
        OPPORTUNITIES_TAB="Opportunities",
        OPPORTUNITY_HEADERS=(),
        PIPELINE_TAB="Pipeline",
        PIPELINE_HEADERS=(),
        RECOVERY_TAB="Recovery",
        RECOVERY_HEADERS=(),
        ensure_managed_tab=lambda *_args, **_kwargs: events.append("structure"),
    )
    calls = []

    def unsafe_preflight(**kwargs):
        calls.append(kwargs["dry_run"])
        return {
            "errored": 0,
            "duplicate_lead_ids": 2,
            "duplicate_linkedin_urls": 3,
        }

    with pytest.raises(SheetsError, match="duplicate stable Lead IDs"):
        _people_gate_then_activate_managed_tabs(
            spreadsheet=object(),
            people_before=SimpleNamespace(row_count=2),
            skip_people=False,
            dry_run=False,
            run_people_sync=unsafe_preflight,
            crm_sheets=fake_crm_sheets,
            sheets=SimpleNamespace(),
        )

    assert calls == [True]
    assert events == []


def test_people_legacy_duplicate_urls_do_not_block_safe_preflight():
    calls = []

    def duplicate_url_only(**kwargs):
        calls.append(kwargs["dry_run"])
        return {
            "errored": 0,
            "duplicate_lead_ids": 0,
            "duplicate_linkedin_urls": 4,
            "ambiguous_existing": 4,
        }

    fake_crm_sheets = SimpleNamespace(
        OPPORTUNITIES_TAB="Opportunities",
        OPPORTUNITY_HEADERS=(),
        PIPELINE_TAB="Pipeline",
        PIPELINE_HEADERS=(),
        RECOVERY_TAB="Recovery",
        RECOVERY_HEADERS=(),
        ensure_managed_tab=lambda *_args, title, **_kwargs: (
            None,
            SimpleNamespace(title=title),
        ),
    )
    report, _managed, blocked = _people_gate_then_activate_managed_tabs(
        spreadsheet=object(),
        people_before=SimpleNamespace(row_count=2),
        skip_people=False,
        dry_run=True,
        run_people_sync=duplicate_url_only,
        crm_sheets=fake_crm_sheets,
        sheets=SimpleNamespace(),
    )

    assert calls == [True]
    assert blocked is False
    assert report["duplicate_linkedin_urls"] == 4
    assert report["ambiguous_existing"] == 4
    assert report["gate_blocked"] is False


def test_legacy_followup_title_swap_uses_one_atomic_batch():
    calls = []
    spreadsheet = SimpleNamespace(
        batch_update=lambda body: calls.append(body),
    )

    _swap_worksheet_titles(
        spreadsheet,
        existing_ws=SimpleNamespace(id=101),
        replacement_ws=SimpleNamespace(id=202),
        legacy_title="Arian - Followups Legacy 20260826",
        canonical_title="Arian - Followups",
    )

    assert calls == [
        {
            "requests": [
                {
                    "updateSheetProperties": {
                        "properties": {
                            "sheetId": 101,
                            "title": "Arian - Followups Legacy 20260826",
                        },
                        "fields": "title",
                    },
                },
                {
                    "updateSheetProperties": {
                        "properties": {
                            "sheetId": 202,
                            "title": "Arian - Followups",
                        },
                        "fields": "title",
                    },
                },
            ],
        },
    ]


def test_all_legacy_followup_title_pairs_use_one_atomic_batch():
    calls = []
    owners = ("Arian", "Athena", "Chuka", "Leili")
    swaps = tuple(
        {
            "existing_ws": SimpleNamespace(id=index),
            "replacement_ws": SimpleNamespace(id=index + 100),
            "legacy_title": f"{owner} - Followups Legacy 20260826",
            "canonical_title": f"{owner} - Followups",
        }
        for index, owner in enumerate(owners, start=1)
    )

    _swap_legacy_followup_titles(
        SimpleNamespace(batch_update=lambda body: calls.append(body)),
        swaps=swaps,
    )

    assert len(calls) == 1
    requests = calls[0]["requests"]
    assert len(requests) == 8
    assert [
        request["updateSheetProperties"]["properties"]
        for request in requests[:4]
    ] == [
        {
            "sheetId": index,
            "title": f"{owner} - Followups Legacy 20260826",
        }
        for index, owner in enumerate(owners, start=1)
    ]
    assert [
        request["updateSheetProperties"]["properties"]
        for request in requests[4:]
    ] == [
        {
            "sheetId": index + 100,
            "title": f"{owner} - Followups",
        }
        for index, owner in enumerate(owners, start=1)
    ]


def _failed_crm_title_recovery_fixture(*, extra_worksheets=()):
    owners = ("Arian", "Athena", "Chuka", "Leili")
    legacy_titles = {
        owner: f"{owner} - Followups Legacy 20260826" for owner in owners
    }
    required_titles = [
        *(crm_sheets.sender_followups_tab(owner) for owner in owners),
        crm_sheets.OPPORTUNITIES_TAB,
        crm_sheets.PIPELINE_TAB,
        crm_sheets.RECOVERY_TAB,
        *(legacy_titles[owner] for owner in owners),
    ]
    worksheets = [
        SimpleNamespace(id=index, title=title)
        for index, title in enumerate(required_titles, start=100)
    ]
    worksheets.extend(extra_worksheets)
    expected_ids = {
        worksheet.title: worksheet.id
        for worksheet in worksheets
        if worksheet.title in required_titles
    }
    return legacy_titles, expected_ids, worksheets


def test_failed_crm_title_recovery_is_one_atomic_title_only_batch():
    failed_at = datetime(2026, 8, 26, 16, 30, 0)
    colliding_failed_title = (
        "Failed CRM 20260826T163000 Arian - Followups"
    )
    legacy_titles, expected_ids, worksheets = (
        _failed_crm_title_recovery_fixture(
            extra_worksheets=(
                SimpleNamespace(id=999, title=colliding_failed_title),
            ),
        )
    )
    calls = []

    class Spreadsheet:
        def worksheets(self):
            return list(worksheets)

        def batch_update(self, body):
            calls.append(body)

    result = recover_failed_crm_sheet_titles(
        Spreadsheet(),
        legacy_titles_by_owner=legacy_titles,
        expected_sheet_ids_by_title=expected_ids,
        failed_at=failed_at,
        crm_sheets=crm_sheets,
    )

    assert len(calls) == 1
    requests = calls[0]["requests"]
    assert len(requests) == 11
    assert all(set(request) == {"updateSheetProperties"} for request in requests)
    assert all(
        request["updateSheetProperties"]["fields"] == "title"
        for request in requests
    )
    generated_titles = [
        *(crm_sheets.sender_followups_tab(owner)
          for owner in ("Arian", "Athena", "Chuka", "Leili")),
        crm_sheets.OPPORTUNITIES_TAB,
        crm_sheets.PIPELINE_TAB,
        crm_sheets.RECOVERY_TAB,
    ]
    generated_properties = [
        request["updateSheetProperties"]["properties"]
        for request in requests[:7]
    ]
    assert [item["sheetId"] for item in generated_properties] == [
        expected_ids[title] for title in generated_titles
    ]
    assert generated_properties[0]["title"] == f"{colliding_failed_title} 2"
    assert len({item["title"] for item in generated_properties}) == 7
    assert [
        request["updateSheetProperties"]["properties"]
        for request in requests[7:]
    ] == [
        {
            "sheetId": expected_ids[legacy_titles[owner]],
            "title": crm_sheets.sender_followups_tab(owner),
        }
        for owner in ("Arian", "Athena", "Chuka", "Leili")
    ]
    assert result["renamed_tabs"] == 11
    assert result["failed_outputs"]["Arian - Followups"].endswith(" 2")


def test_failed_crm_title_recovery_retries_worksheet_inventory(monkeypatch):
    legacy_titles, expected_ids, worksheets = (
        _failed_crm_title_recovery_fixture()
    )
    list_calls = []
    batches = []
    sleeps = []

    class Spreadsheet:
        def worksheets(self):
            list_calls.append(True)
            if len(list_calls) == 1:
                raise _sheet_api_error(503, "private recovery detail")
            return list(worksheets)

        def batch_update(self, body):
            batches.append(body)

    monkeypatch.setattr(crm_sheets.time, "sleep", sleeps.append)

    result = recover_failed_crm_sheet_titles(
        Spreadsheet(),
        legacy_titles_by_owner=legacy_titles,
        expected_sheet_ids_by_title=expected_ids,
        failed_at=datetime(2026, 8, 26, 16, 30, 0),
        crm_sheets=crm_sheets,
    )

    assert result["renamed_tabs"] == 11
    assert len(batches) == 1
    assert sleeps == [5]


@pytest.mark.parametrize("drift", ("missing_title", "changed_id", "duplicate_id"))
def test_failed_crm_title_recovery_rejects_inventory_drift_without_writing(drift):
    legacy_titles, expected_ids, worksheets = (
        _failed_crm_title_recovery_fixture()
    )
    if drift == "missing_title":
        worksheets = [
            worksheet
            for worksheet in worksheets
            if worksheet.title != crm_sheets.OPPORTUNITIES_TAB
        ]
    elif drift == "changed_id":
        for worksheet in worksheets:
            if worksheet.title == crm_sheets.PIPELINE_TAB:
                worksheet.id += 1000
                break
    else:
        worksheets[-1].id = worksheets[0].id
    calls = []

    spreadsheet = SimpleNamespace(
        worksheets=lambda: list(worksheets),
        batch_update=lambda body: calls.append(body),
    )
    with pytest.raises(SheetsError):
        recover_failed_crm_sheet_titles(
            spreadsheet,
            legacy_titles_by_owner=legacy_titles,
            expected_sheet_ids_by_title=expected_ids,
            failed_at=datetime(2026, 8, 26, 16, 30, 0),
            crm_sheets=crm_sheets,
        )

    assert calls == []


def test_failed_crm_title_recovery_requires_exact_legacy_archive_names():
    legacy_titles, expected_ids, _worksheets = (
        _failed_crm_title_recovery_fixture()
    )
    legacy_titles["Arian"] = "Arian - Followups Legacy maybe"
    spreadsheet = SimpleNamespace(
        worksheets=lambda: (_ for _ in ()).throw(
            AssertionError("invalid request must fail before Sheets inventory")
        ),
        batch_update=lambda _body: (_ for _ in ()).throw(
            AssertionError("invalid request must not write")
        ),
    )

    with pytest.raises(SheetsError, match="invalid legacy title"):
        recover_failed_crm_sheet_titles(
            spreadsheet,
            legacy_titles_by_owner=legacy_titles,
            expected_sheet_ids_by_title=expected_ids,
            failed_at=datetime(2026, 8, 26, 16, 30, 0),
            crm_sheets=crm_sheets,
        )


def test_unresolved_legacy_material_is_preserved_for_review_without_blocking():
    owners = ("Arian", "Athena", "Chuka", "Leili")
    worksheets = {
        owner: SimpleNamespace(
            id=index,
            title=crm_sheets.sender_followups_tab(owner),
        )
        for index, owner in enumerate(owners, start=1)
    }
    commits = []

    reports, blocked = _publish_legacy_followup_tabs_atomically(
        SimpleNamespace(worksheets=lambda: list(worksheets.values())),
        legacy_worksheets=worksheets,
        desired_rows_by_owner={owner: () for owner in owners},
        legacy_reports={
            owner: {
                "material_rows_skipped": 2,
                "material_skip_reasons": {"no_stable_identity": 2},
            }
            for owner in owners
        },
        dry_run=True,
        crm_sheets=crm_sheets,
        OpportunityAction=OpportunityAction,
        commit_followup_baselines=lambda updates: commits.append(updates),
        now=timezone.now(),
    )

    assert blocked == set()
    assert commits == []
    assert set(reports) == set(owners)
    for owner in owners:
        assert reports[owner]["blocked"] is False
        assert reports[owner]["status"] == "planned_atomic_archive"
        assert reports[owner]["legacy_preserved"] is True
        assert reports[owner]["review_required"] is True
        assert reports[owner]["material_rows_skipped"] == 2
        assert "Legacy" in reports[owner]["archive_title"]


def test_atomic_legacy_title_inventory_retries_quota(monkeypatch):
    source = SimpleNamespace(
        id=1,
        title=crm_sheets.sender_followups_tab("Arian"),
    )
    calls = []
    sleeps = []

    class Spreadsheet:
        def worksheets(self):
            calls.append(True)
            if len(calls) <= 2:
                raise _sheet_api_error(429, "private legacy title detail")
            return [source]

    monkeypatch.setattr(crm_sheets.time, "sleep", sleeps.append)

    reports, blocked = _publish_legacy_followup_tabs_atomically(
        Spreadsheet(),
        legacy_worksheets={"Arian": source},
        desired_rows_by_owner={"Arian": ()},
        legacy_reports={"Arian": {"material_rows_skipped": 0}},
        dry_run=True,
        crm_sheets=crm_sheets,
        OpportunityAction=OpportunityAction,
        commit_followup_baselines=lambda _updates: None,
        now=timezone.now(),
    )

    assert blocked == set()
    assert reports["Arian"]["status"] == "planned_atomic_archive"
    assert sleeps == [5, 10]


def test_atomic_legacy_swap_failure_leaves_sources_and_baselines_untouched():
    class MemoryWorksheet:
        def __init__(self, title, sheet_id):
            self.title = title
            self.id = sheet_id
            self.col_count = 0
            self.values = []

        def get_all_values(self, **_kwargs):
            return [list(row) for row in self.values]

        def add_cols(self, count):
            self.col_count += count

        def update(self, *, values, range_name):
            assert range_name.endswith("1")
            if self.values:
                self.values[0].extend(values[0])
            else:
                self.values = [list(values[0])]

        def batch_update(self, updates, **_kwargs):
            assert updates == []

        def append_rows(self, rows, **_kwargs):
            self.values.extend([list(row) for row in rows])

    owners = ("Arian", "Athena", "Chuka", "Leili")
    sources = {
        owner: MemoryWorksheet(
            crm_sheets.sender_followups_tab(owner),
            index,
        )
        for index, owner in enumerate(owners, start=1)
    }
    all_worksheets = list(sources.values())
    title_batches = []

    class FailingSpreadsheet:
        def worksheets(self):
            return list(all_worksheets)

        def add_worksheet(self, *, title, rows, cols):
            worksheet = MemoryWorksheet(title, len(all_worksheets) + 100)
            worksheet.col_count = cols
            all_worksheets.append(worksheet)
            return worksheet

        def batch_update(self, body):
            title_batches.append(body)
            raise SheetsError("simulated atomic title failure")

    baseline_commits = []
    reports, blocked = _publish_legacy_followup_tabs_atomically(
        FailingSpreadsheet(),
        legacy_worksheets=sources,
        desired_rows_by_owner={owner: () for owner in owners},
        legacy_reports={
            owner: {
                "material_rows_skipped": 1,
                "material_skip_reasons": {"ambiguous_identity": 1},
            }
            for owner in owners
        },
        dry_run=False,
        crm_sheets=crm_sheets,
        OpportunityAction=OpportunityAction,
        commit_followup_baselines=lambda updates: baseline_commits.append(updates),
        now=timezone.now(),
    )

    assert blocked == set(owners)
    assert len(title_batches) == 1
    assert baseline_commits == []
    assert [sources[owner].title for owner in owners] == [
        crm_sheets.sender_followups_tab(owner) for owner in owners
    ]
    for owner in owners:
        assert reports[owner]["blocked"] is True
        assert reports[owner]["failure_phase"] == "atomic_title_swap"
        assert reports[owner]["legacy_source_untouched"] is True
        assert reports[owner]["baseline_committed"] is False
        assert reports[owner]["orphan_temp_tab"]


@pytest.mark.django_db
def test_dry_run_handle_rolls_back_refresh_model_writes(monkeypatch):
    calls = []

    def fake_refresh(self, options, *, dry_run):
        calls.append(dry_run)
        Account.objects.create(name="refresh-crm dry-run rollback sentinel")
        assert Account.objects.filter(
            name="refresh-crm dry-run rollback sentinel",
        ).exists()
        return {"mode": "dry-run"}

    monkeypatch.setattr(Command, "_refresh", fake_refresh)

    call_command("refresh_crm", stdout=io.StringIO())

    assert calls == [True]
    assert not Account.objects.filter(
        name="refresh-crm dry-run rollback sentinel",
    ).exists()


@pytest.mark.django_db
def test_apply_flag_passes_dry_run_false(monkeypatch):
    calls = []

    def fake_refresh(self, options, *, dry_run):
        calls.append(dry_run)
        return {"mode": "apply"}

    monkeypatch.setattr(Command, "_refresh", fake_refresh)

    call_command("refresh_crm", apply=True, stdout=io.StringIO())

    assert calls == [False]


@pytest.mark.django_db
def test_apply_rolls_back_all_db_writes_when_downstream_sheet_api_fails(monkeypatch):
    def fake_refresh(self, options, *, dry_run):
        assert dry_run is False
        Account.objects.create(name="refresh-crm apply rollback sentinel")
        raise SheetsError("simulated late Sheets API failure")

    monkeypatch.setattr(Command, "_refresh", fake_refresh)

    with pytest.raises(SheetsError, match="simulated late Sheets API failure"):
        call_command("refresh_crm", apply=True, stdout=io.StringIO())

    assert not Account.objects.filter(
        name="refresh-crm apply rollback sentinel",
    ).exists()


@pytest.mark.django_db
def test_apply_rolls_back_all_db_writes_when_refresh_report_is_blocked(monkeypatch):
    def fake_refresh(self, options, *, dry_run):
        assert dry_run is False
        Account.objects.create(name="refresh-crm blocked rollback sentinel")
        return {"mode": "apply", "blocked": True}

    monkeypatch.setattr(Command, "_refresh", fake_refresh)
    stdout = io.StringIO()

    with pytest.raises(CommandError, match="human merge conflicts"):
        call_command("refresh_crm", apply=True, stdout=stdout)

    assert '"blocked": true' in stdout.getvalue()
    assert not Account.objects.filter(
        name="refresh-crm blocked rollback sentinel",
    ).exists()


def test_unknown_or_mutated_opportunity_ids_are_identity_blockers():
    plan = SimpleNamespace(
        unkeyed_nonempty_rows=(9,),
        retained_missing_keys=("not-a-canonical-opportunity-id",),
    )

    assert _opportunity_identity_blocker_count(plan) == 2


@pytest.mark.django_db
def test_lock_contention_surfaces_command_error(monkeypatch):
    def refresh_must_not_run(self, options, *, dry_run):
        raise AssertionError("refresh ran despite lock contention")

    monkeypatch.setattr(Command, "_refresh", refresh_must_not_run)

    with crm_refresh_lock():
        with pytest.raises(CommandError, match="already running"):
            call_command("refresh_crm", stdout=io.StringIO())


@pytest.mark.django_db
def test_retained_waiting_action_imports_by_stable_id_then_fresh_replan_is_clean():
    owner, _ = SalesOwner.objects.get_or_create(handle="Arian")
    opportunity = Opportunity.objects.create(
        account=Account.objects.create(name="Retained action account"),
        owner=owner,
        stage=Opportunity.Stage.DISCOVERY,
        sales_motion_step=2,
    )
    waiting_until = timezone.localdate() + timedelta(days=10)
    baseline = {
        crm_sheets.COL_WAITING_UNTIL: waiting_until.isoformat(),
        crm_sheets.COL_CHANNEL: "email",
        crm_sheets.COL_DRAFT: "Old retained draft",
        crm_sheets.COL_HANDLED: "FALSE",
        crm_sheets.COL_DISPOSITION: "",
        crm_sheets.COL_MANUAL_PIN: "FALSE",
    }
    action = OpportunityAction.objects.create(
        opportunity=opportunity,
        status=OpportunityAction.Status.WAITING,
        kind=OpportunityAction.Kind.NEXT_STEP,
        description="Wait for procurement",
        waiting_until=waiting_until,
        channel="email",
        draft="Old retained draft",
        sheet_human_snapshot=baseline,
    )
    sheet_values = {
        crm_sheets.COL_ACTION_ID: str(action.id),
        **baseline,
        crm_sheets.COL_DRAFT: "Human-edited retained draft",
    }

    class FakeWorksheet:
        title = "Arian - Followups"
        id = 100

        def get_all_values(self, **_kwargs):
            return [
                list(crm_sheets.FOLLOWUP_HEADERS),
                [sheet_values.get(header, "") for header in crm_sheets.FOLLOWUP_HEADERS],
            ]

    ws = FakeWorksheet()
    payload, baselines, telemetry = _followup_plan_payload(
        owner="Arian",
        ws=ws,
        due_rows=(),
        crm_sheets=crm_sheets,
        OpportunityAction=OpportunityAction,
    )
    assert telemetry["due_now_rows"] == 0
    assert telemetry["retained_canonical_rows_considered"] == 1
    assert telemetry["foreign_owner_action_rows"] == 0
    assert payload[0][crm_sheets.COL_ACTION_ID] == str(action.id)
    assert payload[0][crm_sheets.COL_WAITING_UNTIL] == waiting_until.isoformat()

    initial_plan = crm_sheets.followups_adapter(ws).plan(
        payload,
        remove_missing=False,
        baseline_by_id=baselines,
    )
    assert [item.field for item in initial_plan.imports] == [crm_sheets.COL_DRAFT]
    report = apply_followup_imports(initial_plan.imports, dry_run=False)
    assert report.counts() == {
        "actions_updated": 1,
        "fields_imported": 1,
        "completed": 0,
        "reopened": 0,
        "opportunities_pinned": 0,
        "invalid": 0,
    }
    action.refresh_from_db()
    assert action.draft == "Human-edited retained draft"

    fresh_payload, fresh_baselines, _telemetry = _followup_plan_payload(
        owner="Arian",
        ws=ws,
        due_rows=(),
        crm_sheets=crm_sheets,
        OpportunityAction=OpportunityAction,
    )
    fresh_plan = crm_sheets.followups_adapter(ws).plan(
        fresh_payload,
        remove_missing=False,
        baseline_by_id=fresh_baselines,
    )
    assert fresh_plan.imports == []
    assert fresh_plan.conflicts == []


@pytest.mark.django_db
def test_retained_row_for_different_owner_is_reported_not_imported():
    arian, _ = SalesOwner.objects.get_or_create(handle="Arian")
    athena, _ = SalesOwner.objects.get_or_create(handle="Athena")
    opportunity = Opportunity.objects.create(
        account=Account.objects.create(name="Owner mismatch account"),
        owner=athena,
    )
    action = OpportunityAction.objects.create(
        opportunity=opportunity,
        description="Owned by Athena",
        draft="Do not import across owners",
    )
    headers = list(crm_sheets.FOLLOWUP_HEADERS)
    values = {
        crm_sheets.COL_ACTION_ID: str(action.id),
        crm_sheets.COL_DRAFT: "Edited in the wrong sender tab",
    }
    ws = SimpleNamespace(
        title="Arian - Followups",
        id=101,
        get_all_values=lambda **_kwargs: [
            headers,
            [values.get(header, "") for header in headers],
        ],
    )

    payload, _baselines, telemetry = _followup_plan_payload(
        owner=arian.handle,
        ws=ws,
        due_rows=(),
        crm_sheets=crm_sheets,
        OpportunityAction=OpportunityAction,
    )

    assert payload == ()
    assert telemetry["foreign_owner_action_rows"] == 1
    assert telemetry["foreign_owner_material_action_rows"] == 1
    assert telemetry["safe_retired_action_rows"] == 0
    assert telemetry["retained_canonical_rows_considered"] == 0
    assert telemetry["_linked_blocked_owners"] == {"Athena": 1}


@pytest.mark.django_db
def test_material_old_owner_row_blocks_source_and_destination_sender_tabs():
    arian, _ = SalesOwner.objects.get_or_create(handle="Arian")
    athena, _ = SalesOwner.objects.get_or_create(handle="Athena")
    opportunity = Opportunity.objects.create(
        account=Account.objects.create(name="Material reassignment account"),
        owner=athena,
    )
    baseline = {
        crm_sheets.COL_WAITING_UNTIL: "",
        crm_sheets.COL_CHANNEL: "email",
        crm_sheets.COL_DRAFT: "Published draft",
        crm_sheets.COL_HANDLED: "FALSE",
        crm_sheets.COL_DISPOSITION: "",
        crm_sheets.COL_MANUAL_PIN: "FALSE",
    }
    action = OpportunityAction.objects.create(
        opportunity=opportunity,
        description="Reassigned material action",
        due_on=timezone.localdate(),
        channel="email",
        draft="Published draft",
        sheet_human_snapshot=baseline,
    )
    headers = list(crm_sheets.FOLLOWUP_HEADERS)
    old_values = {
        crm_sheets.COL_ACTION_ID: str(action.id),
        crm_sheets.COL_OWNER: arian.handle,
        **baseline,
        crm_sheets.COL_HUMAN_BASELINE: json.dumps(
            baseline,
            sort_keys=True,
            separators=(",", ":"),
        ),
        crm_sheets.COL_DRAFT: "Human edit in old owner tab",
    }
    old_ws = SimpleNamespace(
        title="Arian - Followups",
        id=210,
        get_all_values=lambda **_kwargs: [
            headers,
            [old_values.get(header, "") for header in headers],
        ],
    )
    new_ws = SimpleNamespace(
        title="Athena - Followups",
        id=211,
        get_all_values=lambda **_kwargs: [headers],
    )
    due_row = {
        crm_sheets.COL_ACTION_ID: str(action.id),
        crm_sheets.COL_OPPORTUNITY_ID: str(opportunity.id),
        crm_sheets.COL_OWNER: athena.handle,
        **baseline,
    }

    _old_desired, _old_baselines, old_telemetry, old_plan = (
        _sender_followup_plan(
            owner=arian.handle,
            ws=old_ws,
            due_rows=(),
            crm_sheets=crm_sheets,
            OpportunityAction=OpportunityAction,
        )
    )
    _new_desired, _new_baselines, new_telemetry, new_plan = (
        _sender_followup_plan(
            owner=athena.handle,
            ws=new_ws,
            due_rows=(due_row,),
            crm_sheets=crm_sheets,
            OpportunityAction=OpportunityAction,
        )
    )
    assert old_plan is not None
    assert new_plan is not None
    assert len(new_plan.appends) == 1

    owner_state = {
        "Arian": {
            "identity": _followup_identity_blocker_count(old_telemetry),
        },
        "Athena": {
            "identity": _followup_identity_blocker_count(new_telemetry),
        },
    }
    _propagate_linked_owner_blockers(
        owner_state,
        old_telemetry["_linked_blocked_owners"],
        field="identity",
    )

    assert owner_state == {
        "Arian": {"identity": 1},
        "Athena": {"identity": 1},
    }
    assert _blocked_followup_owners(owner_state) == {"Arian", "Athena"}


@pytest.mark.django_db
def test_canonical_foreign_material_blocks_entire_legacy_cohort_before_swap():
    arian, _ = SalesOwner.objects.get_or_create(handle="Arian")
    athena, _ = SalesOwner.objects.get_or_create(handle="Athena")
    opportunity = Opportunity.objects.create(
        account=Account.objects.create(name="Mixed legacy conflict account"),
        owner=athena,
    )
    baseline = {
        crm_sheets.COL_WAITING_UNTIL: "",
        crm_sheets.COL_CHANNEL: "email",
        crm_sheets.COL_DRAFT: "Published destination draft",
        crm_sheets.COL_HANDLED: "FALSE",
        crm_sheets.COL_DISPOSITION: "",
        crm_sheets.COL_MANUAL_PIN: "FALSE",
    }
    action = OpportunityAction.objects.create(
        opportunity=opportunity,
        description="Now belongs to legacy destination owner",
        due_on=timezone.localdate(),
        channel="email",
        draft="Published destination draft",
        sheet_human_snapshot=baseline,
    )
    canonical_old_owner_row = {
        crm_sheets.COL_ACTION_ID: str(action.id),
        crm_sheets.COL_OPPORTUNITY_ID: str(opportunity.id),
        crm_sheets.COL_OWNER: arian.handle,
        **baseline,
        crm_sheets.COL_DRAFT: "Human edit retained on canonical old owner",
        crm_sheets.COL_HUMAN_BASELINE: json.dumps(
            baseline,
            sort_keys=True,
            separators=(",", ":"),
        ),
    }
    headers = list(crm_sheets.FOLLOWUP_HEADERS)
    canonical_ws = SimpleNamespace(
        title=crm_sheets.sender_followups_tab(arian.handle),
        id=401,
        get_all_values=lambda **_kwargs: [
            headers,
            [canonical_old_owner_row.get(header, "") for header in headers],
        ],
    )
    _desired, _baselines, telemetry, _plan = _sender_followup_plan(
        owner=arian.handle,
        ws=canonical_ws,
        due_rows=(),
        crm_sheets=crm_sheets,
        OpportunityAction=OpportunityAction,
    )
    owner_state = {
        owner: {"identity": 0}
        for owner in ("Arian", "Athena", "Chuka", "Leili")
    }
    owner_state[arian.handle]["identity"] = _followup_identity_blocker_count(
        telemetry
    )
    _propagate_linked_owner_blockers(
        owner_state,
        telemetry["_linked_blocked_owners"],
        field="identity",
    )
    canonical_blocked = _blocked_followup_owners(owner_state)
    assert canonical_blocked == {"Arian", "Athena"}

    legacy_worksheets = {
        "Athena": SimpleNamespace(
            id=402,
            title=crm_sheets.sender_followups_tab("Athena"),
        ),
        "Chuka": SimpleNamespace(
            id=403,
            title=crm_sheets.sender_followups_tab("Chuka"),
        ),
    }
    sheet_calls = []

    class NoTouchSpreadsheet:
        def worksheets(self):
            sheet_calls.append("worksheets")
            raise AssertionError("legacy cohort preblock read worksheet titles")

        def add_worksheet(self, **_kwargs):
            sheet_calls.append("add_worksheet")
            raise AssertionError("legacy cohort preblock built a replacement")

        def batch_update(self, _body):
            sheet_calls.append("batch_update")
            raise AssertionError("legacy cohort preblock attempted a title swap")

    baseline_commits = []
    reports, blocked = _publish_legacy_followup_tabs_atomically(
        NoTouchSpreadsheet(),
        legacy_worksheets=legacy_worksheets,
        desired_rows_by_owner={
            "Athena": ({crm_sheets.COL_ACTION_ID: str(action.id)},),
            "Chuka": (),
        },
        legacy_reports={
            "Athena": {"material_rows_skipped": 0},
            "Chuka": {"material_rows_skipped": 0},
        },
        dry_run=False,
        crm_sheets=crm_sheets,
        OpportunityAction=OpportunityAction,
        commit_followup_baselines=lambda updates: baseline_commits.append(updates),
        canonical_blocked_owners=canonical_blocked,
        now=timezone.now(),
    )

    assert blocked == {"Athena", "Chuka"}
    assert sheet_calls == []
    assert baseline_commits == []
    assert reports["Athena"]["status"] == (
        "blocked_by_canonical_foreign_material"
    )
    assert reports["Chuka"]["status"] == (
        "blocked_by_canonical_foreign_material"
    )
    assert all(not item["replacement_built"] for item in reports.values())
    assert all(not item["title_swap_attempted"] for item in reports.values())
    assert [legacy_worksheets[owner].title for owner in ("Athena", "Chuka")] == [
        crm_sheets.sender_followups_tab(owner) for owner in ("Athena", "Chuka")
    ]


@pytest.mark.django_db
def test_unchanged_row_retires_from_old_owner_and_plans_for_new_owner():
    arian, _ = SalesOwner.objects.get_or_create(handle="Arian")
    athena, _ = SalesOwner.objects.get_or_create(handle="Athena")
    opportunity = Opportunity.objects.create(
        account=Account.objects.create(name="Reassigned account"),
        owner=athena,
        manual_pin=False,
    )
    baseline = {
        crm_sheets.COL_WAITING_UNTIL: "",
        crm_sheets.COL_CHANNEL: "email",
        crm_sheets.COL_DRAFT: "Unchanged published draft",
        crm_sheets.COL_HANDLED: "FALSE",
        crm_sheets.COL_DISPOSITION: "",
        crm_sheets.COL_MANUAL_PIN: "FALSE",
    }
    action = OpportunityAction.objects.create(
        opportunity=opportunity,
        description="Follow up after owner reassignment",
        due_on=timezone.localdate(),
        channel="email",
        draft="Unchanged published draft",
        sheet_human_snapshot=baseline,
        sheet_published_at=timezone.now(),
    )
    old_values = {
        crm_sheets.COL_ACTION_ID: str(action.id),
        crm_sheets.COL_OPPORTUNITY_ID: str(opportunity.id),
        crm_sheets.COL_ACCOUNT: opportunity.account.name,
        crm_sheets.COL_OWNER: arian.handle,
        **baseline,
        crm_sheets.COL_HUMAN_BASELINE: json.dumps(
            baseline,
            sort_keys=True,
            separators=(",", ":"),
        ),
    }
    headers = list(crm_sheets.FOLLOWUP_HEADERS)

    class Worksheet:
        def __init__(self, title, rows):
            self.title = title
            self.id = 201
            self._rows = rows

        def get_all_values(self, **_kwargs):
            return self._rows

    old_ws = Worksheet(
        "Arian - Followups",
        [headers, [old_values.get(header, "") for header in headers]],
    )
    old_payload, old_baselines, old_telemetry = _followup_plan_payload(
        owner=arian.handle,
        ws=old_ws,
        due_rows=(),
        crm_sheets=crm_sheets,
        OpportunityAction=OpportunityAction,
    )

    assert old_payload == ()
    assert old_telemetry["foreign_owner_action_rows"] == 1
    assert old_telemetry["foreign_owner_material_action_rows"] == 0
    assert old_telemetry["safe_retired_action_rows"] == 1
    retire_ids = old_telemetry["_safe_retire_action_ids"]
    old_plan = crm_sheets.followups_adapter(old_ws).plan(
        old_payload,
        baseline_by_id=old_baselines,
    )
    _retire_safe_followup_rows(
        old_plan,
        ws=old_ws,
        action_ids=retire_ids,
        crm_sheets=crm_sheets,
    )
    action_id_changes = [
        change for change in old_plan.changes
        if change.column == crm_sheets.COL_ACTION_ID
    ]
    assert len(action_id_changes) == 1
    assert action_id_changes[0].new_value == ""
    change_count = len(old_plan.changes)
    _retire_safe_followup_rows(
        old_plan,
        ws=old_ws,
        action_ids=retire_ids,
        crm_sheets=crm_sheets,
    )
    assert len(old_plan.changes) == change_count

    new_ws = Worksheet("Athena - Followups", [headers])
    due_row = {
        crm_sheets.COL_ACTION_ID: str(action.id),
        crm_sheets.COL_OPPORTUNITY_ID: str(opportunity.id),
        crm_sheets.COL_OWNER: athena.handle,
        **baseline,
    }
    new_payload, new_baselines, new_telemetry = _followup_plan_payload(
        owner=athena.handle,
        ws=new_ws,
        due_rows=(due_row,),
        crm_sheets=crm_sheets,
        OpportunityAction=OpportunityAction,
    )
    new_plan = crm_sheets.followups_adapter(new_ws).plan(
        new_payload,
        baseline_by_id=new_baselines,
    )
    assert new_telemetry["due_now_rows"] == 1
    assert len(new_plan.appends) == 1
    assert new_plan.appends[0][crm_sheets.COL_ACTION_ID] == str(action.id)


def test_malformed_action_id_is_counted_without_querying_or_importing_it():
    headers = list(crm_sheets.FOLLOWUP_HEADERS)
    values = {
        crm_sheets.COL_ACTION_ID: "not-a-valid-action-uuid",
        crm_sheets.COL_DRAFT: "Material orphaned edit",
    }
    ws = SimpleNamespace(
        title="Arian - Followups",
        id=102,
        get_all_values=lambda **_kwargs: [
            headers,
            [values.get(header, "") for header in headers],
        ],
    )

    payload, baselines, telemetry = _followup_plan_payload(
        owner="Arian",
        ws=ws,
        due_rows=(),
        crm_sheets=crm_sheets,
        OpportunityAction=OpportunityAction,
    )

    assert payload == ()
    assert baselines == {}
    assert telemetry["invalid_action_id_rows"] == 1
    assert telemetry["invalid_material_action_rows"] == 1
    assert telemetry["unknown_action_rows"] == 0


def test_unkeyed_nonempty_followup_row_is_sender_identity_blocker():
    headers = list(crm_sheets.FOLLOWUP_HEADERS)
    values = {
        crm_sheets.COL_ACCOUNT: "Account whose Action ID was deleted",
        crm_sheets.COL_DRAFT: "Do not bypass this human draft",
    }
    ws = SimpleNamespace(
        title="Arian - Followups",
        id=105,
        get_all_values=lambda **_kwargs: [
            headers,
            [values.get(header, "") for header in headers],
        ],
    )

    payload, baselines, telemetry, plan = _sender_followup_plan(
        owner="Arian",
        ws=ws,
        due_rows=(),
        crm_sheets=crm_sheets,
        OpportunityAction=OpportunityAction,
    )

    assert payload == ()
    assert baselines == {}
    assert plan is None
    assert telemetry["unkeyed_nonempty_action_rows"] == 1
    assert telemetry["duplicate_action_id_rows"] == 0
    assert _followup_identity_blocker_count(telemetry) == 1


def test_duplicate_followup_action_ids_block_only_that_sender_plan():
    duplicate_id = "not-a-canonical-uuid"
    headers = list(crm_sheets.FOLLOWUP_HEADERS)
    row = {
        crm_sheets.COL_ACTION_ID: duplicate_id,
        crm_sheets.COL_DRAFT: "Retain duplicate-row context",
    }
    ws = SimpleNamespace(
        title="Arian - Followups",
        id=106,
        get_all_values=lambda **_kwargs: [
            headers,
            [row.get(header, "") for header in headers],
            [row.get(header, "") for header in headers],
        ],
    )

    _payload, _baselines, telemetry, plan = _sender_followup_plan(
        owner="Arian",
        ws=ws,
        due_rows=(),
        crm_sheets=crm_sheets,
        OpportunityAction=OpportunityAction,
    )

    assert plan is None
    assert telemetry["duplicate_action_id_groups"] == 1
    assert telemetry["duplicate_action_id_rows"] == 2
    assert _followup_identity_blocker_count(telemetry) >= 2


def test_malformed_followup_portable_baseline_blocks_only_sender_plan():
    headers = list(crm_sheets.FOLLOWUP_HEADERS)
    action_id = "not-a-canonical-uuid"
    row = {
        crm_sheets.COL_ACTION_ID: action_id,
        crm_sheets.COL_HUMAN_BASELINE: "{not valid json",
    }
    ws = SimpleNamespace(
        title="Arian - Followups",
        id=107,
        get_all_values=lambda **_kwargs: [
            headers,
            [row.get(header, "") for header in headers],
        ],
    )

    _payload, _baselines, telemetry, plan = _sender_followup_plan(
        owner="Arian",
        ws=ws,
        due_rows=(),
        crm_sheets=crm_sheets,
        OpportunityAction=OpportunityAction,
    )

    assert plan is None
    assert telemetry["malformed_baseline_action_rows"] == 1
    assert _followup_identity_blocker_count(telemetry) >= 1


def test_unkeyed_nonempty_opportunity_row_is_identity_blocker():
    headers = list(crm_sheets.OPPORTUNITY_HEADERS)
    row = {
        crm_sheets.COL_ACCOUNT: "Account whose Opportunity ID was deleted",
        crm_sheets.COL_NEXT_ACTION: "Preserve this operator next step",
    }
    ws = SimpleNamespace(
        title=crm_sheets.OPPORTUNITIES_TAB,
        id=108,
        get_all_values=lambda **_kwargs: [
            headers,
            [row.get(header, "") for header in headers],
        ],
    )

    plan = crm_sheets.OpportunitySheetAdapter(ws).plan(())

    assert plan.unkeyed_nonempty_rows == (2,)
    assert _opportunity_identity_blocker_count(plan) == 1


def test_unknown_action_with_unchanged_baseline_defaults_is_safe_to_retire():
    action_id = str(uuid.uuid4())
    baseline = {
        crm_sheets.COL_WAITING_UNTIL: "",
        crm_sheets.COL_CHANNEL: "",
        crm_sheets.COL_DRAFT: "",
        crm_sheets.COL_HANDLED: "FALSE",
        crm_sheets.COL_DISPOSITION: "",
        crm_sheets.COL_MANUAL_PIN: "FALSE",
    }
    headers = list(crm_sheets.FOLLOWUP_HEADERS)
    values = {
        crm_sheets.COL_ACTION_ID: action_id,
        **baseline,
        crm_sheets.COL_HUMAN_BASELINE: json.dumps(baseline),
    }
    ws = SimpleNamespace(
        title="Arian - Followups",
        id=103,
        get_all_values=lambda **_kwargs: [
            headers,
            [values.get(header, "") for header in headers],
        ],
    )

    payload, _baselines, telemetry = _followup_plan_payload(
        owner="Arian",
        ws=ws,
        due_rows=(),
        crm_sheets=crm_sheets,
        OpportunityAction=OpportunityAction,
    )

    assert payload == ()
    assert telemetry["unknown_action_rows"] == 1
    assert telemetry["unknown_material_action_rows"] == 0
    assert telemetry["safe_retired_action_rows"] == 1
    assert telemetry["_safe_retire_action_ids"] == (action_id,)


@pytest.mark.django_db
def test_operator_added_followup_content_blocks_owner_reassignment_retirement():
    athena, _ = SalesOwner.objects.get_or_create(handle="Athena")
    opportunity = Opportunity.objects.create(
        account=Account.objects.create(name="Operator content account"),
        owner=athena,
    )
    baseline = {
        crm_sheets.COL_WAITING_UNTIL: "",
        crm_sheets.COL_CHANNEL: "email",
        crm_sheets.COL_DRAFT: "Published draft",
        crm_sheets.COL_HANDLED: "FALSE",
        crm_sheets.COL_DISPOSITION: "",
        crm_sheets.COL_MANUAL_PIN: "FALSE",
    }
    action = OpportunityAction.objects.create(
        opportunity=opportunity,
        description="Reassigned with operator note",
        channel="email",
        draft="Published draft",
        sheet_human_snapshot=baseline,
    )
    headers = [*crm_sheets.FOLLOWUP_HEADERS, "Operator Notes"]
    values = {
        crm_sheets.COL_ACTION_ID: str(action.id),
        **baseline,
        "Operator Notes": "Keep this context attached to the action",
    }
    ws = SimpleNamespace(
        title="Arian - Followups",
        id=104,
        get_all_values=lambda **_kwargs: [
            headers,
            [values.get(header, "") for header in headers],
        ],
    )

    payload, _baselines, telemetry = _followup_plan_payload(
        owner="Arian",
        ws=ws,
        due_rows=(),
        crm_sheets=crm_sheets,
        OpportunityAction=OpportunityAction,
    )

    assert payload == ()
    assert telemetry["operator_content_action_rows"] == 1
    assert telemetry["foreign_owner_material_action_rows"] == 1
    assert telemetry["safe_retired_action_rows"] == 0


@pytest.mark.django_db
@pytest.mark.parametrize(
    "late_change",
    (
        "human_edit",
        "unkeyed",
        "managed_formula",
        "baseline_edit",
        "operator_column",
        "owner_reassignment",
    ),
)
def test_final_sender_preflight_blocks_late_retirement_safety_changes(
    late_change,
):
    arian, _ = SalesOwner.objects.get_or_create(handle="Arian")
    athena, _ = SalesOwner.objects.get_or_create(handle="Athena")
    opportunity = Opportunity.objects.create(
        account=Account.objects.create(name=f"Late change {late_change}"),
        owner=athena,
    )
    baseline = {
        crm_sheets.COL_WAITING_UNTIL: "",
        crm_sheets.COL_CHANNEL: "email",
        crm_sheets.COL_DRAFT: "Published draft",
        crm_sheets.COL_HANDLED: "FALSE",
        crm_sheets.COL_DISPOSITION: "",
        crm_sheets.COL_MANUAL_PIN: "FALSE",
    }
    action = OpportunityAction.objects.create(
        opportunity=opportunity,
        description="Potential stale retirement",
        channel="email",
        draft="Published draft",
        sheet_human_snapshot=baseline,
    )
    base_headers = list(crm_sheets.FOLLOWUP_HEADERS)
    base_values = {
        crm_sheets.COL_ACTION_ID: str(action.id),
        crm_sheets.COL_OPPORTUNITY_ID: str(opportunity.id),
        crm_sheets.COL_OWNER: arian.handle,
        **baseline,
        crm_sheets.COL_HUMAN_BASELINE: json.dumps(
            baseline,
            sort_keys=True,
            separators=(",", ":"),
        ),
    }
    writes = []

    class ChangingWorksheet:
        title = "Arian - Followups"
        id = 301

        def __init__(self):
            self.reads = 0

        def get_all_values(self, **_kwargs):
            self.reads += 1
            headers = list(base_headers)
            values = dict(base_values)
            if self.reads >= 3:
                if late_change == "human_edit":
                    values[crm_sheets.COL_DRAFT] = "Late human edit"
                elif late_change == "unkeyed":
                    values[crm_sheets.COL_ACTION_ID] = ""
                elif late_change == "managed_formula":
                    values[crm_sheets.COL_CHANNEL] = '=IF(TRUE,"email","")'
                elif late_change == "baseline_edit":
                    changed = {**baseline, crm_sheets.COL_DRAFT: "Changed baseline"}
                    values[crm_sheets.COL_HUMAN_BASELINE] = json.dumps(
                        changed,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                elif late_change == "operator_column":
                    headers.append("Operator note")
                    values["Operator note"] = "Late context; do not clear ID"
                elif late_change == "owner_reassignment":
                    Opportunity.objects.filter(pk=opportunity.pk).update(
                        owner=arian,
                    )
            return [
                headers,
                [values.get(header, "") for header in headers],
            ]

        def update_cells(self, *_args, **_kwargs):
            writes.append("update_cells")

        def batch_clear(self, *_args, **_kwargs):
            writes.append("batch_clear")

    ws = ChangingWorksheet()

    _desired, telemetry, plan = _stable_sender_publication_plan(
        owner=arian.handle,
        ws=ws,
        due_rows=(),
        crm_sheets=crm_sheets,
        OpportunityAction=OpportunityAction,
    )

    assert plan is None
    assert telemetry["concurrent_sheet_change_rows"] == 1
    assert _followup_identity_blocker_count(telemetry) >= 1
    assert writes == []


def test_action_telemetry_sums_every_mutation_counter_across_passes():
    initial = SimpleNamespace(
        actions_created=1,
        actions_completed=2,
        actions_superseded=3,
        actions_targeted=4,
        activity_updated=5,
        counts=lambda: {
            "actions_created": 1,
            "actions_completed": 2,
            "actions_superseded": 3,
            "actions_targeted": 4,
            "activity_updated": 5,
            "surface_counts": {"daily": 1},
        },
    )
    final = SimpleNamespace(
        actions_created=10,
        actions_completed=20,
        actions_superseded=30,
        actions_targeted=40,
        activity_updated=50,
        counts=lambda: {
            "actions_created": 10,
            "actions_completed": 20,
            "actions_superseded": 30,
            "actions_targeted": 40,
            "activity_updated": 50,
            "surface_counts": {"recovery": 2},
        },
    )

    counts = _action_counts_for_run(initial, final)

    assert counts["actions_created"] == 11
    assert counts["actions_completed"] == 22
    assert counts["actions_superseded"] == 33
    assert counts["actions_targeted"] == 44
    assert counts["activity_updated"] == 55
    assert counts["surface_counts"] == {"recovery": 2}
    assert counts["passes"] == {
        "initial": initial.counts(),
        "post_legacy_import": final.counts(),
    }


def test_followup_block_telemetry_keeps_each_exact_reason_count():
    assert _followup_block_summary(
        initial_conflicts=1,
        invalid_imports=2,
        initial_identity_blockers=3,
        fresh_conflicts=4,
        fresh_imports_remaining=5,
        fresh_identity_blockers=6,
    ) == {
        "blocked": True,
        "reason": "invalid, conflicting, or unsafe human action edit",
        "owners_blocked": 0,
        "initial_conflicts": 1,
        "invalid_imports": 2,
        "initial_identity_blockers": 3,
        "fresh_conflicts": 4,
        "fresh_imports_remaining": 5,
        "fresh_identity_blockers": 6,
        "publication_conflicts": 0,
        "publication_imports_remaining": 0,
        "publication_identity_blockers": 0,
    }


def test_followup_merge_blockers_are_sender_scoped():
    owner_state = {
        "Arian": {
            "initial_conflicts": 1,
            "invalid_imports": 0,
        },
        "Athena": {
            "initial_conflicts": 0,
            "invalid_imports": 0,
        },
        "Chuka": {
            "initial_conflicts": 0,
            "invalid_imports": 0,
        },
        "Leili": {
            "initial_conflicts": 0,
            "invalid_imports": 0,
        },
    }

    assert _blocked_followup_owners(owner_state) == {"Arian"}


def test_failed_atomic_legacy_archive_blocks_only_affected_archive_owners():
    assert _legacy_followup_blocked_owners(set()) == set()
    assert _legacy_followup_blocked_owners({"Arian"}) == {"Arian"}
