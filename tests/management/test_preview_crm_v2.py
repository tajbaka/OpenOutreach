import io
import json

import pytest
from django.core.management import call_command
from django.utils import timezone

from crm.models import Lead, Message


pytestmark = pytest.mark.django_db


def test_preview_is_private_and_includes_manual_dno_account(tmp_path):
    lead = Lead.objects.create(
        company_name="StackArmor",
        email="contact@stackarmor.com",
        disqualified=True,
    )
    output = tmp_path / "preview.json"

    call_command(
        "preview_crm_v2",
        "--skip-sales-motion",
        "--manual-pin",
        "StackArmor",
        "--owner-override",
        "StackArmor=Arian",
        "--output",
        str(output),
    )

    payload = json.loads(output.read_text())
    row = payload["active_accounts"][0]
    assert row["account_name"] == "StackArmor"
    assert row["lead_ids"] == [lead.id]
    assert row["do_not_outreach"] is True
    assert row["owner"] == "Arian"
    assert row["decision"]["primary_reason_code"] == "manual_pin"
    assert output.stat().st_mode & 0o777 == 0o600


def test_preview_ignores_outbound_only_linkedin(tmp_path):
    lead = Lead.objects.create(
        company_name="Outbound Only",
        linkedin_url="https://www.linkedin.com/in/outbound-only/",
    )
    Message.objects.create(
        lead=lead,
        source=Message.Source.LINKEDIN,
        external_id="outbound-only",
        direction=Message.Direction.OUTBOUND,
        body="Would love to connect.",
        sent_at="2026-08-20T12:00:00Z",
    )
    output = tmp_path / "preview.json"

    call_command(
        "preview_crm_v2",
        "--skip-sales-motion",
        "--output",
        str(output),
    )

    payload = json.loads(output.read_text())
    assert payload["summary"]["active_accounts"] == 0
    assert payload["summary"]["people_only_accounts"] == 1


def test_preview_reads_people_dno_and_serializes_exact_reminder_target_safety(
    monkeypatch,
    tmp_path,
):
    from linkedin import conf
    from linkedin.notifications import sheets

    allowed = Lead.objects.create(
        first_name="Allowed",
        company_name="Mixed Safety",
        email="allowed@mixed-safety.example",
    )
    stopped = Lead.objects.create(
        first_name="Stopped",
        company_name="Mixed Safety",
        email="stopped@mixed-safety.example",
    )
    Message.objects.create(
        lead=stopped,
        source=Message.Source.GMAIL,
        external_id="mixed-safety-inbound",
        direction=Message.Direction.INBOUND,
        body="Can we schedule a meeting?",
        sent_at=timezone.now(),
    )

    class PeopleWorksheet:
        def get_all_values(self):
            return [
                ["Lead ID", "LinkedIn URL", "Outreach status"],
                [str(allowed.id), "", ""],
                [str(stopped.id), "", "Don't send"],
            ]

    class Spreadsheet:
        id = "preview-workbook"

        def worksheet(self, title):
            assert title == "People"
            return PeopleWorksheet()

    monkeypatch.setattr(conf, "GOOGLE_SHEETS_ID", "preview-workbook")
    monkeypatch.setattr(sheets, "_gspread_client", lambda: Spreadsheet())
    output = tmp_path / "preview.json"

    call_command(
        "preview_crm_v2",
        "--skip-sales-motion",
        "--output",
        str(output),
        stdout=io.StringIO(),
    )

    payload = json.loads(output.read_text())
    row = payload["active_accounts"][0]
    assert payload["summary"]["people_dont_send_leads"] == 1
    assert row["do_not_outreach"] is False
    assert row["reminder_target_lead_id"] == stopped.id
    assert row["reminder_do_not_outreach"] is True
