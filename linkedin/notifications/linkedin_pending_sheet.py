"""Incrementally reconcile the owned ``LinkedIn Pending`` Sheet tab."""
from __future__ import annotations

from typing import Any

from gspread.utils import rowcol_to_a1

from linkedin.exceptions import SheetsError
from linkedin.linkedin_pending import LinkedInPending, build_linkedin_pending
from linkedin.notifications.crm_sheets import retry_sheet_read


TAB_NAME = "LinkedIn Pending"
HEADERS = (
    "Received (Toronto)",
    "Owner",
    "Person",
    "Company",
    "LinkedIn URL",
    "Message",
    "Days waiting",
    "Pending ID",
    "Lead ID",
    "Thread ID",
    "Message ID",
)
PENDING_ID_INDEX = HEADERS.index("Pending ID")
TECHNICAL_START_INDEX = PENDING_ID_INDEX
_HEADER_COLOR = {"red": 0.12, "green": 0.20, "blue": 0.34}
_URGENT_ROW_COLOR = {"red": 1.0, "green": 0.93, "blue": 0.91}
_ATTENTION_ROW_COLOR = {"red": 1.0, "green": 0.98, "blue": 0.88}
_FRESH_ROW_COLOR = {"red": 0.93, "green": 0.96, "blue": 1.0}
_URGENT_CELL_COLOR = {"red": 0.72, "green": 0.12, "blue": 0.12}
_ATTENTION_CELL_COLOR = {"red": 0.88, "green": 0.55, "blue": 0.08}
_FRESH_CELL_COLOR = {"red": 0.22, "green": 0.45, "blue": 0.75}
_WHITE = {"red": 1.0, "green": 1.0, "blue": 1.0}


def sync_linkedin_pending(*, dry_run: bool = True, now=None) -> dict:
    """Append, update, and delete stable pending rows without a tab rebuild."""
    from django.utils import timezone
    from linkedin import conf
    from linkedin.notifications import sheets

    observed_at = now or timezone.now()
    spreadsheet = sheets._gspread_client()
    if not conf.GOOGLE_SHEETS_ID or str(spreadsheet.id) != str(conf.GOOGLE_SHEETS_ID):
        raise SheetsError("LinkedIn Pending publisher opened an unexpected workbook")

    pending, counts = build_linkedin_pending(now=observed_at)
    metadata, existing = _inventory(spreadsheet)
    before = _read_rows(spreadsheet, existing)
    existing_rows = before[1:] if before else []
    desired_by_id = {
        row.pending_id: row.sheet_values(now=observed_at)
        for row in pending
    }
    existing_by_id: dict[str, tuple[int, list[Any]]] = {}
    for row_number, row in enumerate(existing_rows, start=2):
        pending_id = str(row[PENDING_ID_INDEX] or "").strip()
        if not pending_id:
            raise SheetsError(
                f"LinkedIn Pending row {row_number} is missing its stable Pending ID"
            )
        if pending_id in existing_by_id:
            raise SheetsError("LinkedIn Pending contains duplicate Pending IDs")
        existing_by_id[pending_id] = (row_number, row)

    deleted_ids = [key for key in existing_by_id if key not in desired_by_id]
    retained_ids = [key for key in existing_by_id if key in desired_by_id]
    appended_ids = [row.pending_id for row in pending if row.pending_id not in existing_by_id]
    # The builder returns oldest-first urgency order.  Existing rows are moved
    # in place to that order; their content is never rewritten wholesale.
    final_ids = [row.pending_id for row in pending]
    final_rows = [desired_by_id[key] for key in final_ids]
    updated_ids = [
        key for key in retained_ids
        if existing_by_id[key][1] != desired_by_id[key]
    ]
    unchanged = len(retained_ids) - len(updated_ids)

    report = {
        "status": "planned" if dry_run else "published",
        "created": existing is None,
        "rows_before": len(existing_rows),
        "rows": len(final_rows),
        "appended": len(appended_ids),
        "updated": len(updated_ids),
        "deleted": len(deleted_ids),
        "unchanged": unchanged,
        "sends_performed": 0,
        **counts,
    }
    if dry_run:
        return report

    if existing is None:
        _create_tab(
            spreadsheet,
            metadata=metadata,
            rows=final_rows,
        )
    else:
        _reconcile_existing(
            spreadsheet,
            sheet=existing,
            existing_by_id=existing_by_id,
            deleted_ids=deleted_ids,
            updated_ids=updated_ids,
            appended_ids=appended_ids,
            final_ids=final_ids,
            desired_by_id=desired_by_id,
        )

    _, published = _inventory(spreadsheet)
    readback = _read_rows(spreadsheet, published)
    expected = [list(HEADERS), *final_rows]
    if readback != expected:
        raise SheetsError("LinkedIn Pending Sheet readback did not match planned rows")
    report["verified"] = True
    report["sheet_id"] = published["properties"]["sheetId"]
    return report


def _inventory(spreadsheet):
    metadata = retry_sheet_read(
        lambda: spreadsheet.fetch_sheet_metadata(params={
            "fields": (
                "spreadsheetId,sheets(properties,basicFilter,merges,tables,"
                "conditionalFormats)"
            ),
        }),
        context="LinkedIn Pending tab inventory",
    )
    matches = [
        sheet for sheet in metadata.get("sheets", [])
        if sheet["properties"]["title"] == TAB_NAME
    ]
    if len(matches) > 1:
        raise SheetsError("Duplicate LinkedIn Pending tabs")
    return metadata, matches[0] if matches else None


def _read_rows(spreadsheet, sheet) -> list[list[Any]]:
    if sheet is None:
        return []
    if sheet.get("merges") or sheet.get("tables"):
        raise SheetsError("LinkedIn Pending has unexpected merged cells or tables")
    grid = sheet["properties"]["gridProperties"]
    columns = grid["columnCount"]
    if columns < len(HEADERS):
        raise SheetsError("LinkedIn Pending has too few columns")
    end_column = rowcol_to_a1(1, columns).rstrip("1")
    rows: list[list[Any]] = []
    chunk_size = max(1, 25000 // columns)
    for start in range(1, grid["rowCount"] + 1, chunk_size):
        end = min(start + chunk_size - 1, grid["rowCount"])
        metadata = retry_sheet_read(
            lambda start=start, end=end: spreadsheet.fetch_sheet_metadata(params={
                "ranges": f"'{TAB_NAME}'!A{start}:{end_column}{end}",
                "includeGridData": True,
                "fields": (
                    "sheets(data(startRow,rowData(values(userEnteredValue,"
                    "dataValidation,chipRuns))))"
                ),
            }),
            context="LinkedIn Pending native cells",
        )
        for part in metadata.get("sheets", []):
            for block in part.get("data", []):
                offset = block.get("startRow", 0)
                for index, native_row in enumerate(block.get("rowData", []), offset):
                    while len(rows) <= index:
                        rows.append([""] * len(HEADERS))
                    for column, cell in enumerate(native_row.get("values", [])):
                        entered = cell.get("userEnteredValue", {})
                        if (
                            cell.get("dataValidation")
                            or cell.get("chipRuns")
                            or "formulaValue" in entered
                        ):
                            raise SheetsError(
                                "LinkedIn Pending contains formulas, chips, or validation"
                            )
                        raw = entered.get(
                            "numberValue",
                            entered.get("stringValue", entered.get("boolValue", "")),
                        )
                        if column >= len(HEADERS):
                            if raw != "":
                                raise SheetsError(
                                    "LinkedIn Pending has populated extra columns"
                                )
                        else:
                            rows[index][column] = raw
    while rows and not any(value != "" for value in rows[-1]):
        rows.pop()
    if rows and tuple(str(value) for value in rows[0]) != HEADERS:
        raise SheetsError("LinkedIn Pending has an unexpected header")
    if rows:
        seen_blank = False
        for row in rows[1:]:
            material = any(value != "" for value in row)
            if not material:
                seen_blank = True
            elif seen_blank:
                raise SheetsError("LinkedIn Pending contains a blank row inside its data")
    return rows


def _create_tab(spreadsheet, *, metadata, rows) -> None:
    sheet_id = max(
        (sheet["properties"]["sheetId"] for sheet in metadata.get("sheets", [])),
        default=0,
    ) + 1
    desired = [list(HEADERS), *rows]
    grid_rows = max(100, len(desired) + 10)
    region = _region(sheet_id, 0, len(desired))
    requests = [{"addSheet": {"properties": {
        "sheetId": sheet_id,
        "title": TAB_NAME,
        "gridProperties": {
            "rowCount": grid_rows,
            "columnCount": len(HEADERS),
            "frozenRowCount": 1,
        },
    }}}, {
        "updateCells": {
            "range": region,
            "rows": [_native_row(values, data_index=index) for index, values in enumerate(desired)],
            "fields": "userEnteredValue,userEnteredFormat.numberFormat",
        },
    }, {
        "repeatCell": {
            "range": _region(sheet_id, 0, 1),
            "cell": {"userEnteredFormat": {
                "backgroundColor": _HEADER_COLOR,
                "textFormat": {"bold": True, "foregroundColor": {
                    "red": 1, "green": 1, "blue": 1,
                }},
                "horizontalAlignment": "CENTER",
                "verticalAlignment": "MIDDLE",
                "wrapStrategy": "WRAP",
            }},
            "fields": "userEnteredFormat",
        },
    }, {
        "repeatCell": {
            "range": _region(sheet_id, 1, max(2, len(desired))),
            "cell": {"userEnteredFormat": {
                "verticalAlignment": "TOP",
                "wrapStrategy": "WRAP",
            }},
            "fields": "userEnteredFormat.verticalAlignment,userEnteredFormat.wrapStrategy",
        },
    }, {
        "setBasicFilter": {"filter": {"range": region}},
    }, {
        "updateDimensionProperties": {
            "range": {
                "sheetId": sheet_id,
                "dimension": "COLUMNS",
                "startIndex": TECHNICAL_START_INDEX,
                "endIndex": len(HEADERS),
            },
            "properties": {"hiddenByUser": True},
            "fields": "hiddenByUser",
        },
    }]
    for column, width in enumerate((170, 100, 190, 190, 300, 500, 90, 220, 90, 220, 220)):
        requests.append({"updateDimensionProperties": {
            "range": {
                "sheetId": sheet_id,
                "dimension": "COLUMNS",
                "startIndex": column,
                "endIndex": column + 1,
            },
            "properties": {"pixelSize": width},
            "fields": "pixelSize",
        }})
    requests.extend(_conditional_format_requests(sheet_id, grid_rows))
    spreadsheet.batch_update({"requests": requests})


def _reconcile_existing(
    spreadsheet,
    *,
    sheet,
    existing_by_id,
    deleted_ids,
    updated_ids,
    appended_ids,
    final_ids,
    desired_by_id,
) -> None:
    sheet_id = sheet["properties"]["sheetId"]
    requests = []
    for pending_id in sorted(
        deleted_ids,
        key=lambda key: existing_by_id[key][0],
        reverse=True,
    ):
        row_number = existing_by_id[pending_id][0]
        requests.append({"deleteDimension": {"range": {
            "sheetId": sheet_id,
            "dimension": "ROWS",
            "startIndex": row_number - 1,
            "endIndex": row_number,
        }}})

    current_ids = [key for key in existing_by_id if key not in deleted_ids]
    current_position = {key: index + 1 for index, key in enumerate(current_ids)}
    for pending_id in updated_ids:
        row_index = current_position[pending_id]
        requests.append(_update_row_request(
            sheet_id,
            row_index,
            desired_by_id[pending_id],
        ))

    adjusted_grid_rows = sheet["properties"]["gridProperties"]["rowCount"] - len(deleted_ids)
    required_rows = len(final_ids) + 1
    if required_rows > adjusted_grid_rows:
        requests.append({"appendDimension": {
            "sheetId": sheet_id,
            "dimension": "ROWS",
            "length": required_rows - adjusted_grid_rows,
        }})
    for offset, pending_id in enumerate(appended_ids):
        row_index = len(current_ids) + offset + 1
        requests.append(_update_row_request(
            sheet_id,
            row_index,
            desired_by_id[pending_id],
        ))
        requests.append({"repeatCell": {
            "range": _region(sheet_id, row_index, row_index + 1),
            "cell": {"userEnteredFormat": {
                "verticalAlignment": "TOP",
                "wrapStrategy": "WRAP",
            }},
            "fields": "userEnteredFormat.verticalAlignment,userEnteredFormat.wrapStrategy",
        }})

    requests.extend(_row_move_requests(
        sheet_id=sheet_id,
        current_ids=current_ids + appended_ids,
        desired_ids=final_ids,
    ))

    basic_filter = {
        "range": _region(sheet_id, 0, max(1, required_rows)),
    }
    if sheet.get("basicFilter", {}).get("criteria"):
        basic_filter["criteria"] = sheet["basicFilter"]["criteria"]
    requests.append({"setBasicFilter": {"filter": basic_filter}})
    for index in range(len(sheet.get("conditionalFormats", [])) - 1, -1, -1):
        requests.append({"deleteConditionalFormatRule": {
            "sheetId": sheet_id,
            "index": index,
        }})
    presentation_rows = max(adjusted_grid_rows, required_rows)
    requests.extend(_conditional_format_requests(sheet_id, presentation_rows))
    spreadsheet.batch_update({"requests": requests})


def _update_row_request(sheet_id: int, row_index: int, values: list[Any]) -> dict:
    return {"updateCells": {
        "range": _region(sheet_id, row_index, row_index + 1),
        "rows": [_native_row(values, data_index=row_index)],
        "fields": "userEnteredValue,userEnteredFormat.numberFormat",
    }}


def _native_row(values: list[Any], *, data_index: int) -> dict:
    cells = []
    for column, value in enumerate(values):
        cell: dict[str, Any] = {}
        if value != "":
            kind = "numberValue" if isinstance(value, (int, float)) else "stringValue"
            cell["userEnteredValue"] = {kind: value}
        if data_index > 0 and column == 0:
            cell.setdefault("userEnteredFormat", {})["numberFormat"] = {
                "type": "DATE_TIME",
                "pattern": "mmm d, yyyy h:mm AM/PM",
            }
        if data_index > 0 and column == 6:
            cell.setdefault("userEnteredFormat", {})["numberFormat"] = {
                "type": "NUMBER",
                "pattern": "0",
            }
        cells.append(cell)
    return {"values": cells}


def _region(sheet_id: int, start_row: int, end_row: int) -> dict:
    return {
        "sheetId": sheet_id,
        "startRowIndex": start_row,
        "endRowIndex": end_row,
        "startColumnIndex": 0,
        "endColumnIndex": len(HEADERS),
    }


def _row_move_requests(
    *,
    sheet_id: int,
    current_ids: list[str],
    desired_ids: list[str],
) -> list[dict[str, Any]]:
    if len(current_ids) != len(set(current_ids)) or set(current_ids) != set(desired_ids):
        raise SheetsError("LinkedIn Pending could not resolve its urgency row order")
    working = list(current_ids)
    requests: list[dict[str, Any]] = []
    for desired_index, pending_id in enumerate(desired_ids):
        current_index = working.index(pending_id)
        if current_index == desired_index:
            continue
        requests.append({"moveDimension": {
            "source": {
                "sheetId": sheet_id,
                "dimension": "ROWS",
                "startIndex": current_index + 1,
                "endIndex": current_index + 2,
            },
            "destinationIndex": desired_index + 1,
        }})
        working.insert(desired_index, working.pop(current_index))
    return requests


def _conditional_format_requests(sheet_id: int, grid_rows: int) -> list[dict]:
    visible_range = {
        "sheetId": sheet_id,
        "startRowIndex": 1,
        "endRowIndex": grid_rows,
        "startColumnIndex": 0,
        "endColumnIndex": TECHNICAL_START_INDEX,
    }
    days_range = {
        "sheetId": sheet_id,
        "startRowIndex": 1,
        "endRowIndex": grid_rows,
        "startColumnIndex": 6,
        "endColumnIndex": 7,
    }
    rules = [
        (visible_range, "CUSTOM_FORMULA", "=AND($H2<>\"\",$G2>=7)", {
            "backgroundColor": _URGENT_ROW_COLOR,
        }),
        (visible_range, "CUSTOM_FORMULA", "=AND($H2<>\"\",$G2>=3,$G2<7)", {
            "backgroundColor": _ATTENTION_ROW_COLOR,
        }),
        (visible_range, "CUSTOM_FORMULA", "=AND($H2<>\"\",$G2<3)", {
            "backgroundColor": _FRESH_ROW_COLOR,
        }),
        (days_range, "NUMBER_GREATER_THAN_EQ", "7", {
            "backgroundColor": _URGENT_CELL_COLOR,
            "textFormat": {"bold": True, "foregroundColor": _WHITE},
        }),
        (days_range, "CUSTOM_FORMULA", "=AND($G2>=3,$G2<7)", {
            "backgroundColor": _ATTENTION_CELL_COLOR,
            "textFormat": {"bold": True, "foregroundColor": _WHITE},
        }),
        (days_range, "CUSTOM_FORMULA", "=AND($H2<>\"\",$G2<3)", {
            "backgroundColor": _FRESH_CELL_COLOR,
            "textFormat": {"bold": True, "foregroundColor": _WHITE},
        }),
    ]
    return [
        {"addConditionalFormatRule": {"index": 0, "rule": {
            "ranges": [target_range],
            "booleanRule": {
                "condition": {"type": condition_type, "values": [
                    {"userEnteredValue": value},
                ]},
                "format": cell_format,
            },
        }}}
        for target_range, condition_type, value, cell_format in rules
    ]
