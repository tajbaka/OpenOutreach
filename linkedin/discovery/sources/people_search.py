"""Bounded extraction from LinkedIn People-search result cards."""
from __future__ import annotations

import json
import logging

from linkedin.actions.search import search_people
from linkedin.db.urls import public_id_to_url, url_to_public_id
from linkedin.exceptions import (
    AuthenticationError,
    DiscoverySurfaceError,
    LinkedInDiscoveryLimitError,
)

from .base import DiscoveryCard

logger = logging.getLogger(__name__)

# These selectors are deliberately rooted in People-search result containers.
# Do not fall back to harvesting every /in/ anchor on the page.
PEOPLE_RESULT_CARD_SELECTORS = (
    'main [data-testid="lazy-column"] [role="list"] [role="listitem"]',
    "main li.reusable-search__result-container",
    "main div.entity-result",
    "main [data-chameleon-result-urn]",
)
PEOPLE_RESULT_CARD_SELECTOR = ", ".join(PEOPLE_RESULT_CARD_SELECTORS)

_NAME_SELECTORS = (
    ".entity-result__title-text",
    "[data-anonymize='person-name']",
    ".artdeco-entity-lockup__title",
)
_HEADLINE_SELECTORS = (
    ".entity-result__primary-subtitle",
    ".artdeco-entity-lockup__subtitle",
)
_COMPANY_SELECTORS = (
    "[data-anonymize='company-name']",
)


def _first_text(container, selectors: tuple[str, ...]) -> str:
    for selector in selectors:
        locator = container.locator(selector)
        if locator.count() == 0:
            continue
        try:
            value = locator.first.inner_text(timeout=2000).strip()
        except Exception:
            continue
        if value:
            return value
    return ""


def _profile_link_text(links) -> str:
    """Use the first non-empty profile-link label as a hashed-UI name fallback."""
    if not hasattr(links, "all"):
        return ""
    for link in links.all():
        try:
            value = link.inner_text(timeout=2000).strip()
        except Exception:
            continue
        if value:
            return value
    return ""


def _page_text(page) -> str:
    try:
        return page.locator("body").inner_text(timeout=3000)
    except Exception:
        return ""


def _assert_search_surface_available(session) -> str:
    page = session.page
    url = (page.url or "").lower()
    if any(token in url for token in ("/login", "/checkpoint/", "/challenge/")):
        raise AuthenticationError(f"LinkedIn authentication lost at {page.url}")

    text = _page_text(page)
    lowered = text.lower()
    if (
        "commercial use limit" in lowered
        or "search limit" in lowered
        or "you've reached the limit" in lowered
    ):
        raise LinkedInDiscoveryLimitError(
            "LinkedIn displayed a search/commercial-use limit",
        )
    return text


def _profile_link_diagnostics(page) -> list[dict]:
    """Return a small ancestor summary when LinkedIn changes card markup."""
    links = page.locator('main a[href*="/in/"]')
    if not hasattr(links, "evaluate_all"):
        return []
    try:
        return links.evaluate_all(
            """
            nodes => nodes.slice(0, 3).map(link => {
              const ancestors = [];
              let node = link;
              for (let depth = 0; node && depth < 14; depth += 1) {
                const profileHrefs = Array.from(
                  node.querySelectorAll?.('a[href*="/in/"]') || [],
                ).map(anchor => anchor.getAttribute("href") || "");
                ancestors.push({
                  tag: node.tagName,
                  class: node.className || "",
                  id: node.id || "",
                  role: node.getAttribute("role") || "",
                  ariaLabel: node.getAttribute("aria-label") || "",
                  viewName: node.getAttribute("data-view-name") || "",
                  testId: node.getAttribute("data-testid") || "",
                  componentType:
                    node.getAttribute("data-component-type") || "",
                  chameleonUrn:
                    node.getAttribute("data-chameleon-result-urn") || "",
                  uniqueProfileHrefs: new Set(profileHrefs).size,
                });
                node = node.parentElement;
              }
              return {
                href: link.getAttribute("href") || "",
                ancestors,
              };
            })
            """,
        )
    except Exception:
        logger.debug("Could not collect People-search selector diagnostics", exc_info=True)
        return []


def collect_people_search_cards(
    session,
    *,
    query: str,
    page_number: int,
) -> list[DiscoveryCard]:
    """Navigate to one People-search page and return only its result cards."""
    search_people(session, query, page=page_number)
    page_text = _assert_search_surface_available(session)
    containers = session.page.locator(PEOPLE_RESULT_CARD_SELECTOR).all()

    cards: list[DiscoveryCard] = []
    seen: set[str] = set()
    for container in containers:
        links = container.locator('a[href*="/in/"]')
        if links.count() == 0:
            continue
        try:
            href = links.first.get_attribute("href", timeout=2000) or ""
        except Exception:
            continue
        public_identifier = url_to_public_id(href)
        if not public_identifier:
            continue
        public_identifier = public_identifier.strip().lower()
        if public_identifier in seen:
            continue
        seen.add(public_identifier)

        name = _first_text(container, _NAME_SELECTORS) or _profile_link_text(links)
        headline = _first_text(container, _HEADLINE_SELECTORS)
        company_name = _first_text(container, _COMPANY_SELECTORS)
        try:
            context = container.inner_text(timeout=2000).strip()
        except Exception:
            context = ""
        cards.append(
            DiscoveryCard(
                public_identifier=public_identifier,
                linkedin_url=public_id_to_url(public_identifier),
                name=name,
                headline=headline,
                company_name=company_name,
                source_context=context[:1500],
            ),
        )

    if cards:
        return cards

    lowered = page_text.lower()
    if "no results" in lowered or "0 results" in lowered:
        return []

    if session.page.locator('main a[href*="/in/"]').count() > 0:
        diagnostics = _profile_link_diagnostics(session.page)
        raise DiscoverySurfaceError(
            "People-search profile links were present, but no supported result "
            f"card selector matched; samples={json.dumps(diagnostics)}",
        )

    logger.info(
        "People search returned no profile cards for query=%r page=%d",
        query,
        page_number,
    )
    return []
