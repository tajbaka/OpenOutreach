from __future__ import annotations

from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.contrib import admin
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from crm.models import Deal, Lead
from linkedin.admin import LinkedInProfileAdmin
from linkedin.models import (
    Campaign,
    CampaignMessageEnrollment,
    LinkedInProfile,
    MessageProgram,
    MessageProgramVersion,
    OutboundDelivery,
    Task,
)


pytestmark = pytest.mark.django_db


def _profile(username, linkedin_username, **kwargs):
    return LinkedInProfile.objects.create(
        user=User.objects.create(username=username),
        linkedin_username=linkedin_username,
        linkedin_password="synthetic-test-password",
        **kwargs,
    )


def _request(operator="Arian", **kwargs):
    output = StringIO()
    call_command("request_sender_restart", operator=operator, stdout=output, **kwargs)
    return output.getvalue()


def _snapshot():
    return {
        model: list(model.objects.order_by("pk").values())
        for model in (
            User, LinkedInProfile, Campaign, Lead, Deal, MessageProgram,
            MessageProgramVersion, CampaignMessageEnrollment, OutboundDelivery, Task,
        )
    }


@pytest.mark.parametrize("explicit_dry_run", [False, True])
def test_dry_run_previews_the_exact_sender_without_writes(explicit_dry_run):
    _profile("other-owner", "chukyjack@gmail.com")
    arian = _profile("local-login", "ariantajbakh@gmail.com")
    before = _snapshot()

    output = _request(dry_run=explicit_dry_run)

    assert f"Dry run: Arian, LinkedInProfile {arian.pk}: would request restart" in output
    assert _snapshot() == before


@pytest.mark.parametrize("operator,login,canonical", [
    ("Arian", "ariantajbakh@gmail.com", "Arian"),
    ("Chuka", "chukyjack", "Chuka"),
    ("Eddy", "chukyjack@gmail.com", "Chuka"),
    ("athena", "athenaaghdami@gmail.com", "Athena"),
    ("Leili", "leili.ash2011@yahoo.com", "Leili"),
])
def test_apply_uses_canonical_actual_linkedin_login_even_for_inactive_profile(operator, login, canonical):
    profile = _profile("unrelated-django-username", login, active=False)

    output = _request(operator, apply=True)

    profile.refresh_from_db()
    assert profile.restart_requested is True
    assert profile.active is False
    assert f"Restart requested for {canonical}, LinkedInProfile {profile.pk}" in output


def test_apply_changes_only_exact_profile_flag_and_preserves_all_outreach():
    arian = _profile("local-arian", "ariantajbakh@gmail.com", connect_daily_limit=7)
    _profile("local-chuka", "chukyjack@gmail.com", restart_requested=True)
    program = MessageProgram.objects.create(key="restart-fixture", name="Restart fixture")
    version = MessageProgramVersion.objects.create(
        program=program, version=1, payload={}, content_hash="a" * 64, published_by="qa",
    )
    campaign = Campaign.objects.create(
        name="Restart preserves campaign", user=arian.user, status="active",
        active_message_version=version,
    )
    lead = Lead.objects.create(
        first_name="QA", icp="reviewed-audience", role_tag="Founder/CEO",
        public_identifier="restart-preservation-fixture",
        linkedin_url="https://www.linkedin.com/in/restart-preservation-fixture/",
    )
    deal = Deal.objects.create(campaign=campaign, lead=lead)
    enrollment = CampaignMessageEnrollment.objects.create(
        deal=deal, message_version=version, audience_key="reviewed-audience", operator="Arian",
    )
    task = Task.objects.create(
        task_type="follow_up", scheduled_at=timezone.now(),
        payload={
            "lead_id": lead.pk, "public_id": lead.public_identifier,
            "campaign_id": campaign.pk, "operator": "Arian",
        },
    )
    OutboundDelivery.objects.create(
        enrollment=enrollment, channel="linkedin", step_key="follow-up-1", step_index=0,
        variant_key="control", operator="Arian", scheduled_at=task.scheduled_at,
        frozen_body="Exact saved copy", render_hash="b" * 64, task=task,
    )
    before = _snapshot()

    with patch.object(LinkedInProfile, "save", side_effect=AssertionError("Do not save a stale profile")):
        _request(apply=True)

    expected = before
    for profile in expected[LinkedInProfile]:
        if profile["id"] == arian.pk:
            profile["restart_requested"] = True
    assert _snapshot() == expected


@pytest.mark.parametrize("apply", [False, True])
def test_existing_request_is_reported_without_changes(apply):
    profile = _profile("arian-local", "ariantajbakh@gmail.com", restart_requested=True)
    before = _snapshot()

    output = _request(apply=apply)

    assert "already requested" in output
    assert str(profile.pk) in output
    assert _snapshot() == before


@pytest.mark.parametrize("apply", [False, True])
def test_missing_profile_does_not_use_django_username_or_create_one(apply):
    _profile("Arian", "unmapped-sender@example.invalid")
    before = _snapshot()

    with pytest.raises(CommandError, match="found 0"):
        _request(apply=apply)

    assert _snapshot() == before


@pytest.mark.parametrize("apply", [False, True])
@pytest.mark.parametrize("second_active", [False, True])
def test_ambiguous_canonical_sender_profiles_are_refused(apply, second_active):
    _profile("arian-first", "ariantajbakh@gmail.com")
    _profile("arian-second", "arian@boundera.io", active=second_active)
    before = _snapshot()

    with pytest.raises(CommandError, match="found 2"):
        _request(apply=apply)

    assert _snapshot() == before


@pytest.mark.parametrize("operator", ["", "Unknown", "all"])
def test_unknown_operator_is_refused_without_writes(operator):
    before = _snapshot()

    with pytest.raises(CommandError, match="Choose a known operator"):
        _request(operator, apply=True)

    assert _snapshot() == before


def test_admin_exposes_flag_and_only_writes_changed_fields():
    profile = _profile("arian-admin", "ariantajbakh@gmail.com", connect_daily_limit=7)
    model_admin = LinkedInProfileAdmin(LinkedInProfile, admin.site)
    assert "restart_requested" in model_admin.list_display
    assert "restart_requested" in model_admin.list_editable
    assert "restart_requested" in model_admin.list_filter
    LinkedInProfile.objects.filter(pk=profile.pk).update(connect_daily_limit=13)
    profile.restart_requested = True

    model_admin.save_model(None, profile, SimpleNamespace(changed_data=["restart_requested"]), True)

    profile.refresh_from_db()
    assert profile.restart_requested is True
    assert profile.connect_daily_limit == 13


def test_unrelated_admin_edit_does_not_restore_consumed_restart_request():
    profile = _profile("arian-admin", "ariantajbakh@gmail.com", restart_requested=True)
    LinkedInProfile.objects.filter(pk=profile.pk).update(restart_requested=False)
    profile.active = False

    LinkedInProfileAdmin(LinkedInProfile, admin.site).save_model(
        None, profile, SimpleNamespace(changed_data=["active"]), True,
    )

    profile.refresh_from_db()
    assert profile.active is False
    assert profile.restart_requested is False
