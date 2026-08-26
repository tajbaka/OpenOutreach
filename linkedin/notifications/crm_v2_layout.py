"""Restrained Google Sheets layout for the two CRM v2 work surfaces.

The data adapters own values and three-way merge semantics.  This module only
builds additive formatting/filter/validation requests for an already-populated
worksheet.  It never renames or deletes a tab; first-cutover title changes stay
in the orchestrator's separate atomic request.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from linkedin.exceptions import SheetsError
from linkedin.notifications import crm_v2_sheets


_HEADER_BACKGROUND = {"red": 0.12, "green": 0.20, "blue": 0.34}
_HEADER_FOREGROUND = {"red": 1.0, "green": 1.0, "blue": 1.0}

_WIDTHS = {
    crm_v2_sheets.COL_ACCOUNT: 190,
    crm_v2_sheets.COL_OWNER: 105,
    crm_v2_sheets.COL_STAGE: 120,
    crm_v2_sheets.COL_ATTENTION: 105,
    crm_v2_sheets.COL_WHY_ACTIVE: 210,
    crm_v2_sheets.COL_EVIDENCE_TIER: 125,
    crm_v2_sheets.COL_OUTREACH: 95,
    crm_v2_sheets.COL_LAST_MEANINGFUL_TOUCH: 135,
    crm_v2_sheets.COL_NEXT_ACTION: 245,
    crm_v2_sheets.COL_NEXT_ACTION_DUE: 120,
    crm_v2_sheets.COL_WAITING_UNTIL: 110,
    crm_v2_sheets.COL_WHO_OWES: 90,
    crm_v2_sheets.COL_KEY_CONTACTS: 220,
    crm_v2_sheets.COL_MANUAL_PIN: 90,
    crm_v2_sheets.COL_CONTACT: 170,
    crm_v2_sheets.COL_WHY_NOW: 170,
    crm_v2_sheets.COL_CHANNEL: 95,
    crm_v2_sheets.COL_DRAFT: 280,
    crm_v2_sheets.COL_HANDLED: 85,
    crm_v2_sheets.COL_DISPOSITION: 120,
}

_WRAPPED_FIELDS = frozenset({
    crm_v2_sheets.COL_ACCOUNT,
    crm_v2_sheets.COL_WHY_ACTIVE,
    crm_v2_sheets.COL_NEXT_ACTION,
    crm_v2_sheets.COL_KEY_CONTACTS,
    crm_v2_sheets.COL_CONTACT,
    crm_v2_sheets.COL_WHY_NOW,
    crm_v2_sheets.COL_DRAFT,
})


def build_layout_requests(
    worksheet: Any,
    *,
    headers: Sequence[str],
    technical_fields: Iterable[str],
    owner_values: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Return compact, bounded layout requests for one managed worksheet."""
    sheet_id = getattr(worksheet, "id", None)
    if sheet_id is None:
        raise SheetsError("CRM v2 layout requires a worksheet sheet ID")
    header_list = [str(value) for value in headers]
    if not header_list or len(header_list) != len(set(header_list)):
        raise SheetsError("CRM v2 layout requires unique nonempty headers")

    technical = frozenset(technical_fields)
    row_count = max(2, int(getattr(worksheet, "row_count", 1000) or 1000))
    column_count = len(header_list)
    requests: list[dict[str, Any]] = [
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": sheet_id,
                    "gridProperties": {"frozenRowCount": 1},
                },
                "fields": "gridProperties.frozenRowCount",
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
                        "backgroundColor": _HEADER_BACKGROUND,
                        "textFormat": {
                            "foregroundColor": _HEADER_FOREGROUND,
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
    ]

    for index, header in enumerate(header_list):
        dimension = {
            "sheetId": sheet_id,
            "dimension": "COLUMNS",
            "startIndex": index,
            "endIndex": index + 1,
        }
        properties: dict[str, Any] = {}
        fields: list[str] = []
        if header in technical:
            properties["hiddenByUser"] = True
            fields.append("hiddenByUser")
        else:
            properties["pixelSize"] = _WIDTHS.get(header, 120)
            fields.append("pixelSize")
        requests.append({
            "updateDimensionProperties": {
                "range": dimension,
                "properties": properties,
                "fields": ",".join(fields),
            }
        })
        if header in _WRAPPED_FIELDS:
            requests.append({
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": 1,
                        "endRowIndex": row_count,
                        "startColumnIndex": index,
                        "endColumnIndex": index + 1,
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
            })

    allowed_values = {
        crm_v2_sheets.COL_OWNER: tuple(sorted({
            " ".join(str(value or "").split())
            for value in owner_values
            if " ".join(str(value or "").split())
        }, key=str.casefold)),
        crm_v2_sheets.COL_ATTENTION: crm_v2_sheets.ATTENTION_VALUES,
        crm_v2_sheets.COL_OUTREACH: crm_v2_sheets.OUTREACH_VALUES,
    }
    for header, values in allowed_values.items():
        if header not in header_list or not values or len(values) > 500:
            continue
        index = header_list.index(header)
        requests.append({
            "setDataValidation": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 1,
                    "endRowIndex": row_count,
                    "startColumnIndex": index,
                    "endColumnIndex": index + 1,
                },
                "rule": {
                    "condition": {
                        "type": "ONE_OF_LIST",
                        "values": [
                            {"userEnteredValue": value}
                            for value in values
                        ],
                    },
                    "strict": True,
                    "showCustomUi": True,
                },
            }
        })
    return requests


def apply_layout(
    spreadsheet: Any,
    worksheet: Any,
    *,
    headers: Sequence[str],
    technical_fields: Iterable[str],
    owner_values: Iterable[str] = (),
) -> int:
    """Apply one bounded formatting batch and return its request count."""
    requests = build_layout_requests(
        worksheet,
        headers=headers,
        technical_fields=technical_fields,
        owner_values=owner_values,
    )
    try:
        spreadsheet.batch_update({"requests": requests})
    except Exception as exc:
        # Provider errors may embed workbook values or IDs.  Keep the surface
        # sanitized while retaining the original exception as the cause.
        raise SheetsError("CRM v2 layout write failed") from exc
    return len(requests)
