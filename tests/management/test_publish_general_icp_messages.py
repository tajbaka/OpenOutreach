import csv
import io
import json

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from linkedin.general_icp_messages import GENERAL_ICP_MESSAGES_HEADERS, parse_general_icp_message_rows
from linkedin.icp_outbound import CSP_ROLE_PERSONA_AUTHORING_BUCKETS
from linkedin.models import MessageProgram, MessageProgramVersion


def _general_rows(*, body="Hi {first_name}"):
    values = {
        "ICP": "Founder/CEO | Small | Rev5 Ready → 20x | Direct agency",
        "Connect Message": body,
        "Followup Message 1": "Follow up",
        "Email Subject 1": "Subject",
        "Email Body 1": "Email body",
        "Followup Message 2": "Follow up 2",
        "Email Subject 2": "Subject 2",
        "Email Body 2": "Email body 2",
    }
    return [list(GENERAL_ICP_MESSAGES_HEADERS), [values[h] for h in GENERAL_ICP_MESSAGES_HEADERS]]


def _drafts(*, body="Hi {first_name}"):
    return parse_general_icp_message_rows(_general_rows(body=body))


@pytest.mark.django_db
def test_publish_command_reads_json_and_previews_without_db_writes(monkeypatch):
    monkeypatch.setattr("linkedin.general_icp_json.load_general_message_programs", _drafts)
    output = io.StringIO()
    call_command("publish_general_icp_messages", stdout=output)
    payload = json.loads(output.getvalue().split("\nNo database changes", 1)[0])
    assert payload["status"] == "preview"
    assert payload["program_count"] == 1
    assert payload["changes"][0]["action"] == "create_program_and_version"
    assert MessageProgram.objects.count() == 0


@pytest.mark.django_db
def test_apply_requires_reviewer_before_reading_json(monkeypatch):
    reads = []
    monkeypatch.setattr(
        "linkedin.general_icp_json.load_general_message_programs",
        lambda: reads.append(True),
    )
    with pytest.raises(CommandError, match="--published-by is required"):
        call_command("publish_general_icp_messages", apply=True)
    assert reads == []


@pytest.mark.django_db(transaction=True)
def test_apply_creates_and_deduplicates_json_snapshot_versions(monkeypatch):
    current = [_drafts()]
    monkeypatch.setattr(
        "linkedin.general_icp_json.load_general_message_programs",
        lambda: current[0],
    )
    call_command("publish_general_icp_messages", apply=True, published_by="Arian")
    program = MessageProgram.objects.get(key="fedramp-marketplace-csp")
    assert program.versions.get(version=1).published_by == "Arian"
    call_command("publish_general_icp_messages", apply=True, published_by="Arian")
    assert program.versions.count() == 1
    current[0] = _drafts(body="Updated {first_name}")
    call_command("publish_general_icp_messages", apply=True, published_by="Chuka")
    assert program.versions.count() == 2
    assert program.versions.get(version=2).based_on_version_id == program.versions.get(version=1).pk


def _legacy_rows(connect="Connect"):
    return [
        ["ICP", "Connect Message", "Followup Message 1", "Email Subject 1", "Email Body 1"],
        [CSP_ROLE_PERSONA_AUTHORING_BUCKETS[0], connect, "Follow up", "Subject", "Email body"],
    ]


def test_render_command_outputs_one_wide_row_per_icp(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "linkedin.notifications.sheets.read_icp_messages_tab",
        lambda _sender: _legacy_rows(),
    )
    output_path = tmp_path / "general.csv"
    call_command("render_general_icp_messages", output=str(output_path), stdout=io.StringIO())
    with output_path.open(newline="", encoding="utf-8") as file_handle:
        rows = list(csv.reader(file_handle))
    assert rows[0] == list(GENERAL_ICP_MESSAGES_HEADERS)
    assert len(rows) == 2
    assert rows[1][0] == "Founder/CEO | Small | Stage needs review | Unclear"


def test_sync_general_is_preview_only_until_apply(monkeypatch):
    monkeypatch.setattr(
        "linkedin.general_icp_messages_sheet.read_general_icp_messages_tab",
        _general_rows,
    )
    monkeypatch.setattr(
        "linkedin.general_icp_json.render_general_icp_stores",
        lambda _drafts: ({"linkedin": True}, {"gmail": True}),
    )
    writes = []
    monkeypatch.setattr(
        "linkedin.general_icp_json.write_general_icp_stores",
        lambda *stores: writes.append(stores),
    )
    call_command("sync_general_icp_messages", stdout=io.StringIO())
    assert writes == []
    call_command("sync_general_icp_messages", apply=True, stdout=io.StringIO())
    assert writes == [({"linkedin": True}, {"gmail": True})]
