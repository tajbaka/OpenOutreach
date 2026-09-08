"""Dedicated Google Sheets boundary for general ICP message authoring."""
from __future__ import annotations

from collections.abc import Sequence

from gspread.exceptions import APIError, SpreadsheetNotFound, WorksheetNotFound

from linkedin.exceptions import GeneralICPMessageError, SheetsError
from linkedin.general_icp_messages import (
    GENERAL_ICP_MESSAGES_HEADERS,
    GENERAL_ICP_MESSAGES_TAB,
)
from linkedin.notifications.crm_sheets import retry_sheet_read


_COLUMN_WIDTHS = (
    300,  # ICP
    420,  # Connect Message
    520,  # Followup Message 1
    220,  # Email Subject 1
    560,  # Email Body 1
    520,  # Followup Message 2
    220,  # Email Subject 2
    560,  # Email Body 2
)


def _general_tab_layout_requests(worksheet, *, used_rows: int) -> list[dict]:
    """Return the bounded, review-friendly layout for the authoring surface."""
    sheet_id = getattr(worksheet, "id", None)
    if sheet_id is None:
        raise SheetsError("General ICP Messages layout requires a worksheet sheet ID")
    column_count = len(GENERAL_ICP_MESSAGES_HEADERS)
    row_count = max(int(getattr(worksheet, "row_count", 100) or 100), used_rows, 2)
    requests = [
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": sheet_id,
                    "gridProperties": {
                        "frozenRowCount": 1,
                        "frozenColumnCount": 1,
                    },
                },
                "fields": (
                    "gridProperties.frozenRowCount,"
                    "gridProperties.frozenColumnCount"
                ),
            }
        },
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 0,
                    "endRowIndex": 1,
                    "startColumnIndex": 0,
                    "endColumnIndex": column_count,
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": {
                            "red": 31 / 255,
                            "green": 78 / 255,
                            "blue": 121 / 255,
                        },
                        "textFormat": {
                            "foregroundColor": {"red": 1, "green": 1, "blue": 1},
                            "bold": True,
                        },
                        "horizontalAlignment": "CENTER",
                        "verticalAlignment": "MIDDLE",
                        "wrapStrategy": "WRAP",
                    }
                },
                "fields": "userEnteredFormat",
            }
        },
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 1,
                    "endRowIndex": row_count,
                    "startColumnIndex": 0,
                    "endColumnIndex": column_count,
                },
                "cell": {
                    "userEnteredFormat": {
                        "verticalAlignment": "TOP",
                        "wrapStrategy": "WRAP",
                    }
                },
                "fields": (
                    "userEnteredFormat.verticalAlignment,"
                    "userEnteredFormat.wrapStrategy"
                ),
            }
        },
        {
            "setBasicFilter": {
                "filter": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": 0,
                        "endRowIndex": row_count,
                        "startColumnIndex": 0,
                        "endColumnIndex": column_count,
                    }
                }
            }
        },
        {
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "ROWS",
                    "startIndex": 0,
                    "endIndex": 1,
                },
                "properties": {"pixelSize": 38},
                "fields": "pixelSize",
            }
        },
    ]
    for index, width in enumerate(_COLUMN_WIDTHS):
        requests.append({
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "COLUMNS",
                    "startIndex": index,
                    "endIndex": index + 1,
                },
                "properties": {"pixelSize": width},
                "fields": "pixelSize",
            }
        })
    requests.append({
        "updateDimensionProperties": {
            "range": {
                "sheetId": sheet_id,
                "dimension": "COLUMNS",
                "startIndex": 0,
                "endIndex": 8,
            },
            "properties": {"hiddenByUser": False},
            "fields": "hiddenByUser",
        }
    })
    return requests


def read_general_icp_messages_tab() -> list[list[str]]:
    """Read the mutable general-message draft without changing Sheet state."""
    from linkedin.notifications.sheets import _gspread_client

    try:
        spreadsheet = _gspread_client()
    except (APIError, SpreadsheetNotFound, FileNotFoundError) as exc:
        raise SheetsError("failed opening configured ICP Messages workbook") from exc
    try:
        worksheet = retry_sheet_read(
            lambda: spreadsheet.worksheet(GENERAL_ICP_MESSAGES_TAB),
            context=f"open {GENERAL_ICP_MESSAGES_TAB!r}",
        )
    except WorksheetNotFound as exc:
        raise SheetsError(f"{GENERAL_ICP_MESSAGES_TAB} tab not found") from exc

    def _read():
        return worksheet.get_all_values()

    values = retry_sheet_read(
        _read,
        context=f"read {GENERAL_ICP_MESSAGES_TAB!r}",
    )
    return [list(row) for row in values]


def stage_general_icp_messages_tab(
    rows: Sequence[Sequence[str]],
) -> None:
    """Create and populate the general tab only when it is absent or empty.

    This is intentionally a one-time staging primitive.  It never clears or
    replaces an existing draft, so rerunning a migration cannot destroy human
    edits.  Existing sender-specific tabs are never touched.
    """
    if not rows or tuple(str(value).strip() for value in rows[0]) != GENERAL_ICP_MESSAGES_HEADERS:
        raise GeneralICPMessageError("staged rows must use the exact general-message headers")

    from linkedin.notifications.sheets import _gspread_client

    try:
        spreadsheet = _gspread_client()
    except (APIError, SpreadsheetNotFound, FileNotFoundError) as exc:
        raise SheetsError("failed opening configured ICP Messages workbook") from exc
    try:
        worksheet = spreadsheet.worksheet(GENERAL_ICP_MESSAGES_TAB)
        existing = worksheet.get_all_values()
        if any(str(cell).strip() for row in existing for cell in row):
            raise GeneralICPMessageError(
                f"{GENERAL_ICP_MESSAGES_TAB} already contains data; refusing to overwrite it"
            )
    except WorksheetNotFound:
        try:
            worksheet = spreadsheet.add_worksheet(
                title=GENERAL_ICP_MESSAGES_TAB,
                rows=max(len(rows) + 25, 100),
                cols=len(GENERAL_ICP_MESSAGES_HEADERS),
            )
        except APIError as exc:
            raise SheetsError(f"failed creating {GENERAL_ICP_MESSAGES_TAB}") from exc
    except APIError as exc:
        raise SheetsError(f"failed checking {GENERAL_ICP_MESSAGES_TAB}") from exc

    try:
        worksheet.update(
            values=[list(row) for row in rows],
            range_name="A1",
            value_input_option="RAW",
        )
        spreadsheet.batch_update({
            "requests": _general_tab_layout_requests(
                worksheet,
                used_rows=len(rows),
            ),
        })
    except APIError as exc:
        raise SheetsError(f"failed staging {GENERAL_ICP_MESSAGES_TAB}") from exc
