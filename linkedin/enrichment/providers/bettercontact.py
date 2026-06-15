"""BetterContact enrichment providers (async submit → poll).

BetterContact is itself a 20+ provider waterfall, so its NOT_FOUND is
authoritative — that is why it sits first in PROVIDER_CHAIN for phone lookup.
Its submit endpoint requires first + last name + company; linkedin_url is only a
hint. When the lead lacks last_name/company_name we short-circuit to API_FAILURE
rather than calling the API or crashing.
"""
from __future__ import annotations

import logging
import time

from linkedin.conf import (
    BETTERCONTACT_API_KEY,
    BETTERCONTACT_POLL_INTERVAL_SECONDS,
    ENRICHMENT_HTTP_TIMEOUT_SECONDS,
    ENRICHMENT_MAX_DURATION_SECONDS,
)
from linkedin.enrichment.base import EnrichmentResult, EnrichmentStatus
from linkedin.enrichment.http import HttpError, get_json, post_json
from linkedin.exceptions import EnrichmentError

logger = logging.getLogger(__name__)

_BASE = "https://app.bettercontact.rocks/api/v2"


class BetterContactProvider:
    name = "bettercontact"
    request_id_key = "bettercontact_request_id"
    enrich_email_address = False
    enrich_phone_number = True
    result_field = "contact_phone_number"

    def enrich(self, lead, task) -> EnrichmentResult:
        if not BETTERCONTACT_API_KEY:
            logger.warning("BetterContact: no API key configured — API_FAILURE")
            return EnrichmentResult(status=EnrichmentStatus.API_FAILURE, provider=self.name)

        if not lead.last_name or not lead.company_name:
            logger.warning(
                "BetterContact: lead %s missing last_name/company — API_FAILURE",
                lead.id,
            )
            return EnrichmentResult(status=EnrichmentStatus.API_FAILURE, provider=self.name)

        request_id = task.payload.get(self.request_id_key) or ""
        try:
            if not request_id:
                request_id = self._submit(lead)
                task.payload[self.request_id_key] = request_id
                task.save(update_fields=["payload"])
            return self._poll(request_id)
        except HttpError as exc:
            logger.warning("BetterContact API failure: %s", exc)
            return EnrichmentResult(status=EnrichmentStatus.API_FAILURE, provider=self.name)

    def _submit(self, lead) -> str:
        resp = post_json(
            f"{_BASE}/async",
            headers={"X-API-Key": BETTERCONTACT_API_KEY},
            payload={
                "data": [{
                    "first_name": lead.first_name,
                    "last_name": lead.last_name,
                    "company": lead.company_name,
                    "linkedin_url": lead.linkedin_url,
                }],
                "enrich_email_address": self.enrich_email_address,
                "enrich_phone_number": self.enrich_phone_number,
            },
            timeout=ENRICHMENT_HTTP_TIMEOUT_SECONDS,
        )
        request_id = resp.get("id")
        if not request_id:
            raise EnrichmentError(f"BetterContact submit returned no id: {resp}")
        return str(request_id)

    def _poll(self, request_id: str) -> EnrichmentResult:
        deadline = time.monotonic() + ENRICHMENT_MAX_DURATION_SECONDS
        while True:
            resp = get_json(
                f"{_BASE}/async/{request_id}",
                headers={"X-API-Key": BETTERCONTACT_API_KEY},
                timeout=ENRICHMENT_HTTP_TIMEOUT_SECONDS,
            )
            if resp.get("status") == "terminated":
                return self._parse_terminated(resp)
            if time.monotonic() >= deadline:
                logger.warning("BetterContact poll timed out for %s", request_id)
                return EnrichmentResult(
                    status=EnrichmentStatus.API_FAILURE, provider=self.name, raw=resp,
                )
            time.sleep(BETTERCONTACT_POLL_INTERVAL_SECONDS)

    def _parse_terminated(self, resp: dict) -> EnrichmentResult:
        data = resp.get("data")
        if not isinstance(data, list) or not data:
            raise EnrichmentError(f"BetterContact terminated with no data: {resp}")
        value = data[0].get(self.result_field)
        if value:
            kwargs = {"email": str(value)} if self.enrich_email_address else {"phone": str(value)}
            return EnrichmentResult(
                status=EnrichmentStatus.FOUND, provider=self.name,
                raw=resp, **kwargs,
            )
        return EnrichmentResult(
            status=EnrichmentStatus.NOT_FOUND, provider=self.name, raw=resp,
        )


class BetterContactEmailProvider(BetterContactProvider):
    name = "bettercontact"
    request_id_key = "bettercontact_email_request_id"
    enrich_email_address = True
    enrich_phone_number = False
    result_field = "contact_email_address"
