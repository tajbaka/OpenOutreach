from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone

from linkedin import conf
from linkedin.discovery.collector import known_profile_reason, save_discovery_profile
from linkedin.discovery.limits import remaining_today, saved_today
from linkedin.discovery.sources.base import DiscoveryCard
from linkedin.models import LinkedInDiscoveryLead, OutreachSuppression
from tests.factories import LeadFactory, UserFactory


def _profile(public_identifier: str) -> dict:
    return {
        "public_identifier": public_identifier,
        "url": f"https://www.linkedin.com/in/{public_identifier}/",
        "urn": f"urn:li:fsd_profile:{public_identifier}",
        "first_name": "Jane",
        "last_name": "Doe",
        "full_name": "Jane Doe",
        "headline": "VP Security",
        "location_name": "Toronto",
        "positions": [{"company_name": "Example Cloud"}],
    }


@pytest.mark.django_db
def test_save_stores_profile_sender_and_potential_icp(fake_session):
    from crm.models import Deal

    deals_before = Deal.objects.count()
    result = save_discovery_profile(
        linkedin_profile=fake_session.linkedin_profile,
        operator="Arian",
        potential_icp="CSPs",
        profile=_profile("jane-discovery"),
    )

    row = LinkedInDiscoveryLead.objects.get()
    assert result.created
    assert row.stored_by_operator == "Arian"
    assert row.potential_icp == "CSPs"
    assert row.profile_data["headline"] == "VP Security"
    assert row.company_name == "Example Cloud"
    assert Deal.objects.count() == deals_before


@pytest.mark.django_db
def test_first_sender_ownership_is_not_overwritten(fake_session):
    first = save_discovery_profile(
        linkedin_profile=fake_session.linkedin_profile,
        operator="Arian",
        potential_icp="CSPs",
        profile=_profile("shared-discovery"),
    )
    second_user = UserFactory(username="second-sender")
    from linkedin.models import LinkedInProfile

    second_profile = LinkedInProfile.objects.create(
        user=second_user,
        linkedin_username="chukyjack@gmail.com",
        linkedin_password="test",
    )
    second = save_discovery_profile(
        linkedin_profile=second_profile,
        operator="Chuka",
        potential_icp="Advisors",
        profile=_profile("shared-discovery"),
    )

    row = LinkedInDiscoveryLead.objects.get()
    assert first.created
    assert not second.created
    assert second.reason == "existing_discovery_lead"
    assert row.stored_by_operator == "Arian"
    assert row.potential_icp == "CSPs"


@pytest.mark.django_db
def test_sender_daily_limit_blocks_second_new_profile(fake_session, monkeypatch):
    monkeypatch.setattr(conf, "ACTIVE_TIMEZONE", "UTC")
    monkeypatch.setattr(conf, "DISCOVERY_DAILY_LIMIT", 1)

    first = save_discovery_profile(
        linkedin_profile=fake_session.linkedin_profile,
        operator="Arian",
        potential_icp="CSPs",
        profile=_profile("limit-one"),
    )
    second = save_discovery_profile(
        linkedin_profile=fake_session.linkedin_profile,
        operator="Arian",
        potential_icp="CSPs",
        profile=_profile("limit-two"),
    )

    assert first.created and first.daily_limit_reached
    assert not second.created and second.daily_limit_reached
    assert LinkedInDiscoveryLead.objects.count() == 1


@pytest.mark.django_db
def test_duplicate_does_not_consume_sender_capacity(fake_session, monkeypatch):
    monkeypatch.setattr(conf, "ACTIVE_TIMEZONE", "UTC")
    monkeypatch.setattr(conf, "DISCOVERY_DAILY_LIMIT", 2)
    save_discovery_profile(
        linkedin_profile=fake_session.linkedin_profile,
        operator="Arian",
        potential_icp="CSPs",
        profile=_profile("duplicate-capacity"),
    )

    duplicate = save_discovery_profile(
        linkedin_profile=fake_session.linkedin_profile,
        operator="Arian",
        potential_icp="Advisors",
        profile=_profile("duplicate-capacity"),
    )

    assert not duplicate.created
    assert saved_today("Arian") == 1
    assert remaining_today("Arian") == 1


@pytest.mark.django_db
def test_old_rows_do_not_count_against_today(fake_session, monkeypatch):
    monkeypatch.setattr(conf, "ACTIVE_TIMEZONE", "UTC")
    save_discovery_profile(
        linkedin_profile=fake_session.linkedin_profile,
        operator="Arian",
        potential_icp="CSPs",
        profile=_profile("old-discovery"),
    )
    old = datetime.now(tz=ZoneInfo("UTC")) - timedelta(days=2)
    LinkedInDiscoveryLead.objects.update(created_at=old)

    assert saved_today("Arian", now=timezone.now()) == 0


@pytest.mark.django_db
def test_existing_crm_lead_is_skipped_before_profile_visit():
    LeadFactory(
        public_identifier="already-in-crm",
        linkedin_url="https://www.linkedin.com/in/already-in-crm/",
    )

    assert known_profile_reason(
        DiscoveryCard(
            public_identifier="already-in-crm",
            linkedin_url="https://www.linkedin.com/in/already-in-crm/",
        ),
    ) == "existing_crm_lead"


@pytest.mark.django_db
def test_active_suppression_is_skipped_before_profile_visit():
    OutreachSuppression.objects.create(
        kind=OutreachSuppression.Kind.LEAD,
        value="Jane Doe",
        public_identifier="suppressed-profile",
    )

    assert known_profile_reason(
        DiscoveryCard(
            public_identifier="suppressed-profile",
            linkedin_url="https://www.linkedin.com/in/suppressed-profile/",
            name="Jane Doe",
        ),
    ) == "suppressed"
