"""Phone-enrichment provider protocol and result types.

A provider is any object with a `name` and an `enrich(lead, task)` method
returning an EnrichmentResult. The waterfall (waterfall.py) iterates an
ordered list of them. See docs/superpowers/specs/2026-05-17-phone-enrichment-design.md.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable


class EnrichmentStatus(str, Enum):
    """Outcome of one provider call.

    FOUND / NOT_FOUND are both *terminal* for the waterfall — a NOT_FOUND
    from BetterContact (a 20+ provider waterfall itself) is authoritative.
    API_FAILURE drives failover to the next provider.
    """

    FOUND = "found"
    NOT_FOUND = "not_found"
    API_FAILURE = "api_failure"


@dataclass
class EnrichmentResult:
    status: EnrichmentStatus
    provider: str
    phone: str | None = None
    email: str | None = None
    raw: dict = field(default_factory=dict)


@runtime_checkable
class PhoneProvider(Protocol):
    """Structural type every provider satisfies. `task` is the enrich_phone
    Task — BetterContact reads/writes `payload.bettercontact_request_id` on
    it; other providers ignore it."""

    name: str

    def enrich(self, lead, task) -> EnrichmentResult:
        ...
