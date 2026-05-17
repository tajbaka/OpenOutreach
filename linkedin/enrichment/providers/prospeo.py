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
                    # enrich_mobile triggers the reveal — only_verified_mobile
                    # alone returns a masked number (revealed: false).
                    "enrich_mobile": True,
                    "only_verified_mobile": True,
                    "data": {"linkedin_url": lead.linkedin_url},
                },
                timeout=ENRICHMENT_HTTP_TIMEOUT_SECONDS,
            )
        except HttpError as exc:
            # Prospeo signals "no verified mobile found" as HTTP 400 with
            # error_code NO_MATCH — a clean terminal negative, not a
            # transport failure. Everything else (other 400s, rate limits,
            # 5xx, network errors) is a real API_FAILURE → waterfall failover.
            body = exc.body or {}
            if exc.status == 400 and body.get("error_code") == "NO_MATCH":
                logger.info("Prospeo: no match for %s", lead.linkedin_url)
                return EnrichmentResult(
                    status=EnrichmentStatus.NOT_FOUND, provider=self.name, raw=body,
                )
            logger.warning("Prospeo API failure: %s", exc)
            return EnrichmentResult(status=EnrichmentStatus.API_FAILURE, provider=self.name)

        if resp.get("error"):
            logger.warning("Prospeo returned error flag: %s", resp.get("message"))
            return EnrichmentResult(
                status=EnrichmentStatus.API_FAILURE, provider=self.name, raw=resp,
            )

        # enrich-person returns `person` at the top level — no `response`
        # wrapper (that wrapper is only on Prospeo's account-info endpoint).
        person = resp.get("person") or {}
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
