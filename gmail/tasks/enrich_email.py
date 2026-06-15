"""enrich_email task handler - BetterContact email lookup for Gmail fallback."""
from __future__ import annotations

import logging
from email.utils import parseaddr

from linkedin.enrichment.base import EnrichmentResult, EnrichmentStatus
from linkedin.enrichment.providers.bettercontact import BetterContactEmailProvider

logger = logging.getLogger(__name__)


def _normalize_email(value: str) -> str:
    _, addr = parseaddr(value or "")
    return addr.strip().lower()


def handle_enrich_email(task) -> EnrichmentResult | None:
    """Find one lead's email address for the Gmail fallback lane."""
    from crm.models import Deal, Lead
    from gmail.handoff import enqueue_gmail_follow_up
    from linkedin.suppression import lead_suppression_match
    from linkedin.tasks.stop_checks import automation_stop_reason

    lead_id = task.payload.get("lead_id")
    operator = task.payload.get("operator") or ""
    lead = Lead.objects.filter(pk=lead_id).first()
    if lead is None:
        logger.warning("enrich_email: lead %s not found - skipping", lead_id)
        return None
    if lead.disqualified:
        logger.info("enrich_email: lead %s disqualified - skipping", lead_id)
        return None
    if lead.email:
        logger.info("enrich_email: lead %s already has email - enqueueing Gmail", lead_id)
        enqueue_gmail_follow_up(
            lead_id=lead.id,
            operator=operator,
            deal_id=task.payload.get("deal_id"),
            sequence_name=task.payload.get("sequence_name") or "gmail_fallback",
            step_index=int(task.payload.get("step_index") or 0),
        )
        return None

    deal_id = task.payload.get("deal_id")
    deal = None
    if deal_id:
        deal = Deal.objects.filter(pk=deal_id).select_related("lead").first()
    if deal is not None:
        stop_reason = automation_stop_reason(deal)
        if stop_reason:
            logger.info("enrich_email: lead %s stopped - %s", lead_id, stop_reason)
            return None

    suppression = lead_suppression_match(lead)
    if suppression:
        logger.info(
            "enrich_email: lead %s blocked by suppression %s - skipping",
            lead_id, suppression.value,
        )
        return None

    provider = BetterContactEmailProvider()
    tried = list(lead.email_providers_tried or [])
    if provider.name in tried:
        logger.info(
            "enrich_email: lead %s - provider %s already tried, skipping",
            lead_id, provider.name,
        )
        return None

    result = provider.enrich(lead, task)
    if result.status in (EnrichmentStatus.FOUND, EnrichmentStatus.NOT_FOUND):
        if result.provider not in tried:
            tried.append(result.provider)
        lead.email_providers_tried = tried
        update_fields = ["email_providers_tried"]

        if result.status == EnrichmentStatus.FOUND and result.email:
            email = _normalize_email(result.email)
            if email:
                lead.email = email
                update_fields.append("email")

        lead.save(update_fields=update_fields)

        if result.status == EnrichmentStatus.FOUND and lead.email:
            enqueue_gmail_follow_up(
                lead_id=lead.id,
                operator=operator,
                deal_id=deal_id,
                sequence_name=task.payload.get("sequence_name") or "gmail_fallback",
                step_index=int(task.payload.get("step_index") or 0),
            )
    else:
        logger.warning(
            "enrich_email: BetterContact failed for lead %s - not recording tried",
            lead_id,
        )

    return result
