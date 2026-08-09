"""Codex queue/apply helpers for FedRAMP marketplace transition signals."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from django.utils import timezone

from linkedin.models import FedRAMPMarketplaceSignal
from linkedin.suppression import normalize_company_name

ALERT_PRIORITIES = {
    FedRAMPMarketplaceSignal.Priority.HIGH,
    FedRAMPMarketplaceSignal.Priority.URGENT,
}


@dataclass(frozen=True)
class MarketplaceAnalysisResult:
    is_relevant: bool
    should_alert: bool
    priority: str
    relevance_reason: str
    suggested_action: str
    raw: dict


def codex_review_instructions() -> str:
    return (
        "Review new official FedRAMP marketplace transitions for Boundera. The two "
        "target signals are: (1) a Program-path transition to Initial Implementation, "
        "which is a new FedRAMP 20x pipeline entrant and normally urgent; and (2) a "
        "legacy FedRAMP Ready transition, which normally has high outreach value for "
        "the Rev5 Ready to 20x motion. Confirm that the row is a real external company "
        "and not Boundera itself, a duplicate, test data, or an obviously noncommercial "
        "government-only service. Use product_context and crm_matches as evidence. Do "
        "not invent company facts. If context is missing, recommend a concrete research "
        "step. Keep the reason and action short enough for Slack. For a valid external "
        "target, should_alert should normally be true. Suggested actions should name the "
        "correct list: 20x Pipeline for Initial Implementation or Rev5 Ready for FRR."
    )


def serialize_signals_for_codex(signals: Iterable[FedRAMPMarketplaceSignal]) -> dict:
    from crm.models import Lead

    signal_list = list(signals)
    crm_leads = list(
        Lead.objects.exclude(company_name="")
        .only(
            "id", "first_name", "last_name", "company_name",
            "linkedin_url", "icp", "disqualified",
        )
        .order_by("company_name", "id")
    )
    return {
        "instructions": codex_review_instructions(),
        "schema": {
            "signal_id": "integer from input signal.id",
            "is_relevant": "boolean",
            "should_alert": "boolean; true for a valid high-value external transition",
            "priority": ["none", "low", "medium", "high", "urgent"],
            "relevance_reason": "one or two short sentences grounded in the transition",
            "suggested_action": "one concrete next action for the operator",
        },
        "signals": [
            _serialize_signal(signal, crm_leads=crm_leads)
            for signal in signal_list
        ],
    }


def load_decisions(path: str | Path) -> list[tuple[int, MarketplaceAnalysisResult]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = payload.get("decisions", [])
    else:
        raise ValueError("Decision JSON root must be a list or object.")
    if not isinstance(rows, list):
        raise ValueError("Decision JSON must be a list or an object with a decisions list.")
    decisions: list[tuple[int, MarketplaceAnalysisResult]] = []
    seen_ids: set[int] = set()
    for row in rows:
        if not isinstance(row, dict) or "signal_id" not in row:
            raise ValueError("Every marketplace decision row must include signal_id.")
        signal_id = int(row["signal_id"])
        if signal_id in seen_ids:
            raise ValueError(f"Duplicate marketplace decision for signal_id {signal_id}.")
        seen_ids.add(signal_id)
        decisions.append((signal_id, decision_from_mapping(row)))
    return decisions


def decision_from_mapping(row: dict) -> MarketplaceAnalysisResult:
    if not isinstance(row.get("is_relevant"), bool):
        raise ValueError("Marketplace decision is_relevant must be a boolean.")
    if not isinstance(row.get("should_alert"), bool):
        raise ValueError("Marketplace decision should_alert must be a boolean.")
    allowed = {value for value, _label in FedRAMPMarketplaceSignal.Priority.choices}
    priority = str(row.get("priority") or "").strip().lower()
    if priority not in allowed:
        priority = FedRAMPMarketplaceSignal.Priority.NONE
    is_relevant = row["is_relevant"]
    should_alert = (
        row["should_alert"]
        and is_relevant
        and priority in ALERT_PRIORITIES
    )
    return MarketplaceAnalysisResult(
        is_relevant=is_relevant,
        should_alert=should_alert,
        priority=priority,
        relevance_reason=str(row.get("relevance_reason") or "").strip(),
        suggested_action=str(row.get("suggested_action") or "").strip(),
        raw=dict(row),
    )


def save_marketplace_analysis(
    signal: FedRAMPMarketplaceSignal,
    result: MarketplaceAnalysisResult,
) -> FedRAMPMarketplaceSignal:
    signal.analyzed_at = timezone.now()
    signal.is_relevant = result.is_relevant
    signal.should_alert = result.should_alert
    signal.priority = result.priority
    signal.relevance_reason = result.relevance_reason
    signal.suggested_action = result.suggested_action
    signal.raw_analysis = result.raw
    signal.save(
        update_fields=[
            "analyzed_at", "is_relevant", "should_alert", "priority",
            "relevance_reason", "suggested_action", "raw_analysis", "updated_at",
        ]
    )
    return signal


def should_notify_marketplace_signal(signal: FedRAMPMarketplaceSignal) -> bool:
    return (
        signal.slack_notified_at is None
        and signal.is_relevant
        and signal.should_alert
        and signal.priority in ALERT_PRIORITIES
        and bool(signal.relevance_reason)
    )


def mark_marketplace_signals_slack_notified(
    signals: Iterable[FedRAMPMarketplaceSignal],
) -> None:
    ids = [signal.id for signal in signals]
    if not ids:
        return
    now = timezone.now()
    FedRAMPMarketplaceSignal.objects.filter(id__in=ids).update(
        slack_notified_at=now,
        updated_at=now,
    )


def group_marketplace_signals_for_alert(
    signals: Iterable[FedRAMPMarketplaceSignal],
) -> list[list[FedRAMPMarketplaceSignal]]:
    groups: dict[tuple[str, str], list[FedRAMPMarketplaceSignal]] = {}
    for signal in signals:
        provider_key = normalize_company_name(signal.provider_name) or signal.provider_name.casefold()
        groups.setdefault((signal.signal_type, provider_key), []).append(signal)
    return [
        sorted(group, key=lambda item: (item.recorded_at or item.first_seen_at, item.id))
        for _key, group in sorted(groups.items())
    ]


def _serialize_signal(signal: FedRAMPMarketplaceSignal, *, crm_leads=None) -> dict:
    return {
        "id": signal.id,
        "signal_type": signal.signal_type,
        "signal_label": signal.get_signal_type_display(),
        "expected_icp_bucket": signal.icp_bucket,
        "provider_name": signal.provider_name,
        "offering_name": signal.offering_name,
        "product_id": signal.product_id,
        "certification_path": signal.certification_path,
        "from_status": signal.from_status,
        "to_status": signal.to_status,
        "transition_at": signal.transition_at.isoformat() if signal.transition_at else "",
        "recorded_at": signal.recorded_at.isoformat() if signal.recorded_at else "",
        "marketplace_url": signal.marketplace_url,
        "source_url": signal.source_url,
        "source_kind": signal.source_kind,
        "product_context": signal.product_context,
        "crm_matches": crm_matches_for_marketplace_signal(
            signal,
            leads=crm_leads,
        ),
    }


def crm_matches_for_marketplace_signal(
    signal: FedRAMPMarketplaceSignal,
    *,
    leads=None,
) -> list[dict]:
    from crm.models import Deal, Lead

    target = normalize_company_name(signal.provider_name)
    if not target:
        return []
    matched_leads = []
    lead_rows = (
        leads
        if leads is not None
        else Lead.objects.exclude(company_name="").order_by("company_name", "id")
    )
    for lead in lead_rows:
        candidate = normalize_company_name(lead.company_name)
        if not candidate:
            continue
        if candidate == target or (
            min(len(candidate), len(target)) >= 6
            and (candidate in target or target in candidate)
        ):
            matched_leads.append(lead)
        if len(matched_leads) >= 12:
            break
    if not matched_leads:
        return []

    campaigns_by_lead: dict[int, list[dict]] = {}
    deals = (
        Deal.objects.filter(lead_id__in=[lead.id for lead in matched_leads])
        .select_related("campaign")
        .order_by("lead_id", "campaign_id")
    )
    for deal in deals:
        campaigns_by_lead.setdefault(deal.lead_id, []).append({
            "campaign_id": deal.campaign_id,
            "campaign_name": deal.campaign.name,
            "campaign_status": deal.campaign.status,
            "deal_state": deal.state,
        })
    return [
        {
            "lead_id": lead.id,
            "full_name": lead.full_name,
            "company_name": lead.company_name,
            "linkedin_url": lead.linkedin_url,
            "icp": lead.icp,
            "disqualified": lead.disqualified,
            "campaigns": campaigns_by_lead.get(lead.id, []),
        }
        for lead in matched_leads
    ]
