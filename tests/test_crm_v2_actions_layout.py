from __future__ import annotations

from datetime import date

from linkedin.crm_v2_publish import ActionRecord
from linkedin.crm_v2_view_builder import _action_sort_key
from linkedin.notifications import crm_v2_sheets as sheet
from linkedin.notifications.crm_v2_actions_layout import (
    _DUE_TODAY_COLOR,
    _OVERDUE_COLOR,
    build_action_presentation_requests,
)


class _Worksheet:
    id = 321

    def __init__(self, rows):
        self.rows = rows

    def get_all_values(self):
        return [list(row) for row in self.rows]


def _action(action_id, account, why_now, due):
    return {
        sheet.COL_ACTION_ID: action_id,
        sheet.COL_ACCOUNT: account,
        sheet.COL_WHY_NOW: why_now,
        sheet.COL_NEXT_ACTION_DUE: due,
        sheet.COL_OUTREACH: "Allowed",
    }


def test_action_sort_prioritizes_new_human_reply_before_older_followup_timer():
    new_reply = ActionRecord(
        action_id="reply",
        opportunity_id="opp-reply",
        account_id="acct-reply",
        account="Reply Account",
        owner="Arian",
        why_now="New human reply",
        next_action="Respond",
        next_action_due=date(2026, 9, 22),
    )
    elapsed = ActionRecord(
        action_id="elapsed",
        opportunity_id="opp-elapsed",
        account_id="acct-elapsed",
        account="Elapsed Account",
        owner="Arian",
        why_now="Reply window elapsed",
        next_action="Follow up",
        next_action_due=date(2026, 9, 19),
    )

    assert sorted((elapsed, new_reply), key=_action_sort_key) == [new_reply, elapsed]


def test_action_presentation_moves_rows_into_urgency_order_and_bands_groups():
    headers = list(sheet.ACTION_HEADERS)
    current = [
        _action("elapsed-1", "Ramp", "Reply window elapsed", "2026-09-20"),
        _action("reply-1", "Cadra", "New human reply", "2026-09-22"),
        _action("reply-2", "Schellman", "New human reply", "2026-09-22"),
        _action("elapsed-2", "DataLock", "Reply window elapsed", "2026-09-19"),
    ]
    values = [headers]
    values.extend([[row.get(header, "") for header in headers] for row in current])
    desired = [current[1], current[2], current[3], current[0]]

    requests, groups, moves = build_action_presentation_requests(
        _Worksheet(values),
        desired_rows=desired,
        today=date(2026, 9, 22),
    )

    assert groups == 2
    assert moves == 3
    assert all("moveDimension" in request for request in requests[:moves])
    row_requests = [request["repeatCell"] for request in requests[moves:]]
    group_starts = [
        request
        for request in row_requests
        if request["range"]["startColumnIndex"] == 0
        and request["cell"]["userEnteredFormat"]["borders"]["top"]["style"]
        == "SOLID_MEDIUM"
    ]
    assert [request["range"]["startRowIndex"] for request in group_starts] == [1, 3]

    due_index = headers.index(sheet.COL_NEXT_ACTION_DUE)
    due_formats = {
        request["range"]["startRowIndex"]: request["cell"]["userEnteredFormat"]["backgroundColor"]
        for request in row_requests
        if request["range"]["startColumnIndex"] == due_index
    }
    assert due_formats == {
        1: _DUE_TODAY_COLOR,
        2: _DUE_TODAY_COLOR,
        3: _OVERDUE_COLOR,
        4: _OVERDUE_COLOR,
    }
