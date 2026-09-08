import io
from datetime import datetime, timezone

from django.core.management import call_command

from linkedin.general_icp_messages import GENERAL_ICP_MESSAGES_HEADERS
from linkedin.general_icp_review_status import empty_general_icp_review_status


def _rows():
    rows = [list(GENERAL_ICP_MESSAGES_HEADERS)]
    for intent in ("Direct agency", "CSP ecosystem", "Both", "Unclear"):
        rows.append([
            f"Founder/CEO | Small | 20x Initial Implementation | {intent}",
            "Connect",
            "Followup 1",
            "Subject 1",
            "Body 1",
            "Followup 2",
            "Subject 2",
            "Body 2",
        ])
    return rows


def test_live_edit_uses_review_time_as_exact_modification_time(monkeypatch):
    captured = {}
    now = datetime(2026, 9, 3, 17, 45, tzinfo=timezone.utc)
    monkeypatch.setattr(
        "linkedin.general_icp_messages_sheet.read_general_icp_messages_tab",
        _rows,
    )
    monkeypatch.setattr(
        "linkedin.general_icp_review_status.load_general_icp_review_status",
        empty_general_icp_review_status,
    )
    monkeypatch.setattr(
        "linkedin.general_icp_review_status.write_general_icp_review_status",
        lambda payload: captured.setdefault("payload", payload),
    )
    monkeypatch.setattr(
        "linkedin.management.commands.review_general_icp_messages.timezone.localtime",
        lambda: now,
    )

    call_command(
        "review_general_icp_messages",
        cohort=["Founder/CEO | Small | 20x Initial Implementation"],
        field=["connect_message"],
        reviewed_by="Arian",
        changed=True,
        apply=True,
        stdout=io.StringIO(),
    )

    entries = [
        fields["connect_message"]
        for fields in captured["payload"]["rows"].values()
    ]
    assert len(entries) == 4
    assert {entry["last_modified_at"] for entry in entries} == {
        "2026-09-03T17:45:00+00:00"
    }
    assert {entry["reviewed_at"] for entry in entries} == {
        "2026-09-03T17:45:00+00:00"
    }
