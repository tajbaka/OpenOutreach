"""Prospeo phone-enrichment provider (synchronous — last resort in the chain).

Uses POST /enrich-person; Prospeo retired the older /mobile-finder endpoint
on 2026-03-01.
"""
from __future__ import annotations

import logging

from linkedin.conf import ENRICHMENT_HTTP_TIMEOUT_SECONDS, PROSPEO_API_KEY
from linkedin.enrichment.base import EnrichmentResult, EnrichmentStatus
from linkedin.enrichment.http import HttpError, post_json

logger = logging.getLogger(__name__)

_URL = "https://api.prospeo.io/enrich-person"


class ProspeoProvider:
    name = "prospeo"

    def enrich(self, lead, task) -> EnrichmentResult:
        if not PROSPEO_API_KEY:
            logger.warning("Prospeo: no API key configured — API_FAILURE")
            return EnrichmentResult(status=EnrichmentStatus.API_FAILURE, provider=self.name)
        try:
            resp = post_json(
                _URL,
                headers={"X-KEY": PROSPEO_API_KEY},
                payload={
                    "only_verified_mobile": True,
                    "data": {"linkedin_url": lead.linkedin_url},
                },
                timeout=ENRICHMENT_HTTP_TIMEOUT_SECONDS,
            )
        except HttpError as exc:
            logger.warning("Prospeo API failure: %s", exc)
            return EnrichmentResult(status=EnrichmentStatus.API_FAILURE, provider=self.name)

        if resp.get("error"):
            logger.warning("Prospeo returned error flag: %s", resp.get("message"))
            return EnrichmentResult(
                status=EnrichmentStatus.API_FAILURE, provider=self.name, raw=resp,
            )

        person = (resp.get("response") or {}).get("person") or {}
        mobile = person.get("mobile") or {}
        phone = mobile.get("mobile")
        if phone:
            return EnrichmentResult(
                status=EnrichmentStatus.FOUND, provider=self.name,
                phone=str(phone), raw=resp,
            )
        return EnrichmentResult(
            status=EnrichmentStatus.NOT_FOUND, provider=self.name, raw=resp,
        )
