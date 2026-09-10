"""Publish only the rebuildable five-column accepted/no-reply Sheet view."""
from __future__ import annotations

from linkedin.accepted_connections import build_accepted_connections
from linkedin.exceptions import SheetsError
from linkedin.notifications.crm_sheets import retry_sheet_read


TAB_NAME = "Accepted — Awaiting Reply"
HEADERS = ("Connection recorded (Toronto)", "Sender", "Name", "Company", "LinkedIn URL")


def _inventory(spreadsheet):
    metadata = retry_sheet_read(
        lambda: spreadsheet.fetch_sheet_metadata(params={
            "fields": "spreadsheetId,sheets(properties,basicFilter,merges,tables)",
        }), context="accepted-connections tab inventory",
    )
    matches = [sheet for sheet in metadata.get("sheets", [])
               if sheet["properties"]["title"] == TAB_NAME]
    if len(matches) > 1:
        raise SheetsError("Duplicate accepted-connections tabs")
    return metadata, matches[0] if matches else None


def _read_rows(spreadsheet, sheet):
    """Inspect native cells in bounded chunks; never overwrite another schema."""
    if sheet is None:
        return []
    if sheet.get("merges") or sheet.get("tables"):
        raise SheetsError("Accepted-connections tab has unexpected merged cells or tables")
    grid = sheet["properties"]["gridProperties"]
    columns = grid["columnCount"]
    if columns < len(HEADERS):
        raise SheetsError("Accepted-connections tab has too few columns")
    # Reject populated extra columns because sorting only A:E would detach
    # user notes from their people. Empty extra columns are harmless.
    from gspread.utils import rowcol_to_a1
    end_column = rowcol_to_a1(1, columns).rstrip("1")
    rows = []
    chunk_size = max(1, 25000 // columns)
    for start in range(1, grid["rowCount"] + 1, chunk_size):
        end = min(start + chunk_size - 1, grid["rowCount"])
        metadata = retry_sheet_read(
            lambda: spreadsheet.fetch_sheet_metadata(params={
                "ranges": f"'{TAB_NAME}'!A{start}:{end_column}{end}",
                "includeGridData": True,
                "fields": "sheets(data(startRow,rowData(values(userEnteredValue,dataValidation,chipRuns))))",
            }), context="accepted-connections native cells",
        )
        for part in metadata.get("sheets", []):
            for block in part.get("data", []):
                offset = block.get("startRow", 0)
                for index, native_row in enumerate(block.get("rowData", []), offset):
                    while len(rows) <= index:
                        rows.append([""] * len(HEADERS))
                    cells = native_row.get("values", [])
                    for col, cell in enumerate(cells):
                        value = cell.get("userEnteredValue", {})
                        if cell.get("dataValidation") or cell.get("chipRuns") or "formulaValue" in value:
                            raise SheetsError("Accepted-connections tab contains user formulas, chips or validation")
                        raw = value.get("numberValue", value.get("stringValue", value.get("boolValue", "")))
                        if col >= len(HEADERS):
                            if raw != "":
                                raise SheetsError("Accepted-connections tab has populated extra columns")
                        else:
                            rows[index][col] = raw
    while rows and not any(value != "" for value in rows[-1]):
        rows.pop()
    if rows and tuple(rows[0]) != HEADERS:
        raise SheetsError("Accepted-connections tab has an unexpected header; refusing to overwrite it")
    return rows


def sync_accepted_connections(*, dry_run: bool = True) -> dict:
    """Called under the CRM refresh lock; only this derived tab is writable."""
    from linkedin import conf
    from linkedin.notifications import sheets

    spreadsheet = sheets._gspread_client()
    if not conf.GOOGLE_SHEETS_ID or str(spreadsheet.id) != str(conf.GOOGLE_SHEETS_ID):
        raise SheetsError("Accepted-connections publisher opened an unexpected workbook")
    rows, counts = build_accepted_connections()
    desired = [list(HEADERS)] + [row.sheet_values() for row in rows]
    metadata, existing = _inventory(spreadsheet)
    before = _read_rows(spreadsheet, existing)
    report = {"status": "planned" if dry_run else "published", "rows": len(rows),
              "rows_before": max(0, len(before) - 1), "changed": before != desired,
              "created": existing is None, "sends_performed": 0, **counts}
    if dry_run:
        return report

    requests = []
    if existing is None:
        sheet_id = max((s["properties"]["sheetId"] for s in metadata.get("sheets", [])), default=0) + 1
        # Creating and filling the new tab happens in one atomic API batch.
        grid_rows = max(100, len(desired))
        requests.append({"addSheet": {"properties": {
            "sheetId": sheet_id, "title": TAB_NAME,
            "gridProperties": {"rowCount": grid_rows, "columnCount": len(HEADERS), "frozenRowCount": 1},
        }}})
    else:
        sheet_id = existing["properties"]["sheetId"]
        grid_rows = existing["properties"]["gridProperties"]["rowCount"]
        if len(desired) > grid_rows:
            requests.append({"appendDimension": {
                "sheetId": sheet_id, "dimension": "ROWS", "length": len(desired) - grid_rows,
            }})

    def region(start, end):
        return {"sheetId": sheet_id, "startRowIndex": start, "endRowIndex": end,
                "startColumnIndex": 0, "endColumnIndex": len(HEADERS)}

    if before != desired:
        native_rows = []
        for index in range(max(len(before), len(desired))):
            values = desired[index] if index < len(desired) else [""] * len(HEADERS)
            cells = []
            for col, value in enumerate(values):
                cell = {}
                if value != "":
                    cell["userEnteredValue"] = {"numberValue" if isinstance(value, (int, float)) else "stringValue": value}
                if col == 0 and 0 < index < len(desired):
                    cell["userEnteredFormat"] = {"numberFormat": {"type": "DATE_TIME", "pattern": rows[index - 1].date_format}}
                cells.append(cell)
            native_rows.append({"values": cells})
        requests.append({"updateCells": {"range": region(0, len(native_rows)), "rows": native_rows,
            "fields": "userEnteredValue,userEnteredFormat.numberFormat"}})
        basic_filter = {"range": region(0, len(desired))}
        if existing and existing.get("basicFilter", {}).get("criteria"):
            basic_filter["criteria"] = existing["basicFilter"]["criteria"]
        requests.append({"setBasicFilter": {"filter": basic_filter}})
    if existing is None:
        requests.append({"repeatCell": {"range": region(0, 1), "cell": {"userEnteredFormat": {
            "backgroundColor": {"red": 0.93, "green": 0.93, "blue": 0.93},
            "textFormat": {"bold": True}, "wrapStrategy": "WRAP",
        }}, "fields": "userEnteredFormat"}})
        for col, width in enumerate((250, 90, 220, 250, 390)):
            requests.append({"updateDimensionProperties": {"range": {
                "sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": col, "endIndex": col + 1,
            }, "properties": {"pixelSize": width}, "fields": "pixelSize"}})
    if requests:
        spreadsheet.batch_update({"requests": requests})
    _, published = _inventory(spreadsheet)
    if published is None or _read_rows(spreadsheet, published) != desired:
        raise SheetsError("Accepted-connections Sheet readback did not match the planned rows")
    report["verified"] = True
    return report
