import pytest

from linkedin.enums import ProfileState
from linkedin.setup.seeds import create_seed_leads_from_csv


@pytest.mark.django_db
def test_create_seed_leads_from_csv_supports_ready_to_connect(fake_session):
    from crm.models import Deal

    leads = [
        {
            "url": "https://www.linkedin.com/in/alice/",
            "public_id": "alice",
            "first_name": "Alice",
            "last_name": "Smith",
            "company_name": "Acme",
        },
    ]

    created = create_seed_leads_from_csv(
        fake_session.campaign,
        leads,
        initial_state=ProfileState.READY_TO_CONNECT,
    )

    assert created == 1
    deal = Deal.objects.get(
        lead__linkedin_url="https://www.linkedin.com/in/alice/",
        campaign=fake_session.campaign,
    )
    assert deal.state == ProfileState.READY_TO_CONNECT


@pytest.mark.django_db
def test_seed_icp_uses_full_audience_limit_without_reclassifying(fake_session):
    from crm.models import Lead
    from django.core.exceptions import ValidationError

    key = "a" * 160
    entry = {"public_id": "qa-long-icp", "icp": key}
    create_seed_leads_from_csv(fake_session.campaign, [entry])
    lead = Lead.objects.get(public_identifier=entry["public_id"])
    assert lead.icp == key
    assert lead.role_tag == ""
    create_seed_leads_from_csv(fake_session.campaign, [{**entry, "icp": "CSPs"}])
    lead.refresh_from_db()
    assert lead.icp == key  # Imports still fill blank fields only.
    with pytest.raises(ValidationError):
        Lead._meta.get_field("icp").clean("a" * 161, lead)
