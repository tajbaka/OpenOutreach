from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from django.db import transaction

from crm.models import ClosingReason, Deal, Lead
from linkedin.db.urls import url_to_public_id
from linkedin.enums import ProfileState
from linkedin.models import Campaign

ALLOWED_ICP_VALUES = {
    "csp",
    "assessor",
    "advisor_partner",
    "channel",
    "cmmc_buyer",
    "other",
    "not_relevant",
}

ICP_TO_LEAD_BUCKET = {
    "csp": "CSPs",
    "assessor": "3PAOs/Assessors",
    "advisor_partner": "Advisors",
    "channel": "Channel",
    "cmmc_buyer": "CMMC Buyers",
}

ALLOWED_CONFIDENCE_VALUES = {"low", "medium", "high"}


@dataclass(frozen=True)
class LeadQualificationDecision:
    lead_id: int
    campaign_id: int | None
    qualified: bool
    confidence: str
    icp: str
    reason: str
    suggested_action: str
    raw: dict


@dataclass(frozen=True)
class AppliedLeadDecision:
    lead_id: int
    campaign_id: int
    qualified: bool
    deal_id: int
    state: str


def codex_review_instructions() -> str:
    return (
        "Review these LinkedIn lead profiles for Boundera. Boundera sells "
        "FedRAMP 20x, FedRAMP/GRC, and CMMC automation. Qualify leads who "
        "show credible buyer, influencer, channel, assessor, advisor, or "
        "defense supplier relevance. Reject generic cybersecurity profiles, "
        "students, unrelated IT services, recruiters, non-US public-sector "
        "irrelevance, and profiles with no usable signal. Use the explicit "
        "ICP vocabulary only. Keep reasons short and concrete, grounded in "
        "the profile/campaign signal. This queue is offline: write decisions "
        "JSON only; do not message leads."
    )


def serialize_leads_for_codex(rows: Iterable[tuple[Lead, Campaign]]) -> dict:
    candidates = [_serialize_candidate(lead, campaign) for lead, campaign in rows]
    return {
        "instructions": codex_review_instructions(),
        "schema": {
            "lead_id": "integer from input lead.id",
            "campaign_id": (
                "integer from input campaign.id; include this when present so "
                "apply is campaign-scoped and unambiguous"
            ),
            "qualified": "boolean",
            "confidence": ["low", "medium", "high"],
            "icp": [
                "csp",
                "assessor",
                "advisor_partner",
                "channel",
                "cmmc_buyer",
                "other",
                "not_relevant",
            ],
            "reason": "short reason grounded in the profile and campaign",
            "suggested_action": "short next action for qualified leads; blank is OK for rejected leads",
        },
        "candidates": candidates,
    }


def qualification_rows(
    *,
    campaign_id: int | None = None,
    active_only: bool = True,
    limit: int | None = None,
) -> list[tuple[Lead, Campaign]]:
    campaigns = Campaign.objects.all().order_by("id")
    if campaign_id is not None:
        campaigns = campaigns.filter(pk=campaign_id)
    elif active_only:
        campaigns = campaigns.filter(status=Campaign.Status.ACTIVE)

    rows: list[tuple[Lead, Campaign]] = []
    for campaign in campaigns:
        leads = (
            Lead.objects.filter(disqualified=False)
            .exclude(deal__campaign=campaign)
            .order_by("creation_date", "id")
        )
        for lead in leads.iterator(chunk_size=200):
            if lead.public_identifier or url_to_public_id(lead.linkedin_url):
                rows.append((lead, campaign))
                if limit is not None and len(rows) >= limit:
                    return rows
    return rows


def load_decisions(path: str | Path) -> list[LeadQualificationDecision]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = payload if isinstance(payload, list) else payload.get("decisions", [])
    if not isinstance(rows, list):
        raise ValueError("Decision JSON must be a list or an object with a decisions list.")
    return [decision_from_mapping(row) for row in rows]


def decision_from_mapping(row: dict) -> LeadQualificationDecision:
    if "lead_id" not in row:
        raise ValueError("Every lead decision row must include lead_id.")
    confidence = _normalize_choice(
        str(row.get("confidence", "")),
        ALLOWED_CONFIDENCE_VALUES,
        "medium",
    )
    icp = _normalize_choice(str(row.get("icp", "")), ALLOWED_ICP_VALUES, "not_relevant")
    qualified = bool(row.get("qualified"))
    if not qualified and icp != "not_relevant":
        icp = "not_relevant"
    if qualified and icp == "not_relevant":
        raise ValueError(f"Qualified lead {row.get('lead_id')} cannot use icp=not_relevant.")
    campaign_id = row.get("campaign_id")
    return LeadQualificationDecision(
        lead_id=int(row["lead_id"]),
        campaign_id=int(campaign_id) if campaign_id not in (None, "") else None,
        qualified=qualified,
        confidence=confidence,
        icp=icp,
        reason=str(row.get("reason") or "").strip(),
        suggested_action=str(row.get("suggested_action") or "").strip(),
        raw=dict(row),
    )


@transaction.atomic
def apply_decision(
    decision: LeadQualificationDecision,
    *,
    default_campaign_id: int | None = None,
    positive_state: ProfileState = ProfileState.QUALIFIED,
) -> AppliedLeadDecision:
    campaign = _resolve_campaign(decision, default_campaign_id=default_campaign_id)
    lead = Lead.objects.get(pk=decision.lead_id)
    reason = _decision_reason(decision)
    deal, _created = Deal.objects.get_or_create(
        lead=lead,
        campaign=campaign,
        defaults={
            "state": ProfileState.QUALIFIED,
            "reason": reason,
        },
    )
    _assert_safe_to_review(deal)

    if decision.qualified:
        bucket = ICP_TO_LEAD_BUCKET.get(decision.icp)
        if bucket:
            lead.icp = bucket
            lead.save(update_fields=["icp"])
        deal.state = positive_state
        deal.closing_reason = ""
        deal.reason = reason
    else:
        deal.state = ProfileState.FAILED
        deal.closing_reason = ClosingReason.DISQUALIFIED
        deal.reason = reason or "Codex lead review: not relevant"
    deal.save(update_fields=["state", "closing_reason", "reason", "update_date"])
    return AppliedLeadDecision(
        lead_id=lead.pk,
        campaign_id=campaign.pk,
        qualified=decision.qualified,
        deal_id=deal.pk,
        state=deal.state,
    )


def _assert_safe_to_review(deal: Deal) -> None:
    if deal.state in {ProfileState.PENDING, ProfileState.CONNECTED, ProfileState.COMPLETED}:
        raise ValueError(
            f"Refusing to apply lead review to deal_id={deal.pk} at state={deal.state}; "
            "this lead is already in live outreach.",
        )
    if deal.state == ProfileState.FAILED and deal.closing_reason != ClosingReason.DISQUALIFIED:
        raise ValueError(
            f"Refusing to apply lead review to deal_id={deal.pk} with "
            f"closing_reason={deal.closing_reason!r}.",
        )


def _serialize_candidate(lead: Lead, campaign: Campaign) -> dict:
    profile = _profile_json(lead)
    headline = profile.get("headline", "") or ""
    location = profile.get("location_name", "") or ""
    positions = profile.get("positions", []) or []
    current_position = positions[0] if positions else {}
    return {
        "lead_id": lead.pk,
        "campaign_id": campaign.pk,
        "public_identifier": lead.public_identifier,
        "linkedin_url": lead.linkedin_url,
        "first_name": lead.first_name,
        "last_name": lead.last_name,
        "company": lead.company_name,
        "headline": headline,
        "title": current_position.get("title", "") or "",
        "location": location,
        "current_icp": lead.icp,
        "current_state": "awaiting_qualification",
        "profile_text": _profile_text(profile),
        "profile": {
            "summary": profile.get("summary", "") or "",
            "industry": (profile.get("industry") or {}).get("name", "") if isinstance(profile.get("industry"), dict) else "",
            "positions": positions[:5],
            "educations": (profile.get("educations") or [])[:5],
        },
        "campaign": {
            "id": campaign.pk,
            "name": campaign.name,
            "status": campaign.status,
            "objective": campaign.campaign_objective,
            "product_docs": campaign.product_docs,
        },
    }


def _resolve_campaign(
    decision: LeadQualificationDecision,
    *,
    default_campaign_id: int | None,
) -> Campaign:
    campaign_id = decision.campaign_id or default_campaign_id
    if campaign_id is not None:
        return Campaign.objects.get(pk=campaign_id)

    lead = Lead.objects.get(pk=decision.lead_id)
    campaigns = list(
        Campaign.objects.filter(status=Campaign.Status.ACTIVE)
        .exclude(deals__lead=lead)
        .order_by("id")
    )
    if len(campaigns) != 1:
        raise ValueError(
            f"Decision for lead_id={decision.lead_id} must include campaign_id "
            f"(found {len(campaigns)} active candidate campaigns).",
        )
    return campaigns[0]


def _decision_reason(decision: LeadQualificationDecision) -> str:
    prefix = (
        f"Codex lead review: confidence={decision.confidence}; "
        f"icp={decision.icp}."
    )
    if decision.reason:
        return f"{prefix} {decision.reason}"
    return prefix


def _profile_json(lead: Lead) -> dict:
    if not lead.description:
        return {}
    try:
        value = json.loads(lead.description)
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _profile_text(profile: dict) -> str:
    if not profile:
        return ""
    from linkedin.ml.profile_text import build_profile_text

    return build_profile_text({"profile": profile})


def _normalize_choice(value: str, allowed: set[str], default: str) -> str:
    cleaned = (value or "").strip().lower()
    return cleaned if cleaned in allowed else default
