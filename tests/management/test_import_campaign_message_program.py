import json

import pytest
from django.contrib.auth.models import User
from django.core.management import call_command

from linkedin.general_icp_messages import GENERAL_ICP_MESSAGES_HEADERS, parse_general_icp_message_rows
from linkedin.models import Campaign, LinkedInProfile


def _drafts(body="Hi {first_name}"):
    values = {
        "ICP": "CRO | Enterprise | Rev5 Authorized → 20x | Direct agency",
        "Connect Message": body,
        "Followup Message 1": "Follow up",
        "Email Subject 1": "Subject",
        "Email Body 1": "Email body",
        "Followup Message 2": "Follow up 2",
        "Email Subject 2": "Subject 2",
        "Email Body 2": "Email body 2",
    }
    return parse_general_icp_message_rows([
        list(GENERAL_ICP_MESSAGES_HEADERS),
        [values[header] for header in GENERAL_ICP_MESSAGES_HEADERS],
    ])


@pytest.mark.django_db(transaction=True)
def test_import_campaign_snapshots_current_json_program(monkeypatch, tmp_path):
    user = User.objects.create(username="Arian")
    LinkedInProfile.objects.create(
        user=user,
        linkedin_username="Arian",
        linkedin_password="unused",
        active=True,
    )
    current = [_drafts()]
    monkeypatch.setattr(
        "linkedin.general_icp_json.load_general_message_programs",
        lambda: current[0],
    )
    path = tmp_path / "campaign.json"
    path.write_text(json.dumps({
        "name": "Marketplace campaign",
        "message_program_key": "fedramp-marketplace-csp",
        "gmail_start_mode": "post_acceptance",
        "owner_username": "Arian",
    }))

    call_command("import_campaign", str(path))
    campaign = Campaign.objects.get(name="Marketplace campaign")
    first_version = campaign.active_message_version
    assert first_version.payload == current[0][0].payload()

    current[0] = _drafts(body="Updated {first_name}")
    call_command("import_campaign", str(path))
    campaign.refresh_from_db()
    assert campaign.active_message_version.version == 2
    assert campaign.active_message_version_id != first_version.pk
