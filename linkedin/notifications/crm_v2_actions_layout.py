"""Urgency grouping and formatting requests for generated CRM Actions."""
from __future__ import annotations

from datetime import date
from typing import Any, Iterable, Mapping, Sequence

from django.utils import timezone

from linkedin.exceptions import SheetsError
from linkedin.notifications import crm_v2_sheets


_ROW_COLORS = {
    "reply": {"red": 0.93, "green": 0.96, "blue": 1.0},
    "overdue": {"red": 1.0, "green": 0.94, "blue": 0.90},
    "meeting": {"red": 0.95, "green": 0.93, "blue": 1.0},
    "today": {"red": 1.0, "green": 0.98, "blue": 0.89},
    "followup": {"red": 0.95, "green": 0.98, "blue": 0.94},
    "review": {"red": 0.96, "green": 0.96, "blue": 0.96},
}
_WHY_NOW_COLORS = {
    "reply": {"red": 0.70, "green": 0.82, "blue": 0.98},
    "overdue": {"red": 0.98, "green": 0.72, "blue": 0.62},
    "meeting": {"red": 0.80, "green": 0.74, "blue": 0.96},
    "today": {"red": 0.99, "green": 0.88, "blue": 0.52},
    "followup": {"red": 0.72, "green": 0.90, "blue": 0.70},
    "review": {"red": 0.84, "green": 0.84, "blue": 0.84},
}
_GROUP_BORDER = {"red": 0.48, "green": 0.55, "blue": 0.65}
_TEXT_COLOR = {"red": 0.12, "green": 0.15, "blue": 0.20}
_DUE_TODAY_COLOR = {"red": 0.99, "green": 0.88, "blue": 0.52}
_OVERDUE_COLOR = {"red": 0.98, "green": 0.68, "blue": 0.68}
_OVERDUE_TEXT = {"red": 0.55, "green": 0.06, "blue": 0.06}


def _material_rows(values: list[list[Any]]) -> list[list[str]]:
    rows = [[str(cell or "") for cell in row] for row in values]
    while rows and not any(rows[-1]):
        rows.pop()
    return rows


def _reason_group(reason: str) -> str:
    normalized = " ".join(str(reason or "").split()).casefold()
    if normalized == "new human reply":
        return "reply"
    if normalized in {"overdue next step", "reply window elapsed"}:
        return "overdue"
    if normalized in {"meeting approaching", "meeting completed"}:
        return "meeting"
    if normalized == "due today":
        return "today"
    if normalized in {"follow up", "follow-up"}:
        return "followup"
    return "review"


def _parse_date(value: object) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError as exc:
        raise SheetsError(f"Actions has an invalid due date: {text!r}") from exc


def _row_move_requests(
    *,
    sheet_id: int,
    current_ids: list[str],
    desired_ids: list[str],
) -> list[dict[str, Any]]:
    if len(desired_ids) != len(set(desired_ids)):
        raise SheetsError("Actions presentation received duplicate desired Action IDs")
    missing = [stable_id for stable_id in desired_ids if stable_id not in current_ids]
    if missing:
        raise SheetsError("Actions presentation could not resolve every desired Action ID")

    working = list(current_ids)
    requests: list[dict[str, Any]] = []
    for desired_index, stable_id in enumerate(desired_ids):
        current_index = working.index(stable_id)
        if current_index == desired_index:
            continue
        requests.append({
            "moveDimension": {
                "source": {
                    "sheetId": sheet_id,
                    "dimension": "ROWS",
                    "startIndex": current_index + 1,
                    "endIndex": current_index + 2,
                },
                "destinationIndex": desired_index + 1,
            },
        })
        moved = working.pop(current_index)
        working.insert(desired_index, moved)
    return requests


def build_action_presentation_requests(
    worksheet: Any,
    *,
    desired_rows: Iterable[Mapping[str, Any]],
    today: date | None = None,
) -> tuple[list[dict[str, Any]], int, int]:
    """Return row moves plus bounded cohort formatting for Actions."""
    values = _material_rows(worksheet.get_all_values())
    if not values:
        raise SheetsError("Actions staging sheet is empty")
    live_headers = tuple(str(value).strip() for value in values[0])
    if not all(live_headers) or len(live_headers) != len(set(live_headers)):
        raise SheetsError("Actions contains blank or duplicate headings")
    required = (
        crm_v2_sheets.COL_ACTION_ID,
        crm_v2_sheets.COL_WHY_NOW,
        crm_v2_sheets.COL_NEXT_ACTION_DUE,
        crm_v2_sheets.COL_OUTREACH,
    )
    missing_headers = [header for header in required if header not in live_headers]
    if missing_headers:
        raise SheetsError("Actions is missing presentation heading(s)")

    planned = [dict(row) for row in desired_rows]
    action_index = live_headers.index(crm_v2_sheets.COL_ACTION_ID)
    current_ids = [
        (
            row[action_index].strip()
            if action_index < len(row) and row[action_index].strip()
            else f"__row_{index}"
        )
        for index, row in enumerate(values[1:])
    ]
    desired_ids = [
        str(row.get(crm_v2_sheets.COL_ACTION_ID) or "").strip()
        for row in planned
    ]
    if any(not stable_id for stable_id in desired_ids):
        raise SheetsError("Actions presentation received a blank Action ID")

    sheet_id = int(worksheet.id)
    requests = _row_move_requests(
        sheet_id=sheet_id,
        current_ids=current_ids,
        desired_ids=desired_ids,
    )
    move_count = len(requests)
    why_index = live_headers.index(crm_v2_sheets.COL_WHY_NOW)
    due_index = live_headers.index(crm_v2_sheets.COL_NEXT_ACTION_DUE)
    outreach_index = live_headers.index(crm_v2_sheets.COL_OUTREACH)
    current_day = today or timezone.localdate()
    previous_group = ""
    group_count = 0

    for row_index, row in enumerate(planned, start=1):
        reason = str(row.get(crm_v2_sheets.COL_WHY_NOW) or "")
        group = _reason_group(reason)
        group_start = group != previous_group
        if group_start:
            group_count += 1
        previous_group = group
        requests.append({
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": row_index,
                    "endRowIndex": row_index + 1,
                    "startColumnIndex": 0,
                    "endColumnIndex": len(live_headers),
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": _ROW_COLORS[group],
                        "borders": {
                            "top": {
                                "style": "SOLID_MEDIUM" if group_start else "SOLID",
                                "color": _GROUP_BORDER,
                            },
                        },
                        "textFormat": {
                            "bold": False,
                            "foregroundColor": _TEXT_COLOR,
                        },
                    },
                },
                "fields": (
                    "userEnteredFormat.backgroundColor,"
                    "userEnteredFormat.borders.top,"
                    "userEnteredFormat.textFormat.bold,"
                    "userEnteredFormat.textFormat.foregroundColor"
                ),
            },
        })
        requests.append({
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": row_index,
                    "endRowIndex": row_index + 1,
                    "startColumnIndex": why_index,
                    "endColumnIndex": why_index + 1,
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": _WHY_NOW_COLORS[group],
                        "textFormat": {"bold": True, "foregroundColor": _TEXT_COLOR},
                    },
                },
                "fields": (
                    "userEnteredFormat.backgroundColor,"
                    "userEnteredFormat.textFormat.bold,"
                    "userEnteredFormat.textFormat.foregroundColor"
                ),
            },
        })
        due_on = _parse_date(row.get(crm_v2_sheets.COL_NEXT_ACTION_DUE))
        if due_on is not None and due_on <= current_day:
            overdue = due_on < current_day
            requests.append({
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": row_index,
                        "endRowIndex": row_index + 1,
                        "startColumnIndex": due_index,
                        "endColumnIndex": due_index + 1,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": _OVERDUE_COLOR if overdue else _DUE_TODAY_COLOR,
                            "textFormat": {
                                "bold": True,
                                "foregroundColor": _OVERDUE_TEXT if overdue else _TEXT_COLOR,
                            },
                        },
                    },
                    "fields": (
                        "userEnteredFormat.backgroundColor,"
                        "userEnteredFormat.textFormat.bold,"
                        "userEnteredFormat.textFormat.foregroundColor"
                    ),
                },
            })
        if str(row.get(crm_v2_sheets.COL_OUTREACH) or "").strip() == "Stopped":
            requests.append({
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": row_index,
                        "endRowIndex": row_index + 1,
                        "startColumnIndex": outreach_index,
                        "endColumnIndex": outreach_index + 1,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": _OVERDUE_COLOR,
                            "textFormat": {"bold": True, "foregroundColor": _OVERDUE_TEXT},
                        },
                    },
                    "fields": (
                        "userEnteredFormat.backgroundColor,"
                        "userEnteredFormat.textFormat.bold,"
                        "userEnteredFormat.textFormat.foregroundColor"
                    ),
                },
            })
    return requests, group_count, move_count
