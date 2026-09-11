from __future__ import annotations

import json
from io import StringIO

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError

from crm.models import Deal, Lead
from linkedin.models import Campaign, CampaignMessageEnrollment, MessageProgramVersion, Task
from linkedin.setup.freemium import import_freemium_campaign
from tests.management.test_import_campaign_message_program import _drafts
from tests.test_campaign_email_mode import _owner


pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def campaign_payload(monkeypatch):
    _owner("Arian")
    _owner("Chuka")
    monkeypatch.setattr("linkedin.general_icp_json.load_general_message_programs", _drafts)
    return {
        "name": "Explicit mode campaign", "owner_username": "Chuka",
        "gmail_start_mode": "invitation_sent", "message_program_key": "fedramp-marketplace-csp",
    }


def _import(tmp_path, payload, **kwargs):
    path = tmp_path / "campaign.json"
    path.write_text(json.dumps(payload))
    output = StringIO()
    call_command("import_campaign", str(path), stdout=output, **kwargs)
    return output.getvalue()


def _counts():
    return tuple(model.objects.count() for model in (Campaign, MessageProgramVersion, CampaignMessageEnrollment, Task))


@pytest.mark.parametrize("mode", ["invitation_sent", "post_acceptance"])
def test_new_import_persists_explicit_mode_and_exact_owner_not_first_profile(tmp_path, campaign_payload, mode):
    campaign_payload["gmail_start_mode"] = mode
    output = _import(tmp_path, campaign_payload)
    campaign = Campaign.objects.get(name=campaign_payload["name"])
    assert campaign.user.username == "Chuka"
    assert campaign.gmail_start_mode == mode
    assert campaign.active_message_version is not None
    assert campaign.status == ("disabled" if mode == "invitation_sent" else "active")
    assert f"gmail_start_mode={mode}" in output
    assert "owner_username=Chuka" in output
    assert CampaignMessageEnrollment.objects.count() == 0
    assert Task.objects.count() == 0


@pytest.mark.parametrize("status", ["active", "disabled", "finished"])
def test_import_preserves_explicit_valid_status(tmp_path, campaign_payload, status):
    campaign_payload["status"] = status
    output = _import(tmp_path, campaign_payload)
    assert Campaign.objects.get(name=campaign_payload["name"]).status == status
    assert f"status={status}" in output


@pytest.mark.parametrize("status", [None, "", "invalid"])
def test_import_rejects_invalid_status_before_writes(tmp_path, campaign_payload, status):
    campaign_payload["status"] = status
    before = _counts()
    with pytest.raises(CommandError):
        _import(tmp_path, campaign_payload)
    assert _counts() == before


@pytest.mark.parametrize("field,value", [
    ("gmail_start_mode", None), ("gmail_start_mode", ""), ("gmail_start_mode", "pre"),
    ("owner_username", None), ("owner_username", ""), ("owner_username", "eddy"),
    ("message_program_key", None), ("message_program_key", "invalid"),
])
def test_missing_or_invalid_creation_choice_fails_before_any_writes(tmp_path, campaign_payload, field, value):
    campaign_payload[field] = value
    before = _counts()
    with pytest.raises(CommandError):
        _import(tmp_path, campaign_payload)
    assert _counts() == before


def test_import_dry_run_is_read_only_and_shows_choice(tmp_path, campaign_payload):
    before = _counts()
    output = _import(tmp_path, campaign_payload, dry_run=True)
    assert "gmail_start_mode=invitation_sent" in output
    assert "Dry run" in output
    assert _counts() == before


@pytest.mark.parametrize("field,value", [("booking_link", "not-a-url"), ("action_fraction", "invalid")])
def test_import_dry_run_rejects_invalid_non_mode_fields(tmp_path, campaign_payload, field, value):
    before = _counts()
    campaign_payload[field] = value
    with pytest.raises(CommandError):
        _import(tmp_path, campaign_payload, dry_run=True)
    assert _counts() == before


def test_cli_explicit_choices_supported_without_json_defaults(tmp_path, campaign_payload):
    campaign_payload.pop("gmail_start_mode")
    campaign_payload.pop("owner_username")
    _import(tmp_path, campaign_payload, gmail_start_mode="post_acceptance", owner_username="Arian")
    campaign = Campaign.objects.get(name=campaign_payload["name"])
    assert campaign.gmail_start_mode == "post_acceptance"
    assert campaign.user.username == "Arian"


def test_existing_import_with_no_mode_or_owner_preserves_both(tmp_path, campaign_payload):
    _import(tmp_path, campaign_payload)
    campaign = Campaign.objects.get(name=campaign_payload["name"])
    Deal.objects.create(campaign=campaign, lead=Lead.objects.create(first_name="QA"))
    campaign_payload.pop("owner_username")
    campaign_payload.pop("gmail_start_mode")
    campaign_payload["product_docs"] = "Updated description"
    _import(tmp_path, campaign_payload)
    campaign.refresh_from_db()
    assert campaign.user.username == "Chuka"
    assert campaign.gmail_start_mode == "invitation_sent"
    assert campaign.product_docs == "Updated description"


def test_existing_campaign_rejects_mode_change_and_rolls_back_program_publication(tmp_path, campaign_payload):
    _import(tmp_path, campaign_payload)
    campaign = Campaign.objects.get(name=campaign_payload["name"])
    Deal.objects.create(campaign=campaign, lead=Lead.objects.create(first_name="QA"))
    before = _counts()
    campaign_payload["gmail_start_mode"] = "post_acceptance"
    with pytest.raises(CommandError, match="cannot change after enrollment"):
        _import(tmp_path, campaign_payload)
    campaign.refresh_from_db()
    assert campaign.gmail_start_mode == "invitation_sent"
    assert _counts() == before


def test_export_import_roundtrip_preserves_mode_owner_and_program(tmp_path, campaign_payload):
    _import(tmp_path, campaign_payload)
    campaign = Campaign.objects.get(name=campaign_payload["name"])
    output = StringIO()
    call_command("export_campaign", str(campaign.pk), stdout=output)
    exported = json.loads(output.getvalue())
    assert exported["gmail_start_mode"] == "invitation_sent"
    assert exported["owner_username"] == "Chuka"
    assert exported["status"] == "disabled"
    assert exported["message_program_key"] == campaign_payload["message_program_key"]
    exported["name"] = "Imported copy"
    _import(tmp_path, exported)
    copied = Campaign.objects.get(name="Imported copy")
    assert copied.gmail_start_mode == campaign.gmail_start_mode
    assert copied.user_id == campaign.user_id
    assert copied.active_message_version_id == campaign.active_message_version_id
    assert copied.status == campaign.status


def _kit(**kwargs):
    return {
        "campaign_name": "Freemium explicit", "product_docs": "Product", "campaign_objective": "Learn",
        "booking_link": "", "action_fraction": .2, **kwargs,
    }


def test_freemium_new_kit_requires_explicit_choice_before_writes(campaign_payload):
    before = _counts()
    with pytest.raises(ValidationError, match="explicit gmail_start_mode"):
        import_freemium_campaign(_kit())
    assert _counts() == before


def test_freemium_existing_kit_preserves_mode_and_owner_when_omitted(campaign_payload):
    campaign = import_freemium_campaign(_kit(owner_username="Chuka", gmail_start_mode="post_acceptance"))
    updated = import_freemium_campaign(_kit())
    assert campaign.pk == updated.pk
    assert updated.user.username == "Chuka"
    assert updated.gmail_start_mode == "post_acceptance"


def test_freemium_invitation_mode_requires_versioned_program(campaign_payload):
    before = _counts()
    with pytest.raises(ValidationError, match="message_program_key"):
        import_freemium_campaign(_kit(owner_username="Chuka", gmail_start_mode="invitation_sent"))
    assert _counts() == before
