import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from crm.models import Lead
from crm.models.lead import LEAD_ROLE_TAG_VALUES, normalize_lead_role_tag


EXPECTED_ROLE_TAGS = {
    "Founder/CEO",
    "CFO/Finance",
    "COO/Operations",
    "CRO/Revenue",
    "Product Executive",
    "Technology/Engineering Executive",
    "CIO/Internal IT Executive",
    "Security/Trust Executive",
    "Product/Engineering N-1",
    "Security/Compliance N-1",
    "Compliance/Risk/Privacy Executive",
    "FedRAMP Owner/Operator",
    "GRC Manager/Lead",
    "GRC Engineer",
    "GRC Analyst/Practitioner",
    "Federal/Public Sector Executive",
    "Federal Sales/BD IC",
    "Federal Solutions Engineer/Architect",
    "Public Sector Partnerships/Alliances/CS",
    "Field CTO/CISO/Technical Evangelist",
}


def test_lead_role_tag_vocabulary_is_canonical():
    assert set(LEAD_ROLE_TAG_VALUES) == EXPECTED_ROLE_TAGS
    assert set(Lead.RoleTag.values) == EXPECTED_ROLE_TAGS


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", ""),
        ("  ", ""),
        ("CFO/Finance", "CFO/Finance"),
        ("  cfo/finance  ", "CFO/Finance"),
        ("FedRAMP   Owner/Operator", "FedRAMP Owner/Operator"),
    ],
)
def test_normalize_lead_role_tag_returns_canonical_values(raw, expected):
    assert normalize_lead_role_tag(raw) == expected


def test_normalize_lead_role_tag_rejects_unknown_persona():
    with pytest.raises(ValueError, match="unknown Role Tag"):
        normalize_lead_role_tag("Finance-ish")


def test_lead_role_tag_model_validation_rejects_unknown_persona():
    lead = Lead(role_tag="Finance-ish")

    with pytest.raises(ValidationError, match="not a valid choice"):
        lead.full_clean()


@pytest.mark.django_db
def test_lead_role_tag_database_constraint_rejects_unknown_persona():
    with pytest.raises(IntegrityError), transaction.atomic():
        Lead.objects.create(role_tag="Finance-ish")
