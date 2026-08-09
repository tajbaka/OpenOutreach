"""Lightweight card-level matching against a sender's enabled ICPs."""
from __future__ import annotations

import json
import re

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
    score: int = 0
    reason: str = ""


class DiscoveryCardScore(BaseModel):
    public_identifier: str
    best_icp: str = ""
    score: int = Field(ge=0, le=100)
    reason: str = ""


class DiscoveryScoreBatch(BaseModel):
    scores: list[DiscoveryCardScore] = Field(default_factory=list)


def _canonical_public_identifier(value: str) -> str:
    return re.sub(r"\s+", "", value or "").strip().strip("/").lower()


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
        "You are scoring LinkedIn recommendation cards against enabled ICP "
        "profiles. The ICP profiles are the only targeting rules. For each "
        "card, consider every enabled ICP independently, then return the best "
        "matching ICP name, an ICP-fit score from 0 to 100, and a short reason. "
        "Score high only when the visible name, headline, company, or context "
        "contains concrete evidence matching that ICP profile. Score low when "
        "the evidence is ambiguous, generic, or based only on geography, "
        "school, mutual connections, generic seniority, or broad technology "
        "terms unless that ICP profile itself makes those signals sufficient. "
        "Use best_icp=\"\" when no enabled ICP is a concrete fit. Do not invent "
        "ICP names. Return exactly one score for every supplied card.\n\n"
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
            timeout=120,
        )
        structured_model = llm.with_structured_output(DiscoveryScoreBatch)

    result = structured_model.invoke(_screening_prompt(cards, targets))
    if isinstance(result, dict):
        result = DiscoveryScoreBatch.model_validate(result)
    if not isinstance(result, DiscoveryScoreBatch):
        raise DiscoveryScreeningError("Screening returned an unexpected result type")

    card_ids = [_canonical_public_identifier(card.public_identifier) for card in cards]
    card_id_set = set(card_ids)
    enabled_icp_set = {target.icp for target in targets}
    scores: dict[str, DiscoveryCardScore] = {}
    for score in result.scores:
        public_identifier = _canonical_public_identifier(score.public_identifier)
        if public_identifier not in card_id_set:
            raise DiscoveryScreeningError(
                f"Screening returned unknown profile {public_identifier!r}",
            )
        if score.best_icp and score.best_icp not in enabled_icp_set:
            raise DiscoveryScreeningError(
                f"Screening returned disabled ICP {score.best_icp!r}",
            )
        if public_identifier in scores:
            raise DiscoveryScreeningError(
                f"Screening returned duplicate profile {public_identifier!r}",
            )
        scores[public_identifier] = score

    missing = card_id_set - set(scores)
    if missing:
        raise DiscoveryScreeningError(
            f"Screening omitted profiles: {sorted(missing)}",
        )

    decisions: dict[str, DiscoveryScreenDecision] = {}
    for public_identifier in card_ids:
        best = scores[public_identifier]
        should_visit = (
            best.score >= conf.DISCOVERY_VISIT_SCORE_THRESHOLD
            and best.best_icp in enabled_icp_set
        )
        decisions[public_identifier] = DiscoveryScreenDecision(
            public_identifier=public_identifier,
            should_visit=should_visit,
            potential_icp=best.best_icp if should_visit else "",
            score=best.score,
            reason=best.reason,
        )
    return decisions
