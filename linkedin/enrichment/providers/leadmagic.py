"""LeadMagic phone-enrichment provider (synchronous, LinkedIn-URL native)."""
from __future__ import annotations

import logging

from linkedin.conf import ENRICHMENT_HTTP_TIMEOUT_SECONDS, LEADMAGIC_API_KEY
from linkedin.enrichment.base import EnrichmentResult, EnrichmentStatus
from linkedin.enrichment.http import HttpError, post_json

logger = logging.getLogger(__name__)

_URL = "https://api.leadmagic.io/mobile-finder"


class LeadMagicProvider:
    name = "leadmagic"

    def enrich(self, lead, task) -> EnrichmentResult:
        if not LEADMAGIC_API_KEY:
            logger.warning("LeadMagic: no API key configured — API_FAILURE")
            return EnrichmentResult(status=EnrichmentStatus.API_FAILURE, provider=self.name)
        try:
            resp = post_json(
                _URL,
                headers={"X-API-Key": LEADMAGIC_API_KEY},
                payload={"profile_url": lead.linkedin_url},
                timeout=ENRICHMENT_HTTP_TIMEOUT_SECONDS,
            )
        except HttpError as exc:
            logger.warning("LeadMagic API failure: %s", exc)
            return EnrichmentResult(status=EnrichmentStatus.API_FAILURE, provider=self.name)

        phone = resp.get("mobile_number")
        if phone:
            return EnrichmentResult(
                status=EnrichmentStatus.FOUND, provider=self.name,
                phone=str(phone), raw=resp,
            )
        return EnrichmentResult(
            status=EnrichmentStatus.NOT_FOUND, provider=self.name, raw=resp,
        )
