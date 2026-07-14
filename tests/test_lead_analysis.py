from __future__ import annotations

import json

import pytest
from django.core.management import call_command

from crm.models import ClosingReason, Deal, Lead
from linkedin.enums import ProfileState
from linkedin.lead_analysis import (
    apply_decision,
    decision_from_mapping,
    load_decisions,
    serialize_leads_for_codex,
)
from linkedin.models import Campaign
from tests.factories import UserFactory


def _campaign(name="Codex Review"):
    return Campaign.objects.create(
        name=name,
        user=UserFactory(),
        product_docs="Boundera automates FedRAMP 20x and CMMC evidence workflows.",
        campaign_objective="Find defense suppliers and FedRAMP/CMMC partners.",
    )


def _lead(**overrides):
    profile = {
        "public_identifier": "josh-cramer",
        "headline": "Federal capture and GTM strategy leader",
        "summary": "Helps defense suppliers with public sector growth.",
        "location_name": "Washington, DC",
        "industry": {"name": "Defense and Space"},
        "positions": [{
            "title": "VP Federal Strategy",
            "company_name": "Acme Defense",
            "location": "Washington, DC",
            "description": "Federal capture, partner strategy, and DoD supplier work.",
        }],
        "educations": [{"school_name": "State", "degree": "MBA"}],
    }
    data = {
        "first_name": "Josh",
        "last_name": "Cramer",
        "company_name": "Acme Defense",
        "linkedin_url": "https://www.linkedin.com/in/josh-cramer/",
        "public_identifier": "josh-cramer",
        "description": json.dumps(profile),
    }
    data.update(overrides)
    return Lead.objects.create(**data)


@pytest.mark.django_db
def test_serialize_leads_for_codex_includes_campaign_and_profile_context():
    campaign = _campaign()
    lead = _lead()

    payload = serialize_leads_for_codex([(lead, campaign)])

    row = payload["candidates"][0]
    assert row["lead_id"] == lead.pk
    assert row["campaign_id"] == campaign.pk
    assert row["linkedin_url"] == "https://www.linkedin.com/in/josh-cramer/"
    assert row["headline"] == "Federal capture and GTM strategy leader"
    assert "federal capture" in row["profile_text"]
    assert row["campaign"]["objective"] == campaign.campaign_objective
    assert "cmmc_buyer" in payload["schema"]["icp"]


def test_decision_from_mapping_validates_icp_vocab():
    decision = decision_from_mapping({
        "lead_id": 123,
        "campaign_id": 456,
        "qualified": True,
        "confidence": "HIGH",
        "icp": "CHANNEL",
        "reason": "Federal GTM partner signal.",
        "suggested_action": "Connect with channel angle.",
    })

    assert decision.lead_id == 123
    assert decision.campaign_id == 456
    assert decision.qualified is True
    assert decision.confidence == "high"
    assert decision.icp == "channel"


def test_decision_from_mapping_rejects_qualified_not_relevant():
    with pytest.raises(ValueError, match="not_relevant"):
        decision_from_mapping({
            "lead_id": 123,
            "qualified": True,
            "icp": "not_relevant",
        })


@pytest.mark.django_db
def test_apply_decision_qualifies_campaign_deal_and_stamps_icp():
    campaign = _campaign()
    lead = _lead()
    decision = decision_from_mapping({
        "lead_id": lead.pk,
        "campaign_id": campaign.pk,
        "qualified": True,
        "confidence": "high",
        "icp": "channel",
        "reason": "Federal GTM strategy signal.",
        "suggested_action": "Connect with federal GTM angle.",
    })

    applied = apply_decision(decision, positive_state=ProfileState.READY_TO_CONNECT)

    lead.refresh_from_db()
    deal = Deal.objects.get(pk=applied.deal_id)
    assert applied.qualified is True
    assert lead.icp == "Channel"
    assert deal.campaign == campaign
    assert deal.state == ProfileState.READY_TO_CONNECT
    assert deal.closing_reason == ""
    assert "Federal GTM strategy signal" in deal.reason


@pytest.mark.django_db
def test_apply_decision_rejects_campaign_scoped_without_global_disqualification():
    campaign = _campaign()
    lead = _lead()
    decision = decision_from_mapping({
        "lead_id": lead.pk,
        "campaign_id": campaign.pk,
        "qualified": False,
        "confidence": "high",
        "icp": "not_relevant",
        "reason": "No FedRAMP, CMMC, federal, assessor, or partner signal.",
        "suggested_action": "",
    })

    applied = apply_decision(decision)

    lead.refresh_from_db()
    deal = Deal.objects.get(pk=applied.deal_id)
    assert lead.disqualified is False
    assert deal.state == ProfileState.FAILED
    assert deal.closing_reason == ClosingReason.DISQUALIFIED
    assert "No FedRAMP" in deal.reason


@pytest.mark.django_db
def test_apply_decision_refuses_to_overwrite_live_outreach_deal():
    campaign = _campaign()
    lead = _lead()
    Deal.objects.create(lead=lead, campaign=campaign, state=ProfileState.PENDING)
    decision = decision_from_mapping({
        "lead_id": lead.pk,
        "campaign_id": campaign.pk,
        "qualified": False,
        "confidence": "high",
        "icp": "not_relevant",
        "reason": "Stale review file.",
        "suggested_action": "",
    })

    with pytest.raises(ValueError, match="live outreach"):
        apply_decision(decision)


def test_load_decisions_requires_lead_id(tmp_path):
    path = tmp_path / "decisions.json"
    path.write_text(json.dumps({"decisions": [{"qualified": True}]}))

    with pytest.raises(ValueError, match="lead_id"):
        load_decisions(path)


@pytest.mark.django_db
def test_analyze_lead_qualification_exports_queue(tmp_path):
    campaign = _campaign()
    lead = _lead()
    out = tmp_path / "queue.json"

    call_command("analyze_lead_qualification", campaign=campaign.pk, output=str(out))

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["candidates"][0]["lead_id"] == lead.pk
    assert payload["candidates"][0]["campaign_id"] == campaign.pk
    assert "schema" in payload


@pytest.mark.django_db
def test_analyze_lead_qualification_applies_decision(tmp_path):
    campaign = _campaign()
    lead = _lead()
    decisions = tmp_path / "decisions.json"
    decisions.write_text(json.dumps({
        "decisions": [{
            "lead_id": lead.pk,
            "campaign_id": campaign.pk,
            "qualified": True,
            "confidence": "high",
            "icp": "cmmc_buyer",
            "reason": "Defense supplier CMMC signal.",
            "suggested_action": "Connect with CMMC readiness angle.",
        }],
    }))

    call_command("analyze_lead_qualification", apply_json=str(decisions), ready=True)

    lead.refresh_from_db()
    deal = Deal.objects.get(lead=lead, campaign=campaign)
    assert lead.icp == "CMMC Buyers"
    assert deal.state == ProfileState.READY_TO_CONNECT
