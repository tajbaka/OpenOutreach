# tests/conftest.py
from unittest.mock import patch

import pytest

from linkedin.management.setup_crm import setup_crm
from tests.factories import UserFactory


@pytest.fixture(autouse=True)
def _ensure_crm_data(db):
    """
    Ensure CRM bootstrap data exists before every test.
    Uses `db` fixture (not transactional_db) for compatibility.
    Since transaction=True tests rollback, we re-create data each time.
    """
    setup_crm()


@pytest.fixture(autouse=True)
def _silence_slack(monkeypatch):
    """Disable Slack notifications during tests.

    Without this, `handle_sweep_connections` tests (which transition the
    fixture lead "Alice Smith" from PENDING → CONNECTED) call
    `notify_connection_accepted`, which reads `SLACK_WEBHOOK_URL` from
    the live .env and POSTs to the real Slack channel. That's why
    operators were seeing "Alice Smith just accepted" pinging the team
    every test run (2026-05-12). Clear the webhook env so the slack
    notify is a no-op for all tests — production daemon path is
    untouched.
    """
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "")
    # Module-level constant is read at import time, so also patch the
    # imported reference and any cached copies in callers.
    monkeypatch.setattr("linkedin.conf.SLACK_WEBHOOK_URL", "")
    monkeypatch.setattr(
        "linkedin.notifications.slack.SLACK_WEBHOOK_URL", "",
    )


class FakeAccountSession:
    """Minimal stand-in for AccountSession — exposes django_user + campaign."""

    def __init__(self, django_user, linkedin_profile, campaign):
        self.django_user = django_user
        self.handle = django_user.username
        self.linkedin_profile = linkedin_profile
        self.campaign = campaign

    @property
    def campaigns(self):
        from linkedin.models import Campaign
        return Campaign.objects.filter(users=self.django_user)

    def ensure_browser(self):
        pass


@pytest.fixture
def fake_session(db):
    """An AccountSession-like object backed by the Django test DB."""
    from linkedin.models import Campaign, LinkedInProfile

    user = UserFactory(username="testuser")

    campaign = Campaign.objects.first()
    if campaign is None:
        campaign = Campaign.objects.create(name="LinkedIn Outreach")
    campaign.users.add(user)

    linkedin_profile, _ = LinkedInProfile.objects.get_or_create(
        user=user,
        defaults={
            "linkedin_username": "testuser@example.com",
            "linkedin_password": "testpass",
        },
    )

    return FakeAccountSession(django_user=user, linkedin_profile=linkedin_profile, campaign=campaign)


@pytest.fixture
def embeddings_db(db):
    """Marker fixture — embeddings now live in the Django test DB."""
    yield
