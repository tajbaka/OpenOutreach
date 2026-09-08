import copy

import pytest

from linkedin.exceptions import GeneralICPMessageError
from linkedin.general_icp_messages import GENERAL_ICP_MESSAGES_HEADERS
from linkedin.general_icp_review_status import (
    compare_general_icp_review_status,
    empty_general_icp_review_status,
    mark_general_icp_fields_reviewed,
    message_cell_hash,
    select_general_icp_labels,
    validate_general_icp_review_status,
)


def _row(*, intent="Direct agency", connect="Connect"):
    return [
        f"Founder/CEO | Small | 20x Initial Implementation | {intent}",
        connect,
        "Followup 1",
        "Subject 1",
        "Body 1",
        "Followup 2",
        "Subject 2",
        "Body 2",
    ]


def _rows(*message_rows):
    return [list(GENERAL_ICP_MESSAGES_HEADERS), *message_rows]


def _all_intent_rows():
    return _rows(*[
        _row(intent=intent)
        for intent in ("Direct agency", "CSP ecosystem", "Both", "Unclear")
    ])


def test_cell_hash_normalizes_sheet_line_endings_and_outer_space():
    assert message_cell_hash("  one\r\ntwo  ") == message_cell_hash("one\ntwo")


def test_cohort_selection_resolves_all_revenue_intents():
    labels = select_general_icp_labels(
        _all_intent_rows(),
        cohorts=["Founder/CEO | Small | 20x Initial Implementation"],
    )
    assert len(labels) == 4
    assert labels[0].endswith("Both")


def test_cohort_selection_fails_closed_when_an_intent_is_missing():
    with pytest.raises(GeneralICPMessageError, match="all four revenue intents"):
        select_general_icp_labels(
            _rows(_row()),
            cohorts=["Founder/CEO | Small | 20x Initial Implementation"],
        )


def test_review_hash_survives_row_reordering_and_detects_copy_change():
    rows = _all_intent_rows()
    label = rows[1][0]
    ledger = mark_general_icp_fields_reviewed(
        rows,
        empty_general_icp_review_status(),
        icp_labels=[label],
        field_keys=["connect_message"],
        reviewed_at="2026-09-03T12:00:00-04:00",
        reviewed_by="Arian",
        changed=True,
        modified_date="2026-09-03",
    )
    reordered = [rows[0], *reversed(rows[1:])]
    comparison = compare_general_icp_review_status(reordered, ledger)
    assert comparison["current_field_count"] == 1
    assert comparison["stale_field_count"] == 0

    changed_rows = copy.deepcopy(reordered)
    changed_rows[-1][1] = "Different connection copy"
    comparison = compare_general_icp_review_status(changed_rows, ledger)
    assert comparison["current_field_count"] == 0
    assert comparison["stale_field_count"] == 1


def test_re_review_without_edit_preserves_known_modification_date():
    rows = _rows(_row())
    label = rows[1][0]
    ledger = mark_general_icp_fields_reviewed(
        rows,
        empty_general_icp_review_status(),
        icp_labels=[label],
        field_keys=["connect_message"],
        reviewed_at="2026-09-03T12:00:00-04:00",
        reviewed_by="Arian",
        changed=True,
        modified_date="2026-09-03",
    )
    ledger = mark_general_icp_fields_reviewed(
        rows,
        ledger,
        icp_labels=[label],
        field_keys=["connect_message"],
        reviewed_at="2026-09-04T12:00:00-04:00",
        reviewed_by="Chuka",
        changed=False,
    )
    entry = ledger["rows"][label]["connect_message"]
    assert entry["change_kind"] == "reviewed_unchanged"
    assert entry["last_modified_date"] == "2026-09-03"
    assert entry["reviewed_by"] == "Chuka"


def test_edited_entry_requires_a_modification_timestamp_or_date():
    with pytest.raises(GeneralICPMessageError, match="require modified_at or modified_date"):
        mark_general_icp_fields_reviewed(
            _rows(_row()),
            empty_general_icp_review_status(),
            icp_labels=[_row()[0]],
            field_keys=["connect_message"],
            reviewed_at="2026-09-03T12:00:00-04:00",
            reviewed_by="Arian",
            changed=True,
        )


def test_unchanged_review_rejects_a_new_modification_date():
    with pytest.raises(GeneralICPMessageError, match="require changed=True"):
        mark_general_icp_fields_reviewed(
            _rows(_row()),
            empty_general_icp_review_status(),
            icp_labels=[_row()[0]],
            field_keys=["connect_message"],
            reviewed_at="2026-09-03T12:00:00-04:00",
            reviewed_by="Arian",
            changed=False,
            modified_date="2026-09-03",
        )


def test_modification_date_must_be_a_real_calendar_date():
    with pytest.raises(GeneralICPMessageError, match="must use YYYY-MM-DD"):
        mark_general_icp_fields_reviewed(
            _rows(_row()),
            empty_general_icp_review_status(),
            icp_labels=[_row()[0]],
            field_keys=["connect_message"],
            reviewed_at="2026-09-03T12:00:00-04:00",
            reviewed_by="Arian",
            changed=True,
            modified_date="2026-99-99",
        )


def test_ledger_rejects_unknown_message_fields():
    payload = empty_general_icp_review_status()
    payload["rows"] = {
        _row()[0]: {
            "unknown": {
                "change_kind": "edited",
                "content_sha256": "0" * 64,
                "last_modified_at": "2026-09-03T12:00:00-04:00",
                "reviewed_at": "2026-09-03T12:00:00-04:00",
                "reviewed_by": "Arian",
            }
        }
    }
    with pytest.raises(GeneralICPMessageError, match="unknown field"):
        validate_general_icp_review_status(payload)
