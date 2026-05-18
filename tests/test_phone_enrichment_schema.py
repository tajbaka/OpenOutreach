"""Schema tests for the phone-enrichment Lead fields (multi-number)."""
import pytest

from crm.models import Lead


@pytest.mark.django_db
def test_lead_phone_fields_default_empty():
    lead = Lead.objects.create(
        first_name="Ada", linkedin_url="https://www.linkedin.com/in/ada/",
    )
    assert lead.phones == []
    assert lead.phone_providers_tried == []
    assert lead.phone_numbers == []


@pytest.mark.django_db
def test_lead_phones_persist_and_phone_numbers_property():
    lead = Lead.objects.create(
        first_name="Grace", linkedin_url="https://www.linkedin.com/in/grace/",
        phones=[
            {"number": "+14155550199", "provider": "leadmagic",
             "found_at": "2026-05-18T00:00:00"},
            {"number": "+14155550123", "provider": "bettercontact",
             "found_at": "2026-05-18T01:00:00"},
        ],
        phone_providers_tried=["leadmagic", "bettercontact"],
    )
    lead.refresh_from_db()
    assert len(lead.phones) == 2
    assert lead.phone_numbers == ["+14155550199", "+14155550123"]
    assert lead.phone_providers_tried == ["leadmagic", "bettercontact"]
