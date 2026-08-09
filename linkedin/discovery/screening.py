"""Lightweight card-level matching against a sender's enabled ICPs."""
from __future__ import annotations

import json

from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from linkedin import conf
from linkedin.exceptions import (
    DiscoveryConfigurationError,
    DiscoveryScreeningError,
)
from linkedin.icp_outbound import DiscoveryTarget

from .sources.base import DiscoveryCard


class DiscoveryScreenDecision(BaseModel):
    public_identifier: str
    should_visit: bool
    potential_icp: str = ""


class DiscoveryScreenBatch(BaseModel):
    decisions: list[DiscoveryScreenDecision] = Field(default_factory=list)


def _screening_prompt(
    cards: list[DiscoveryCard],
    targets: tuple[DiscoveryTarget, ...],
) -> str:
    target_payload = [
        {"icp": target.icp, "profile": target.profile}
        for target in targets
    ]
    card_payload = [
        {
            "public_identifier": card.public_identifier,
            "name": card.name,
            "headline": card.headline,
            "company_name": card.company_name,
            "visible_context": card.source_context,
        }
        for card in cards
    ]
    return (
        "You are doing a deliberately lightweight first-pass ICP screen of "
        "LinkedIn recommendation cards. Decide only whether each visible card "
        "is plausibly worth opening. Prefer should_visit=true when the card is "
        "ambiguous but reasonably compatible. For a visited card, select "
        "exactly one ICP from the supplied enabled ICP list. Do not invent ICP "
        "names. Return exactly one decision for every supplied card.\n\n"
        f"Enabled ICPs:\n{json.dumps(target_payload, indent=2)}\n\n"
        f"Cards:\n{json.dumps(card_payload, indent=2)}"
    )


def screen_cards(
    cards: list[DiscoveryCard],
    targets: tuple[DiscoveryTarget, ...],
    *,
    structured_model=None,
) -> dict[str, DiscoveryScreenDecision]:
    if not cards:
        return {}
    if not targets:
        raise DiscoveryConfigurationError("No discovery-enabled ICPs")

    if structured_model is None:
        if not conf.LLM_API_KEY or not conf.AI_MODEL:
            raise DiscoveryConfigurationError(
                "LLM_API_KEY and AI_MODEL are required when profile discovery "
                "is enabled",
            )
        llm = ChatOpenAI(
            model=conf.AI_MODEL,
            temperature=0,
            api_key=conf.LLM_API_KEY,
            base_url=conf.LLM_API_BASE,
            timeout=60,
        )
        structured_model = llm.with_structured_output(DiscoveryScreenBatch)

    result = structured_model.invoke(_screening_prompt(cards, targets))
    if isinstance(result, dict):
        result = DiscoveryScreenBatch.model_validate(result)
    if not isinstance(result, DiscoveryScreenBatch):
        raise DiscoveryScreeningError("Screening returned an unexpected result type")

    card_ids = {card.public_identifier for card in cards}
    enabled_icps = {target.icp for target in targets}
    decisions: dict[str, DiscoveryScreenDecision] = {}
    for decision in result.decisions:
        public_identifier = decision.public_identifier.strip().lower()
        if public_identifier not in card_ids:
            raise DiscoveryScreeningError(
                f"Screening returned unknown profile {public_identifier!r}",
            )
        if public_identifier in decisions:
            raise DiscoveryScreeningError(
                f"Screening returned duplicate profile {public_identifier!r}",
            )
        if decision.should_visit and decision.potential_icp not in enabled_icps:
            raise DiscoveryScreeningError(
                f"Screening returned disabled ICP {decision.potential_icp!r}",
            )
        decisions[public_identifier] = decision

    missing = card_ids - set(decisions)
    if missing:
        raise DiscoveryScreeningError(
            f"Screening omitted profiles: {sorted(missing)}",
        )
    return decisions
