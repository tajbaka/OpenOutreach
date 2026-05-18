"""enrich_phone task handler — runs the phone-enrichment waterfall.

Unlike the daemon-loop handlers (handle_connect / handle_follow_up) this
takes NO `session` argument: it runs in the EnrichmentWorker thread, does
HTTP only, and never touches the browser. The EnrichmentWorker sets the
task's final status from the returned EnrichmentResult.
"""
from __future__ import annotations

import logging

from django.utils import timezone

from linkedin.enrichment.base import EnrichmentResult, EnrichmentStatus
from linkedin.enrichment.waterfall import PROVIDERS_BY_NAME, run_waterfall
from linkedin.notifications.slack import notify_phone_enriched

logger = logging.getLogger(__name__)


def handle_enrich_phone(task) -> EnrichmentResult | None:
    """Enrich one lead's phone number.

    Returns the waterfall EnrichmentResult, or None when the task was a
    no-op skip (lead missing / already enriched / disqualified). The
    EnrichmentWorker treats None and FOUND/NOT_FOUND as `completed`, and
    API_FAILURE as `failed`.
    """
    from crm.models import Lead

    lead_id = task.payload.get("lead_id")
    lead = Lead.objects.filter(pk=lead_id).first()
    if lead is None:
        logger.warning("enrich_phone: lead %s not found — skipping", lead_id)
        return None
    if lead.phone_enriched_at is not None:
        logger.info("enrich_phone: lead %s already enriched — skipping", lead_id)
        return None
    if lead.disqualified:
        logger.info("enrich_phone: lead %s disqualified — skipping", lead_id)
        return None

    provider = task.payload.get("provider", "waterfall")
    if provider == "waterfall":
        result = run_waterfall(lead, task)
    else:
        chosen = PROVIDERS_BY_NAME.get(provider)
        if chosen is None:
            logger.warning(
                "enrich_phone: unknown provider %r — running full waterfall",
                provider,
            )
            result = run_waterfall(lead, task)
        else:
            result = run_waterfall(lead, task, chain=[chosen])

    if result.status == EnrichmentStatus.FOUND:
        lead.phone = result.phone or ""
        lead.phone_enriched_at = timezone.now()
        lead.save(update_fields=["phone", "phone_enriched_at"])
        notify_phone_enriched(lead=lead, result=result)
    elif result.status == EnrichmentStatus.NOT_FOUND:
        # Stamp so we never re-enrich a confirmed empty result.
        lead.phone_enriched_at = timezone.now()
        lead.save(update_fields=["phone_enriched_at"])
        notify_phone_enriched(lead=lead, result=result)
    else:  # API_FAILURE — do NOT stamp; the lead's next reply re-attempts.
        logger.warning(
            "enrich_phone: all providers failed for lead %s — not stamping", lead_id,
        )

    return result
