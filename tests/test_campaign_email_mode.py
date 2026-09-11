from __future__ import annotations

from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from crm.models import Deal, Lead
from linkedin.admin import CampaignAdminForm
from linkedin.campaign_setup import EMAIL_START_QUESTION, exact_campaign_owner, prompt_gmail_start_mode
from linkedin.models import Campaign, LinkedInProfile, MessageProgram, MessageProgramVersion, Task


pytestmark = pytest.mark.django_db


def _owner(username="Arian"):
    user = User.objects.create(username=username)
    LinkedInProfile.objects.create(
        user=user, linkedin_username=username, linkedin_password="unused", active=True,
    )
    return user


def _version():
    program = MessageProgram.objects.create(key="mode-test", name="Mode test")
    return MessageProgramVersion.objects.create(
        program=program, version=1, payload={}, content_hash="a" * 64, published_by="qa",
    )


def _invitation_campaign():
    return Campaign.objects.create(
        name="New invitation email", user=_owner(), active_message_version=_version(),
        gmail_start_mode=Campaign.GmailStartMode.INVITATION_SENT,
    )


def test_default_preserves_internal_post_acceptance_campaigns():
    campaign = Campaign.objects.create(name="Legacy internal", user=_owner())
    assert campaign.gmail_start_mode == Campaign.GmailStartMode.POST_ACCEPTANCE


@pytest.mark.parametrize("invalid", ["", "before", "pre", None])
def test_model_rejects_invalid_mode(invalid):
    with pytest.raises(ValidationError, match="Choose pre-acceptance"):
        Campaign.objects.create(name="Invalid", user=_owner(), gmail_start_mode=invalid)
    assert not Campaign.objects.filter(name="Invalid").exists()


def test_pre_acceptance_requires_version_and_canonical_owner():
    owner = _owner("unmapped-user")
    campaign = Campaign(name="Unbound", user=owner, gmail_start_mode="invitation_sent")
    with pytest.raises(ValidationError) as error:
        campaign.save()
    assert {"user", "active_message_version"} <= set(error.value.message_dict)


@pytest.mark.parametrize("profile_username", ["unknown@example.com", "Chuka"])
def test_exact_owner_rejects_unknown_or_conflicting_actual_profile(profile_username):
    owner = _owner()
    LinkedInProfile.objects.filter(user=owner).update(linkedin_username=profile_username)
    with pytest.raises(ValidationError, match="canonical LinkedIn sender"):
        exact_campaign_owner(owner.username)
    with pytest.raises(ValidationError, match="exact canonical sender"):
        Campaign.objects.create(
            name="Mismatched sender", user=owner, active_message_version=_version(), gmail_start_mode="invitation_sent",
        )


def test_new_pre_acceptance_rejects_missing_or_inactive_profile():
    owner = _owner()
    LinkedInProfile.objects.filter(user=owner).update(active=False)
    with pytest.raises(ValidationError, match="exact canonical sender"):
        Campaign.objects.create(
            name="Inactive sender", user=owner, active_message_version=_version(), gmail_start_mode="invitation_sent",
        )


def test_historical_campaign_cannot_opt_in_even_before_enrollment():
    campaign = Campaign.objects.create(name="Historical", user=_owner(), active_message_version=_version())
    campaign.gmail_start_mode = "invitation_sent"
    with pytest.raises(ValidationError, match="only available when creating"):
        campaign.save()
    campaign.refresh_from_db()
    assert campaign.gmail_start_mode == "post_acceptance"


@pytest.mark.parametrize("work", ["deal", "task"])
@pytest.mark.parametrize("field", ["gmail_start_mode", "user"])
def test_mode_and_owner_frozen_after_enrollment_or_outreach(work, field):
    campaign = _invitation_campaign()
    if work == "deal":
        Deal.objects.create(campaign=campaign, lead=Lead.objects.create(first_name="QA"))
    else:
        Task.objects.create(
            task_type="connect", scheduled_at=timezone.now(), payload={"campaign_id": campaign.pk},
        )
    setattr(campaign, field, "post_acceptance" if field == "gmail_start_mode" else _owner("Chuka"))
    with pytest.raises(ValidationError, match="cannot change after enrollment"):
        campaign.save(update_fields=[field])
    campaign.refresh_from_db()
    assert campaign.gmail_start_mode == "invitation_sent"
    assert campaign.user.username == "Arian"


def test_disabling_campaign_remains_possible_without_retiming():
    campaign = _invitation_campaign()
    Deal.objects.create(campaign=campaign, lead=Lead.objects.create(first_name="QA"))
    campaign.status = Campaign.Status.DISABLED
    campaign.save(update_fields=["status"])
    campaign.refresh_from_db()
    assert campaign.status == Campaign.Status.DISABLED
    assert campaign.gmail_start_mode == "invitation_sent"


def test_status_only_pause_allowed_after_sender_becomes_unavailable():
    campaign = _invitation_campaign()
    LinkedInProfile.objects.filter(user=campaign.user).update(active=False)
    campaign.status = Campaign.Status.DISABLED
    campaign.save(update_fields=["status"])
    campaign.refresh_from_db()
    assert campaign.status == Campaign.Status.DISABLED


@pytest.mark.parametrize("field", ["user", "user_id"])
def test_partial_owner_save_cannot_mask_persisted_invitation_mode(field):
    campaign = _invitation_campaign()
    original_owner = campaign.user_id
    campaign.gmail_start_mode = "post_acceptance"  # Not part of this UPDATE.
    campaign.user = _owner("unmapped-owner")
    with pytest.raises(ValidationError, match="exact canonical sender"):
        campaign.save(update_fields=[field])
    campaign.refresh_from_db()
    assert campaign.user_id == original_owner
    assert campaign.gmail_start_mode == "invitation_sent"


@pytest.mark.parametrize("field", ["active_message_version", "active_message_version_id"])
def test_partial_version_save_cannot_mask_invitation_binding_requirement(field):
    campaign = _invitation_campaign()
    original_version = campaign.active_message_version_id
    campaign.gmail_start_mode = "post_acceptance"  # Not being persisted.
    campaign.active_message_version = None
    with pytest.raises(ValidationError, match="published message version"):
        campaign.save(update_fields=[field])
    campaign.refresh_from_db()
    assert campaign.active_message_version_id == original_version
    assert campaign.gmail_start_mode == "invitation_sent"


def test_partial_version_save_ignores_dirty_unpersisted_mode_after_enrollment():
    campaign = _invitation_campaign()
    Deal.objects.create(campaign=campaign, lead=Lead.objects.create(first_name="QA"))
    version_id = campaign.active_message_version_id
    campaign.gmail_start_mode = "post_acceptance"
    campaign.save(update_fields=["active_message_version"])
    # The caller's dirty value stays local; validation used the fresh DB mode.
    assert campaign.gmail_start_mode == "post_acceptance"
    persisted = Campaign.objects.get(pk=campaign.pk)
    assert persisted.gmail_start_mode == "invitation_sent"
    assert persisted.active_message_version_id == version_id


def test_partial_mode_save_ignores_dirty_unpersisted_owner_after_enrollment():
    campaign = _invitation_campaign()
    Deal.objects.create(campaign=campaign, lead=Lead.objects.create(first_name="QA"))
    owner_id = campaign.user_id
    campaign.user = _owner("unmapped-owner")
    campaign.save(update_fields=["gmail_start_mode"])
    persisted = Campaign.objects.get(pk=campaign.pk)
    assert persisted.user_id == owner_id
    assert persisted.gmail_start_mode == "invitation_sent"


def test_partial_save_consumes_update_fields_generator_once():
    campaign = _invitation_campaign()
    replacement = _owner("Chuka")
    campaign.user = replacement
    campaign.save(update_fields=(field for field in ["user_id"]))
    campaign.refresh_from_db()
    assert campaign.user_id == replacement.pk
    assert campaign.gmail_start_mode == "invitation_sent"


def test_model_database_constraints_reject_invalid_mode_or_unbound_opt_in():
    campaign = Campaign.objects.create(name="Constrained", user=_owner())
    with pytest.raises(IntegrityError), transaction.atomic():
        Campaign.objects.filter(pk=campaign.pk).update(gmail_start_mode="invalid")
    with pytest.raises(IntegrityError), transaction.atomic():
        Campaign.objects.filter(pk=campaign.pk).update(gmail_start_mode="invitation_sent")


@pytest.mark.parametrize("choice,expected", [("pre", "invitation_sent"), ("post", "post_acceptance")])
def test_interactive_question_requires_explicit_answer_without_default(choice, expected, capsys):
    with patch("builtins.input", side_effect=["", "maybe", choice]) as prompt:
        assert prompt_gmail_start_mode() == expected
    assert prompt.call_count == 3
    output = capsys.readouterr().out
    assert EMAIL_START_QUESTION in output
    assert "explicitly choose" in output


def test_interactive_cancellation_does_not_create_campaign():
    before = Campaign.objects.count()
    with patch("builtins.input", side_effect=EOFError), pytest.raises(EOFError):
        prompt_gmail_start_mode()
    assert Campaign.objects.count() == before


def test_admin_new_campaign_shows_blank_required_mode_not_model_default():
    form = CampaignAdminForm()
    assert form.initial["gmail_start_mode"] == ""
    assert form.fields["gmail_start_mode"].required
    assert form.fields["gmail_start_mode"].label == EMAIL_START_QUESTION
    assert form.fields["gmail_start_mode"].choices[0][0] == ""
    bound = CampaignAdminForm(data={"name": "Missing", "user": _owner().pk})
    assert not bound.is_valid()
    assert "gmail_start_mode" in bound.errors


def test_admin_existing_campaign_displays_saved_mode():
    campaign = _invitation_campaign()
    form = CampaignAdminForm(instance=campaign)
    assert form.initial["gmail_start_mode"] == "invitation_sent"


def test_onboarding_cancellation_before_mode_choice_does_not_write():
    from linkedin.onboarding import _onboard_campaign

    owner = _owner()
    before = Campaign.objects.count()
    # A true EOF aborts instead of silently accepting the migration default.
    with patch("builtins.input", side_effect=["Campaign name", EOFError]), pytest.raises(EOFError):
        _onboard_campaign(owner)
    assert Campaign.objects.count() == before


@pytest.mark.parametrize("mode", ["pre", "post"])
def test_onboarding_records_explicit_sender_and_mode_in_preview(tmp_path, monkeypatch, capsys, mode):
    from linkedin import onboarding
    from tests.management.test_import_campaign_message_program import _drafts

    active_first = _owner("Arian")
    chosen = _owner("Chuka")
    default_text = tmp_path / "default.txt"
    default_text.write_text("Synthetic product and objective")
    monkeypatch.setattr(onboarding, "DEFAULT_PRODUCT_DOCS", default_text)
    monkeypatch.setattr(onboarding, "DEFAULT_CAMPAIGN_OBJECTIVE", default_text)
    monkeypatch.setattr("linkedin.general_icp_json.load_general_message_programs", _drafts)
    answers = ["Interactive choice", mode, chosen.username]
    if mode == "pre":
        answers.append("fedramp-marketplace-csp")
    answers += ["Y", "Y", ""]
    with patch("builtins.input", side_effect=answers):
        campaign = onboarding._onboard_campaign(active_first)
    assert campaign.user == chosen
    assert campaign.gmail_start_mode == ("invitation_sent" if mode == "pre" else "post_acceptance")
    assert bool(campaign.active_message_version_id) == (mode == "pre")
    assert "sender=Chuka" in capsys.readouterr().out
