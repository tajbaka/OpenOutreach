import pytest
from gspread.exceptions import WorksheetNotFound

from linkedin.exceptions import GeneralICPMessageError
from linkedin.general_icp_messages import GENERAL_ICP_MESSAGES_HEADERS
from linkedin.general_icp_messages_sheet import (
    read_general_icp_messages_tab,
    stage_general_icp_messages_tab,
)


class _Worksheet:
    def __init__(self, values=None):
        self.values = values or []
        self.updates = []
        self.id = 7123
        self.row_count = 100

    def get_all_values(self):
        return self.values

    def update(self, **kwargs):
        self.updates.append(kwargs)

class _Spreadsheet:
    def __init__(self, worksheet=None):
        self.current = worksheet
        self.added = []
        self.batch_updates = []

    def worksheet(self, _title):
        if self.current is None:
            raise WorksheetNotFound("missing")
        return self.current

    def add_worksheet(self, **kwargs):
        self.added.append(kwargs)
        self.current = _Worksheet()
        return self.current

    def batch_update(self, payload):
        self.batch_updates.append(payload)


def _rows():
    return [
        list(GENERAL_ICP_MESSAGES_HEADERS),
        [
            "Founder/CEO | Small | Rev5 Ready → 20x | Direct agency",
            "Hello",
            "Follow up",
            "Subject 1",
            "Email body 1",
            "Follow up 2",
            "Subject 2",
            "Email body 2",
        ],
    ]


def test_read_general_tab_is_read_only(monkeypatch):
    worksheet = _Worksheet(_rows())
    spreadsheet = _Spreadsheet(worksheet)
    monkeypatch.setattr(
        "linkedin.notifications.sheets._gspread_client",
        lambda: spreadsheet,
    )

    assert read_general_icp_messages_tab() == _rows()
    assert worksheet.updates == []


def test_stage_creates_only_the_general_tab_when_missing(monkeypatch):
    spreadsheet = _Spreadsheet()
    monkeypatch.setattr(
        "linkedin.notifications.sheets._gspread_client",
        lambda: spreadsheet,
    )

    stage_general_icp_messages_tab(_rows())

    assert spreadsheet.added[0]["title"] == "General ICP Messages"
    assert spreadsheet.current.updates == [{
        "values": _rows(),
        "range_name": "A1",
        "value_input_option": "RAW",
    }]
    assert spreadsheet.batch_updates
    requests = spreadsheet.batch_updates[0]["requests"]
    assert requests[0]["updateSheetProperties"]["properties"] == {
        "sheetId": 7123,
        "gridProperties": {
            "frozenRowCount": 1,
            "frozenColumnCount": 1,
        },
    }


def test_stage_refuses_to_overwrite_existing_general_draft(monkeypatch):
    worksheet = _Worksheet([list(GENERAL_ICP_MESSAGES_HEADERS), ["human edit"]])
    monkeypatch.setattr(
        "linkedin.notifications.sheets._gspread_client",
        lambda: _Spreadsheet(worksheet),
    )

    with pytest.raises(GeneralICPMessageError, match="refusing to overwrite"):
        stage_general_icp_messages_tab(_rows())

    assert worksheet.updates == []
