"""Shared discovery-source value objects."""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class DiscoveryCard:
    public_identifier: str
    linkedin_url: str
    name: str = ""
    headline: str = ""
    company_name: str = ""
    source_context: str = ""
    potential_icp: str = ""

    def to_payload(self) -> dict[str, str]:
        return asdict(self)

    @classmethod
    def from_payload(cls, value: dict) -> "DiscoveryCard":
        allowed = {
            "public_identifier",
            "linkedin_url",
            "name",
            "headline",
            "company_name",
            "source_context",
            "potential_icp",
        }
        return cls(**{key: value.get(key, "") for key in allowed})
