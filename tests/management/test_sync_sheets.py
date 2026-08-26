from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from crm.models import Deal, Lead
from linkedin.enums import ProfileState
from linkedin.crm_lock import crm_refresh_lock
from linkedin.management.commands.sync_sheets import Command, run_people_sync
from linkedin.models import Campaign
from linkedin.notifications import sheets


class _Worksheet:
    def __init__(self):
        self.appended = []
        self.updated = []
        self.rows = []

    def append_rows(self, rows, **kwargs):
        self.appended.extend(rows)

    def batch_update(self, updates, **kwargs):
        self.updated.extend(updates)

    def batch_get(self, ranges, **_kwargs):
        values = []
        for cell_range in ranges:
            column_name = str(cell_range).split("1", 1)[0]
            column = 0
            for character in column_name.upper():
                column = column * 26 + ord(character) - ord("A") + 1
            cells = []
            for row in self.rows:
                value = row[column - 1] if column <= len(row) else ""
                cells.append([value])
            values.append(cells)
        return values


def _configure_people_index(monkeypatch, index):
    loaded_with = []
    index.ws.rows = [list(row) for row in index.rows]

    def fake_load(*, apply_schema=True):
        loaded_with.append(apply_schema)
        return index

    monkeypatch.setattr(sheets.SheetIndex, "load", fake_load)
    monkeypatch.setattr("linkedin.conf.GOOGLE_SHEETS_ID", "crm-sheet-id")
    monkeypatch.setattr(
        "linkedin.conf.GOOGLE_SHEETS_CREDENTIALS_PATH",
        "service-account.json",
    )
    return loaded_with


def test_standalone_people_sync_shares_the_refresh_lock():
    with crm_refresh_lock():
        with pytest.raises(CommandError, match="already running"):
            call_command("sync_sheets", dry_run=True, stdout=StringIO())


def test_internal_people_sync_can_reuse_an_already_held_refresh_lock(monkeypatch):
    worksheet = _Worksheet()
    index = sheets.SheetIndex(worksheet, [list(sheets.HEADERS)])
    _configure_people_index(monkeypatch, index)

    with crm_refresh_lock():
        result = run_people_sync(
            dry_run=True,
            stdout=StringIO(),
            stderr=StringIO(),
            lock_held=True,
        )

    assert result["status"] == "planned"
    assert worksheet.appended == []
    assert worksheet.updated == []


def test_dry_run_uses_live_index_plan_with_read_only_schema_and_no_writes(
    fake_session,
    monkeypatch,
):
    lead = Lead.objects.create(
        first_name="Jane",
        last_name="Doe",
        company_name="Acme",
        linkedin_url="https://www.linkedin.com/in/jane-sync-sheets/",
    )
    Deal.objects.create(
        lead=lead,
        campaign=fake_session.campaign,
        state=ProfileState.CONNECTED,
    )
    worksheet = _Worksheet()
    index = sheets.SheetIndex(worksheet, [list(sheets.HEADERS)])
    loaded_with = _configure_people_index(monkeypatch, index)
    stdout = StringIO()
    stderr = StringIO()

    call_command("sync_sheets", dry_run=True, stdout=stdout, stderr=stderr)

    assert loaded_with == [False]
    assert "Exact People plan" in stdout.getvalue()
    assert "appended:1" in stdout.getvalue()
    assert "No writes" in stdout.getvalue()
    assert worksheet.appended == []
    assert worksheet.updated == []


def test_apply_sync_appends_one_lead_once_when_it_has_two_deals(
    fake_session,
    monkeypatch,
):
    lead = Lead.objects.create(
        first_name="One",
        last_name="Contact",
        company_name="Acme",
        linkedin_url="https://www.linkedin.com/in/one-contact/",
    )
    second_campaign = Campaign.objects.create(
        name="Second People Ledger Campaign",
        user=fake_session.django_user,
    )
    Deal.objects.create(
        lead=lead,
        campaign=fake_session.campaign,
        state=ProfileState.QUALIFIED,
    )
    Deal.objects.create(
        lead=lead,
        campaign=second_campaign,
        state=ProfileState.CONNECTED,
    )
    worksheet = _Worksheet()
    index = sheets.SheetIndex(worksheet, [list(sheets.HEADERS)])
    loaded_with = _configure_people_index(monkeypatch, index)

    result = run_people_sync(
        dry_run=False,
        stdout=StringIO(),
        stderr=StringIO(),
    )

    assert loaded_with == [True]
    assert result["source_leads"] == 1
    assert result["source_deals"] == 2
    assert result["appended"] == 1
    assert result["rows_before"] == 0
    assert result["rows_after"] == 1
    assert len(worksheet.appended) == 1
    appended = worksheet.appended[0]
    assert appended[index.actual_index_0[sheets.COL_LEAD_ID]] == str(lead.pk)
    assert appended[index.actual_index_0[sheets.COL_OUTREACH_STATUS]] == (
        sheets.STATUS_CONNECTED
    )


def test_sync_includes_no_deal_id_only_and_pre_funnel_contacts(
    fake_session,
    monkeypatch,
):
    id_only = Lead.objects.create(first_name="Email", last_name="Only")
    qualified = Lead.objects.create(
        first_name="Early",
        last_name="Lead",
        linkedin_url="https://www.linkedin.com/in/early-lead/",
    )
    Deal.objects.create(
        lead=qualified,
        campaign=fake_session.campaign,
        state=ProfileState.READY_TO_CONNECT,
    )
    disqualified = Lead.objects.create(
        first_name="Historical",
        last_name="Disqualified",
        linkedin_url="https://www.linkedin.com/in/historical-disqualified/",
        disqualified=True,
    )
    worksheet = _Worksheet()
    index = sheets.SheetIndex(worksheet, [list(sheets.HEADERS)])
    _configure_people_index(monkeypatch, index)

    result = run_people_sync(
        dry_run=False,
        stdout=StringIO(),
        stderr=StringIO(),
    )

    assert result["source_leads"] == 3
    assert result["source_deals"] == 1
    assert result["appended"] == 3
    by_id = {
        row[index.actual_index_0[sheets.COL_LEAD_ID]]: row
        for row in worksheet.appended
    }
    assert by_id[str(id_only.pk)][index.actual_index_0[sheets.COL_LINKEDIN_URL]] == ""
    assert by_id[str(qualified.pk)][index.actual_index_0[sheets.COL_OUTREACH_STATUS]] == (
        sheets.STATUS_INVITE_SENT
    )
    assert by_id[str(disqualified.pk)][
        index.actual_index_0[sheets.COL_OUTREACH_STATUS]
    ] == sheets.STATUS_DONT_SEND


def test_empty_source_never_shrinks_or_rewrites_existing_people(
    monkeypatch,
):
    actual_headers = [*sheets.HEADERS, "Apollo Email"]
    existing = {header: "" for header in actual_headers}
    existing.update({
        sheets.COL_LEAD_ID: "legacy-1",
        sheets.COL_LINKEDIN_URL: "https://www.linkedin.com/in/legacy-one/",
        sheets.COL_NOTES: "human note",
        "Apollo Email": '=IF(A2="Legacy","legacy@example.com","")',
    })
    original_row = [existing[header] for header in actual_headers]
    worksheet = _Worksheet()
    index = sheets.SheetIndex(
        worksheet,
        [actual_headers, list(original_row)],
        actual_headers=actual_headers,
    )
    _configure_people_index(monkeypatch, index)

    result = run_people_sync(
        dry_run=False,
        stdout=StringIO(),
        stderr=StringIO(),
    )

    assert result["source_leads"] == 0
    assert result["appended"] == 0
    assert result["updated"] == 0
    assert result["rows_before"] == result["rows_after"] == 1
    assert index.rows[1] == original_row
    assert worksheet.appended == []
    assert worksheet.updated == []


def test_campaign_partial_sync_only_appends_selected_lead_and_keeps_other_row(
    fake_session,
    monkeypatch,
):
    selected = Lead.objects.create(
        first_name="Selected",
        linkedin_url="https://www.linkedin.com/in/selected/",
    )
    Deal.objects.create(
        lead=selected,
        campaign=fake_session.campaign,
        state=ProfileState.CONNECTED,
    )
    other_campaign = Campaign.objects.create(
        name="Other Partial Source Campaign",
        user=fake_session.django_user,
    )
    other = Lead.objects.create(
        first_name="Other",
        linkedin_url="https://www.linkedin.com/in/other/",
    )
    Deal.objects.create(
        lead=other,
        campaign=other_campaign,
        state=ProfileState.CONNECTED,
    )
    existing = {header: "" for header in sheets.HEADERS}
    existing.update({
        sheets.COL_LEAD_ID: str(other.pk),
        sheets.COL_LINKEDIN_URL: other.linkedin_url,
        sheets.COL_NOTES: "must survive partial source",
    })
    original_row = [existing[header] for header in sheets.HEADERS]
    worksheet = _Worksheet()
    index = sheets.SheetIndex(
        worksheet,
        [list(sheets.HEADERS), list(original_row)],
    )
    _configure_people_index(monkeypatch, index)
    command = Command(stdout=StringIO(), stderr=StringIO())

    command.handle(dry_run=False, campaign=fake_session.campaign.pk)

    assert command.result["source_leads"] == 1
    assert command.result["appended"] == 1
    assert index.rows[1] == original_row
    assert len(worksheet.appended) == 1
    assert worksheet.appended[0][index.actual_index_0[sheets.COL_LEAD_ID]] == (
        str(selected.pk)
    )


def test_legacy_duplicate_url_is_reported_as_represented_not_omitted(
    monkeypatch,
):
    lead = Lead.objects.create(
        first_name="Duplicate",
        linkedin_url="https://www.linkedin.com/in/duplicate-ledger/",
    )
    first = {header: "" for header in sheets.HEADERS}
    first[sheets.COL_LINKEDIN_URL] = lead.linkedin_url
    second = dict(first)
    second[sheets.COL_LINKEDIN_URL] = (
        "https://linkedin.com/in/duplicate-ledger?trk=legacy"
    )
    worksheet = _Worksheet()
    index = sheets.SheetIndex(
        worksheet,
        [
            list(sheets.HEADERS),
            [first[header] for header in sheets.HEADERS],
            [second[header] for header in sheets.HEADERS],
        ],
    )
    _configure_people_index(monkeypatch, index)

    result = run_people_sync(
        dry_run=True,
        stdout=StringIO(),
        stderr=StringIO(),
    )

    assert result["source_leads"] == 1
    assert result["ambiguous_existing"] == 1
    assert result["errored"] == 0
    assert result["duplicate_lead_ids"] == 0
    assert result["duplicate_linkedin_urls"] == 1
    assert result["appended"] == 0


def test_url_claimed_by_another_stable_id_is_a_true_people_error(monkeypatch):
    lead = Lead.objects.create(
        first_name="Expected",
        linkedin_url="https://www.linkedin.com/in/claimed-url/",
    )
    other = Lead.objects.create(
        first_name="Other",
        linkedin_url="https://www.linkedin.com/in/other-url/",
    )
    existing = {header: "" for header in sheets.HEADERS}
    existing.update({
        sheets.COL_LEAD_ID: str(other.id),
        sheets.COL_LINKEDIN_URL: lead.linkedin_url,
    })
    worksheet = _Worksheet()
    index = sheets.SheetIndex(
        worksheet,
        [
            list(sheets.HEADERS),
            [existing[header] for header in sheets.HEADERS],
        ],
    )
    _configure_people_index(monkeypatch, index)

    result = run_people_sync(
        dry_run=True,
        stdout=StringIO(),
        stderr=StringIO(),
    )

    assert result["errored"] == 1
    assert result["ambiguous_existing"] == 0
