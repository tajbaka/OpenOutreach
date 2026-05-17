"""Schema tests for the phone-enrichment Lead fields."""
import pytest

from crm.models import Lead


@pytest.mark.django_db
def test_lead_phone_defaults_blank():
    lead = Lead.objects.create(
        first_name="Ada", linkedin_url="https://www.linkedin.com/in/ada/",
    )
    assert lead.phone == ""
    assert lead.phone_enriched_at is None


@pytest.mark.django_db
def test_lead_phone_fields_persist():
    from django.utils import timezone

    now = timezone.now()
    lead = Lead.objects.create(
        first_name="Grace", linkedin_url="https://www.linkedin.com/in/grace/",
        phone="+14155550199", phone_enriched_at=now,
    )
    lead.refresh_from_db()
    assert lead.phone == "+14155550199"
    assert lead.phone_enriched_at == now
