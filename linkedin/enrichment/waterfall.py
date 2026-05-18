"""Phone-enrichment provider waterfall.

Iterates PROVIDER_CHAIN in order. FOUND or NOT_FOUND is terminal — return
immediately (a NOT_FOUND from BetterContact, itself a 20+ provider waterfall,
is authoritative). API_FAILURE escalates to the next provider. If every
provider fails, the last API_FAILURE result is returned.
"""
from __future__ import annotations

import logging

from linkedin.enrichment.base import EnrichmentResult, EnrichmentStatus
from linkedin.enrichment.providers.bettercontact import BetterContactProvider
from linkedin.enrichment.providers.leadmagic import LeadMagicProvider
from linkedin.enrichment.providers.prospeo import ProspeoProvider

logger = logging.getLogger(__name__)

# Order matters — see docs/superpowers/specs/2026-05-17-phone-enrichment-design.md.
# To add a provider: implement the PhoneProvider protocol and append it here.
PROVIDER_CHAIN = [
    BetterContactProvider(),
    LeadMagicProvider(),
    ProspeoProvider(),
]

# Name → provider, for single-provider routing (the Slack "X only" options).
# See linkedin/tasks/enrich_phone.py and api/slack_enrich.py.
PROVIDERS_BY_NAME = {p.name: p for p in PROVIDER_CHAIN}


def run_waterfall(lead, task, chain=None) -> EnrichmentResult:
    """Run the provider chain for one lead. `chain` is injectable for tests."""
    providers = chain if chain is not None else PROVIDER_CHAIN
    last = EnrichmentResult(status=EnrichmentStatus.API_FAILURE, provider="none")
    for provider in providers:
        result = provider.enrich(lead, task)
        if result.status in (EnrichmentStatus.FOUND, EnrichmentStatus.NOT_FOUND):
            logger.info(
                "Enrichment %s via %s", result.status.value, provider.name,
            )
            return result
        logger.warning("Provider %s failed — escalating", provider.name)
        last = result
    logger.warning("All enrichment providers failed")
    return last
