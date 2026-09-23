from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import json
import re

from django.core.management import call_command

from linkedin.linkedin_pending import LinkedInPending
from linkedin.notifications import linkedin_pending_sheet as pub


NOW = datetime(2026, 9, 22, 16, tzinfo=timezone.utc)


def row(key, *, lead_id, days=1, message="Can you send pricing?"):
    return LinkedInPending(
        pending_id=key,
        lead_id=lead_id,
        operator="Arian",
        thread_external_id=f"thread-{lead_id}",
        message_external_id=f"message-{lead_id}-{days}",
        received_at=NOW - timedelta(days=days),
        name=f"Person {lead_id}",
        company=f"Company {lead_id}",
        linkedin_url=f"https://www.linkedin.com/in/person-{lead_id}/",
        message=message,
    )


class NativeSheet:
    id = "pending-workbook"

    def __init__(self):
        self.sheets = [{
            "properties": {
                "title": "People",
                "sheetId": 7,
                "gridProperties": {"rowCount": 100, "columnCount": 5},
            },
            "cells": [[{"userEnteredValue": {"stringValue": "untouched"}}]],
        }]
        self.batches = []

    @property
    def report(self):
        return next(
            sheet for sheet in self.sheets
            if sheet["properties"]["title"] == pub.TAB_NAME
        )

    def fetch_sheet_metadata(self, *, params):
        if "ranges" not in params:
            return {"spreadsheetId": self.id, "sheets": [
                {key: deepcopy(value) for key, value in sheet.items() if key != "cells"}
                for sheet in self.sheets
            ]}
        start, end = map(
            int,
            re.search(r"!A(\d+):[A-Z]+(\d+)$", params["ranges"]).groups(),
        )
        return {"sheets": [{"data": [{
            "startRow": start - 1,
            "rowData": [
                {"values": deepcopy(cells)}
                for cells in self.report["cells"][start - 1:end]
            ],
        }]}]}

    def batch_update(self, body):
        self.batches.append(deepcopy(body))
        for request in body["requests"]:
            kind, value = next(iter(request.items()))
            if kind == "addSheet":
                self.sheets.append({
                    "properties": deepcopy(value["properties"]),
                    "cells": [],
                    "conditionalFormats": [],
                })
            elif kind == "updateCells":
                start = value["range"]["startRowIndex"]
                rows = [deepcopy(item["values"]) for item in value["rows"]]
                while len(self.report["cells"]) < start + len(rows):
                    self.report["cells"].append([])
                self.report["cells"][start:start + len(rows)] = rows
            elif kind == "deleteDimension":
                start = value["range"]["startIndex"]
                end = value["range"]["endIndex"]
                del self.report["cells"][start:end]
                self.report["properties"]["gridProperties"]["rowCount"] -= end - start
            elif kind == "appendDimension":
                self.report["properties"]["gridProperties"]["rowCount"] += value["length"]
            elif kind == "setBasicFilter":
                self.report["basicFilter"] = deepcopy(value["filter"])
            elif kind == "addConditionalFormatRule":
                self.report["conditionalFormats"].insert(
                    value["index"], deepcopy(value["rule"])
                )
            elif kind == "deleteConditionalFormatRule":
                del self.report["conditionalFormats"][value["index"]]
            elif kind == "moveDimension":
                start = value["source"]["startIndex"]
                end = value["source"]["endIndex"]
                assert end - start == 1
                moved = self.report["cells"].pop(start)
                self.report["cells"].insert(value["destinationIndex"], moved)
            else:
                assert kind in {"repeatCell", "updateDimensionProperties"}


def setup_env(monkeypatch):
    from linkedin import conf
    from linkedin.notifications import sheets

    book = NativeSheet()
    rows = [row("pending-a", lead_id=1), row("pending-b", lead_id=2, days=2)]
    monkeypatch.setattr(sheets, "_gspread_client", lambda: book)
    monkeypatch.setattr(conf, "GOOGLE_SHEETS_ID", book.id)
    monkeypatch.setattr(pub, "build_linkedin_pending", lambda **_kwargs: (list(rows), {}))
    return book, rows


def test_preview_has_no_writes(monkeypatch):
    book, rows = setup_env(monkeypatch)

    report = pub.sync_linkedin_pending(now=NOW)

    assert report["status"] == "planned"
    assert report["appended"] == len(rows)
    assert book.batches == []


def test_create_then_incrementally_append_update_and_delete(monkeypatch):
    book, rows = setup_env(monkeypatch)
    people_before = deepcopy(book.sheets[0])

    created = pub.sync_linkedin_pending(dry_run=False, now=NOW)

    assert created["verified"] is True
    assert created["created"] is True
    assert book.sheets[0] == people_before
    assert book.report["properties"]["gridProperties"]["frozenRowCount"] == 1
    assert len(book.report["conditionalFormats"]) == 6
    formulas = {
        rule["booleanRule"]["condition"]["values"][0]["userEnteredValue"]
        for rule in book.report["conditionalFormats"]
    }
    assert '=AND($H2<>"",$G2>=7)' in formulas
    assert '=AND($H2<>"",$G2>=3,$G2<7)' in formulas
    assert '=AND($H2<>"",$G2<3)' in formulas

    rows[:] = [
        row("pending-a", lead_id=1, message="Could you send the revised pricing?"),
        row("pending-c", lead_id=3),
    ]
    before_batches = len(book.batches)
    result = pub.sync_linkedin_pending(dry_run=False, now=NOW)

    assert result["appended"] == 1
    assert result["updated"] == 1
    assert result["deleted"] == 1
    assert result["rows"] == 2
    assert result["verified"] is True
    requests = book.batches[before_batches]["requests"]
    assert sum("deleteDimension" in request for request in requests) == 1
    updates = [request["updateCells"] for request in requests if "updateCells" in request]
    assert len(updates) == 2
    assert all(update["range"]["endRowIndex"] - update["range"]["startRowIndex"] == 1
               for update in updates)
    assert pub._read_rows(book, book.report) == [
        list(pub.HEADERS),
        rows[0].sheet_values(now=NOW),
        rows[1].sheet_values(now=NOW),
    ]


def test_existing_rows_move_to_builder_urgency_order_without_rewrite(monkeypatch):
    book, rows = setup_env(monkeypatch)
    pub.sync_linkedin_pending(dry_run=False, now=NOW)
    rows.reverse()
    before_batches = len(book.batches)

    result = pub.sync_linkedin_pending(dry_run=False, now=NOW)

    requests = book.batches[before_batches]["requests"]
    assert result["updated"] == 0
    assert sum("moveDimension" in request for request in requests) == 1
    assert not any("updateCells" in request for request in requests)
    assert pub._read_rows(book, book.report) == [
        list(pub.HEADERS),
        rows[0].sheet_values(now=NOW),
        rows[1].sheet_values(now=NOW),
    ]


def test_command_defaults_to_preview_and_apply_is_explicit(monkeypatch):
    book, _rows = setup_env(monkeypatch)
    output = io.StringIO()

    call_command("sync_linkedin_pending", stdout=output)

    assert json.loads(output.getvalue())["status"] == "planned"
    assert book.batches == []

    output = io.StringIO()
    call_command("sync_linkedin_pending", "--apply", stdout=output)
    assert json.loads(output.getvalue())["verified"] is True
