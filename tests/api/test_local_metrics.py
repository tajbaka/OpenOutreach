from datetime import timedelta

import pytest
from django.contrib.auth.models import User

from linkedin.models import ActionLog, Campaign, LinkedInProfile, active_day_start


@pytest.mark.django_db
def test_connect_counts_today_defaults_to_operator_profiles(client):
    users = {
        handle: User.objects.create_user(username=handle)
        for handle in ("ariantajbakh", "chukyjack", "leiliash2011")
    }
    campaign = Campaign.objects.create(name="Metrics Test", user=users["ariantajbakh"])
    profiles = {
        handle: LinkedInProfile.objects.create(
            user=user,
            linkedin_username=f"{handle}@example.com",
            linkedin_password="test",
        )
        for handle, user in users.items()
    }

    for _ in range(2):
        ActionLog.objects.create(
            linkedin_profile=profiles["ariantajbakh"],
            campaign=campaign,
            action_type=ActionLog.ActionType.CONNECT,
        )
    ActionLog.objects.create(
        linkedin_profile=profiles["chukyjack"],
        campaign=campaign,
        action_type=ActionLog.ActionType.CONNECT,
    )
    old = ActionLog.objects.create(
        linkedin_profile=profiles["leiliash2011"],
        campaign=campaign,
        action_type=ActionLog.ActionType.CONNECT,
    )
    ActionLog.objects.filter(pk=old.pk).update(
        created_at=active_day_start() - timedelta(minutes=1)
    )

    response = client.get("/api/local/connect-counts/")

    assert response.status_code == 200
    data = response.json()
    counts = {profile["handle"]: profile["connects_today"] for profile in data["profiles"]}
    assert counts == {
        "ariantajbakh": 2,
        "chukyjack": 1,
        "leiliash2011": 0,
    }
    assert data["total"] == 3
    assert data["missing_handles"] == []


@pytest.mark.django_db
def test_connect_counts_today_supports_handles_filter(client):
    user = User.objects.create_user(username="custom")
    campaign = Campaign.objects.create(name="Metrics Filter Test", user=user)
    profile = LinkedInProfile.objects.create(
        user=user,
        linkedin_username="custom@example.com",
        linkedin_password="test",
    )
    ActionLog.objects.create(
        linkedin_profile=profile,
        campaign=campaign,
        action_type=ActionLog.ActionType.CONNECT,
    )

    response = client.get("/api/local/connect-counts/?handles=custom,missing")

    assert response.status_code == 200
    data = response.json()
    assert [profile["handle"] for profile in data["profiles"]] == ["custom"]
    assert data["profiles"][0]["connects_today"] == 1
    assert data["missing_handles"] == ["missing"]
