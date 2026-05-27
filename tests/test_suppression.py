from types import SimpleNamespace

from linkedin.models import OutreachSuppression
from linkedin.suppression import normalize_company_name, suppression_matches_lead


def _suppression(kind, value, **overrides):
    defaults = {
        "Kind": OutreachSuppression.Kind,
        "kind": kind,
        "value": value,
        "normalized_value": normalize_company_name(value)
        if kind == OutreachSuppression.Kind.COMPANY
        else "".join(ch for ch in value.lower() if ch.isalnum()),
        "normalized_aliases": [],
        "domain": "",
        "email": "",
        "linkedin_url": "",
        "public_identifier": "",
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_company_suppression_matches_exact_normalized_company():
    suppression = _suppression(OutreachSuppression.Kind.COMPANY, "Vanta")
    lead = SimpleNamespace(company_name="Vanta Inc.", email="")

    assert suppression_matches_lead(suppression, lead)


def test_company_suppression_does_not_substring_match_company():
    suppression = _suppression(OutreachSuppression.Kind.COMPANY, "Vanta")
    lead = SimpleNamespace(company_name="Advantage Solutions", email="")

    assert not suppression_matches_lead(suppression, lead)


def test_lead_suppression_domain_does_not_block_whole_company():
    suppression = _suppression(
        OutreachSuppression.Kind.LEAD,
        "Jane Doe",
        normalized_value="janedoe",
        domain="vanta.com",
    )
    lead = SimpleNamespace(
        first_name="Unrelated",
        last_name="Person",
        company_name="Vanta",
        email="unrelated@vanta.com",
        linkedin_url="",
        public_identifier="",
    )

    assert not suppression_matches_lead(suppression, lead)
