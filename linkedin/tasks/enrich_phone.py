"""enrich_phone task handler — runs the phone-enrichment waterfall.

Unlike the daemon-loop handlers (handle_connect / handle_follow_up) this
takes NO `session` argument: it runs in the EnrichmentWorker thread, does
HTTP only, and never touches the browser. The EnrichmentWorker sets the
task's final status from the returned EnrichmentResult.

A lead carries multiple numbers — `Lead.phones` accrues one entry per
provider that returns a hit. Provider-specific requests return a cached
`Lead.phones` entry before calling the API. `Lead.phone_providers_tried`
records every provider that gave a definitive answer (FOUND or NOT_FOUND),
so a provider that already answered is never billed again; API_FAILURE is
not recorded and stays retryable.
"""
from __future__ import annotations

import logging
import re

from django.utils import timezone

from linkedin.enrichment.base import EnrichmentResult, EnrichmentStatus
from linkedin.enrichment.waterfall import (
    PROVIDER_CHAIN,
    PROVIDERS_BY_NAME,
    run_waterfall,
)
from linkedin.notifications.slack import notify_phone_enriched

logger = logging.getLogger(__name__)


def _normalize_phone(raw: str) -> str:
    """Canonical comparison/storage key for a phone number — a leading '+'
    followed by digits only. Collapses provider formatting differences
    ('+1 904 945 0716' vs '+19049450716') so the same number from two
    providers dedups to a single entry instead of two look-alike rows.
    """
    digits = re.sub(r"\D", "", raw or "")
    return f"+{digits}" if digits else ""


def _existing_phone_for_provider(lead, provider: str) -> str:
    """Return a stored number for this provider, if one exists.

    This covers legacy/manual rows where `phones` was populated before
    `phone_providers_tried` was stamped. Provider-specific Slack requests
    should return the cached answer instead of billing the same provider again.
    """
    if not provider or provider == "waterfall":
        return ""
    for entry in lead.phones or []:
        if entry.get("provider") == provider:
            return _normalize_phone(entry.get("number", ""))
    return ""


def _cached_result_for_provider(
    lead,
    provider: str,
    tried: list[str],
) -> EnrichmentResult | None:
    existing_phone = _existing_phone_for_provider(lead, provider)
    if not existing_phone:
        return None

    if provider not in tried:
        tried.append(provider)
        lead.phone_providers_tried = tried
        lead.save(update_fields=["phone_providers_tried"])

    result = EnrichmentResult(
        status=EnrichmentStatus.FOUND,
        provider=provider,
        phone=existing_phone,
        raw={"cached": True},
    )
    notify_phone_enriched(lead=lead, result=result)
    logger.info(
        "enrich_phone: lead %s — returning cached %s phone",
        lead.id, provider,
    )
    return result


def handle_enrich_phone(task) -> EnrichmentResult | None:
    """Enrich one lead's phone number.

    Returns the waterfall EnrichmentResult, or None when the task was a
    no-op skip (lead missing / disqualified / the requested provider(s)
    already tried). The EnrichmentWorker treats None and FOUND/NOT_FOUND as
    `completed`, and API_FAILURE as `failed`.
    """
    from crm.models import Lead

    lead_id = task.payload.get("lead_id")
    lead = Lead.objects.filter(pk=lead_id).first()
    if lead is None:
        logger.warning("enrich_phone: lead %s not found — skipping", lead_id)
        return None
    if lead.disqualified:
        logger.info("enrich_phone: lead %s disqualified — skipping", lead_id)
        return None

    provider = task.payload.get("provider", "waterfall")
    tried = list(lead.phone_providers_tried or [])

    # Per-provider skip — never bill a provider that already answered.
    if provider == "waterfall":
        for candidate in PROVIDER_CHAIN:
            cached = _cached_result_for_provider(lead, candidate.name, tried)
            if cached is not None:
                return cached
        if all(p.name in tried for p in PROVIDER_CHAIN):
            logger.info(
                "enrich_phone: lead %s — all providers already tried, skipping",
                lead_id,
            )
            return None
    else:
        cached = _cached_result_for_provider(lead, provider, tried)
        if cached is not None:
            return cached
    if provider != "waterfall" and provider in tried:
        logger.info(
            "enrich_phone: lead %s — provider %s already tried, skipping",
            lead_id, provider,
        )
        return None

    if provider == "waterfall":
        result = run_waterfall(
            lead,
            task,
            chain=[p for p in PROVIDER_CHAIN if p.name not in tried],
        )
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

    if result.status in (EnrichmentStatus.FOUND, EnrichmentStatus.NOT_FOUND):
        # Record the provider that gave the definitive answer so it is not
        # re-run. (For a waterfall this is the terminal provider; earlier
        # providers that API_FAILED stay retryable, which is correct.)
        if result.provider not in tried:
            tried.append(result.provider)
        lead.phone_providers_tried = tried
        update_fields = ["phone_providers_tried"]

        if result.status == EnrichmentStatus.FOUND and result.phone:
            # Store and dedup on the normalized form so the same number from
            # two providers collapses to one entry.
            number = _normalize_phone(result.phone)
            existing = {_normalize_phone(e.get("number", "")) for e in lead.phones}
            if number and number not in existing:
                lead.phones = list(lead.phones) + [{
                    "number": number,
                    "provider": result.provider,
                    "found_at": timezone.now().isoformat(),
                }]
                update_fields.append("phones")

        lead.save(update_fields=update_fields)
        notify_phone_enriched(lead=lead, result=result)
    else:  # API_FAILURE — not recorded; the provider stays retryable.
        logger.warning(
            "enrich_phone: all providers failed for lead %s — not recording",
            lead_id,
        )

    return result
