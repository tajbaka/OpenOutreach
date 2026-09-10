"""Native Sheets writes stay inside the owned projection; all I/O is faked."""
from copy import deepcopy
from datetime import datetime, timezone
import io
import json
import re

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection

from linkedin.accepted_connections import AcceptedConnection
from linkedin.exceptions import SheetsError
from linkedin.notifications import accepted_connections_sheet as pub


class NativeSheet:
    """Small native grid fake for the exact requests this publisher uses."""
    id = "report-workbook"

    def __init__(self):
        self.sheets = [{"properties": {"title": "People", "sheetId": 7,
                       "gridProperties": {"rowCount": 100, "columnCount": 5}},
                       "cells": [[{"userEnteredValue": {"stringValue": "untouched"}}]]}]
        self.batches = []

    @property
    def report(self):
        return next(s for s in self.sheets if s["properties"]["title"] == pub.TAB_NAME)

    def fetch_sheet_metadata(self, *, params):
        if "ranges" not in params:
            return {"spreadsheetId": self.id, "sheets": [
                {k: deepcopy(v) for k, v in s.items() if k != "cells"} for s in self.sheets
            ]}
        start, end = map(int, re.search(r"!A(\d+):[A-Z]+(\d+)$", params["ranges"]).groups())
        return {"sheets": [{"data": [{"startRow": start - 1, "rowData": [
            {"values": deepcopy(cells)} for cells in self.report["cells"][start - 1:end]
        ]}]}]}

    def batch_update(self, body):
        self.batches.append(deepcopy(body))
        for req in body["requests"]:
            kind, value = next(iter(req.items()))
            if kind == "addSheet":
                self.sheets.append({**deepcopy(value), "cells": []})
            elif kind == "appendDimension":
                self.report["properties"]["gridProperties"]["rowCount"] += value["length"]
            elif kind == "updateCells":
                assert value["range"]["sheetId"] == self.report["properties"]["sheetId"]
                assert value["range"]["startColumnIndex"] == 0
                assert value["range"]["endColumnIndex"] == 5
                self.report["cells"] = [deepcopy(r["values"]) for r in value["rows"]]
            elif kind == "setBasicFilter":
                self.report["basicFilter"] = deepcopy(value["filter"])
            else:
                assert kind in {"repeatCell", "updateDimensionProperties"}


@pytest.fixture
def report_env(monkeypatch):
    from linkedin import conf
    from linkedin.notifications import sheets
    book = NativeSheet()
    rows = [AcceptedConnection(1, "Arian", datetime(2026, 9, 10, 17, tzinfo=timezone.utc),
                              "=Test Person", "Example", "https://www.linkedin.com/in/example/")]
    monkeypatch.setattr(sheets, "_gspread_client", lambda: book)
    monkeypatch.setattr(conf, "GOOGLE_SHEETS_ID", book.id)
    monkeypatch.setattr(pub, "build_accepted_connections", lambda: (list(rows), {}))
    return book, rows


def test_default_preview_has_no_writes(report_env):
    book, rows = report_env
    result = pub.sync_accepted_connections()
    assert result["status"] == "planned"
    assert result["rows"] == len(rows)
    assert book.batches == []
    assert len(book.sheets) == 1


def test_atomic_create_native_dates_literal_names_and_idempotent_readback(report_env):
    book, rows = report_env
    people_before = deepcopy(book.sheets[0])
    result = pub.sync_accepted_connections(dry_run=False)
    assert result["verified"] and result["created"]
    assert len(book.batches) == 1
    assert "addSheet" in book.batches[0]["requests"][0]
    assert book.sheets[0] == people_before
    grid = book.report["properties"]["gridProperties"]
    assert grid["columnCount"] == 5 and grid["frozenRowCount"] == 1
    native = book.report["cells"][1]
    assert native[0]["userEnteredValue"] == {"numberValue": rows[0].sheet_values()[0]}
    assert native[0]["userEnteredFormat"]["numberFormat"]["pattern"] == rows[0].date_format
    assert native[2]["userEnteredValue"] == {"stringValue": "=Test Person"}
    assert pub.sync_accepted_connections(dry_run=False)["changed"] is False
    assert len(book.batches) == 1


def test_refresh_clears_removed_rows_and_keeps_filter_criteria(report_env):
    book, rows = report_env
    pub.sync_accepted_connections(dry_run=False)
    criteria = {"1": {"hiddenValues": ["Eddy"]}}
    book.report["basicFilter"]["criteria"] = criteria
    rows.clear()
    result = pub.sync_accepted_connections(dry_run=False)
    assert result["rows_before"] == 1 and result["rows"] == 0
    assert book.report["cells"][1] == [{}, {}, {}, {}, {}]
    assert book.report["basicFilter"]["range"]["endRowIndex"] == 1
    assert book.report["basicFilter"]["criteria"] == criteria
    assert pub._read_rows(book, book.report) == [list(pub.HEADERS)]


@pytest.mark.parametrize("issue", ["formula", "chip", "validation", "extra-column", "header", "merges", "tables"])
def test_unknown_native_structure_refused_before_any_more_writes(report_env, issue):
    book, _ = report_env
    pub.sync_accepted_connections(dry_run=False)
    cell = book.report["cells"][1][2]
    if issue == "formula":
        cell["userEnteredValue"] = {"formulaValue": "=1+1"}
    elif issue == "chip":
        cell["chipRuns"] = [{"startIndex": 0}]
    elif issue == "validation":
        cell["dataValidation"] = {"condition": {"type": "BOOLEAN"}}
    elif issue == "extra-column":
        book.report["properties"]["gridProperties"]["columnCount"] = 6
        book.report["cells"][1].append({"userEnteredValue": {"stringValue": "human note"}})
    elif issue == "header":
        book.report["cells"][0][0] = {"userEnteredValue": {"stringValue": "My own table"}}
    else:
        book.report[issue] = [{"test": True}]
    with pytest.raises(SheetsError):
        pub.sync_accepted_connections(dry_run=False)
    assert len(book.batches) == 1


def test_wrong_workbook_refused(report_env, monkeypatch):
    from linkedin import conf
    book, _ = report_env
    monkeypatch.setattr(conf, "GOOGLE_SHEETS_ID", "another-workbook")
    with pytest.raises(SheetsError, match="unexpected workbook"):
        pub.sync_accepted_connections(dry_run=False)
    assert book.batches == []


def test_failed_readback_and_unexpected_api_error_surface(report_env, monkeypatch):
    book, _ = report_env
    monkeypatch.setattr(book, "batch_update", lambda body: None)
    with pytest.raises(SheetsError, match="readback"):
        pub.sync_accepted_connections(dry_run=False)
    def fail(body):
        raise RuntimeError("API failed")
    monkeypatch.setattr(book, "batch_update", fail)
    with pytest.raises(RuntimeError, match="API failed"):
        pub.sync_accepted_connections(dry_run=False)


def test_command_default_preview_and_explicit_apply(report_env):
    book, _ = report_env
    output = io.StringIO()
    call_command("sync_accepted_connections", stdout=output)
    assert json.loads(output.getvalue())["status"] == "planned"
    assert book.batches == []
    output = io.StringIO()
    call_command("sync_accepted_connections", "--apply", stdout=output)
    assert json.loads(output.getvalue())["verified"]


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("apply,failed", [(False, False), (True, False), (True, True)])
def test_refresh_invokes_projection_after_its_crm_transaction(monkeypatch, apply, failed):
    from crm.models import Account
    from linkedin.management.commands.refresh_crm_v2 import Command
    def run(self, options, **kwargs):
        assert connection.in_atomic_block
        Account.objects.create(name="Committed before reporting")
        return {"publication": {}}
    calls = []
    def publish(*, dry_run):
        assert not connection.in_atomic_block
        assert Account.objects.filter(name="Committed before reporting").exists() is apply
        calls.append(dry_run)
        if failed:
            raise SheetsError("Reporting failed")
        return {"status": "planned" if dry_run else "published"}
    monkeypatch.setattr(Command, "_run", run)
    monkeypatch.setattr(pub, "sync_accepted_connections", publish)
    args = ["--apply", "--routine"] if apply else []
    if failed:
        with pytest.raises(CommandError, match="Reporting failed"):
            call_command("refresh_crm_v2", *args)
        assert Account.objects.filter(name="Committed before reporting").exists()
    else:
        call_command("refresh_crm_v2", *args, stdout=io.StringIO())
    assert calls == [not apply]
