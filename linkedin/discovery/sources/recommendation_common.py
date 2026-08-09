"""Shared, read-only extraction helpers for LinkedIn recommendations."""
from __future__ import annotations

from dataclasses import dataclass

from linkedin.db.urls import public_id_to_url, url_to_public_id
from linkedin.exceptions import AuthenticationError, DiscoverySurfaceError

from .base import DiscoveryCard

_EXTRACT_PROFILE_ROWS_JS = r"""
(links) => {
  const rows = [];
  const seen = new Set();
  for (const link of links) {
    const href = link.getAttribute("href") || "";
    if (!href.includes("/in/")) continue;
    const cleanHref = href.split("?")[0].split("#")[0];
    if (seen.has(cleanHref)) continue;
    seen.add(cleanHref);

    let card = link;
    let node = link.parentElement;
    while (node && node !== document.body) {
      const hrefs = new Set(
        Array.from(node.querySelectorAll('a[href*="/in/"]'))
          .map((anchor) => (anchor.getAttribute("href") || "")
            .split("?")[0].split("#")[0])
          .filter(Boolean),
      );
      if (hrefs.size > 1) break;
      card = node;
      node = node.parentElement;
    }

    const text = (card.innerText || card.textContent || "")
      .replace(/\s+/g, " ").trim();
    const linkText = (link.innerText || link.textContent || "")
      .replace(/\s+/g, " ").trim();
    const aria = (link.getAttribute("aria-label") || "")
      .replace(/\s+/g, " ").trim();
    const imageAlt = (link.querySelector("img")?.getAttribute("alt") || "")
      .replace(/\s+/g, " ").trim();
    const headlineNode = card.querySelector(
      '[data-anonymize="headline"], [class*="subtitle"], '
      + '.artdeco-entity-lockup__subtitle',
    );
    const companyNode = card.querySelector('[data-anonymize="company-name"]');
    rows.push({
      href,
      name: linkText || imageAlt || aria,
      headline: (headlineNode?.innerText || "").replace(/\s+/g, " ").trim(),
      company_name: (companyNode?.innerText || "")
        .replace(/\s+/g, " ").trim(),
      context: text || aria,
    });
  }
  return rows;
}
"""


@dataclass(frozen=True)
class RecommendationSourceResult:
    cards: tuple[DiscoveryCard, ...]
    sections_scanned: int = 0
    scroll_rounds: int = 0
    consecutive_empty_scrolls: int = 0
    stop_reason: str = "source_exhausted"
    section_headings: tuple[str, ...] = ()
    overlays_opened: int = 0


def assert_authenticated(page) -> None:
    """Fail explicitly if a recommendation surface lost authentication."""
    url = (page.url or "").lower()
    if any(token in url for token in ("/login", "/checkpoint/", "/challenge/")):
        raise AuthenticationError(f"LinkedIn authentication lost at {page.url}")


def cards_from_rows(
    rows: list[dict],
    *,
    source_kind: str,
    source_section: str,
    source_profile_public_identifier: str = "",
    recommendation_depth: int,
) -> list[DiscoveryCard]:
    """Convert browser-extracted rows into canonical, source-labelled cards."""
    cards: list[DiscoveryCard] = []
    seen: set[str] = set()
    for row in rows:
        public_identifier = (url_to_public_id(row.get("href") or "") or "").lower()
        if not public_identifier or public_identifier in seen:
            continue
        seen.add(public_identifier)
        cards.append(
            DiscoveryCard(
                public_identifier=public_identifier,
                linkedin_url=public_id_to_url(public_identifier),
                name=(row.get("name") or "").strip()[:300],
                headline=(row.get("headline") or "").strip()[:1000],
                company_name=(row.get("company_name") or "").strip()[:300],
                source_context=(row.get("context") or "").strip()[:1500],
                source_kind=source_kind,
                source_section=source_section,
                source_profile_public_identifier=(
                    source_profile_public_identifier.strip().lower()
                ),
                recommendation_depth=recommendation_depth,
            ),
        )
    return cards


def extract_cards(
    container,
    *,
    source_kind: str,
    source_section: str,
    source_profile_public_identifier: str = "",
    recommendation_depth: int,
) -> list[DiscoveryCard]:
    rows = container.locator('a[href*="/in/"]').evaluate_all(
        _EXTRACT_PROFILE_ROWS_JS,
    )
    return cards_from_rows(
        rows,
        source_kind=source_kind,
        source_section=source_section,
        source_profile_public_identifier=source_profile_public_identifier,
        recommendation_depth=recommendation_depth,
    )


def recommendation_overlay_by_heading(page, heading_text: str):
    """Find LinkedIn's current role-less recommendation overlay safely."""
    headings = page.get_by_role("heading", name=heading_text, exact=True)
    for heading in headings.all():
        if not heading.is_visible():
            continue
        overlay = heading.locator(
            "xpath=ancestor::*[.//button[@aria-label='Dismiss'] "
            "and .//a[contains(@href, '/in/')]][1]",
        )
        if overlay.count() > 0 and overlay.first.is_visible():
            return overlay.first
    return None


def scroll_recommendation_container(container) -> None:
    """Advance the deepest useful scroll area without clicking any controls."""
    container.evaluate(
        """
        root => {
          const nodes = [root, ...root.querySelectorAll("*")];
          const scrollable = nodes
            .filter((node) => node.scrollHeight > node.clientHeight + 20)
            .sort((a, b) => b.scrollHeight - a.scrollHeight)[0] || root;
          scrollable.scrollTop += Math.max(scrollable.clientHeight * 0.85, 500);
        }
        """,
    )
    links = container.locator('a[href*="/in/"]')
    if links.count() > 0:
        links.last.scroll_into_view_if_needed(timeout=2_000)
    container.page.wait_for_timeout(900)


def dismiss_dialog(dialog) -> None:
    """Close only the overlay-level Dismiss button, never a suggestion action."""
    dismiss = dialog.locator('button[aria-label="Dismiss"]')
    visible = [button for button in dismiss.all() if button.is_visible()]
    if len(visible) != 1:
        raise DiscoverySurfaceError(
            "Recommendation overlay must have exactly one visible exact "
            f"Dismiss control; found {len(visible)}",
        )
    visible[0].click()
    dialog.page.wait_for_timeout(350)


def collect_dialog_cards(
    dialog,
    *,
    source_kind: str,
    source_section: str,
    source_profile_public_identifier: str = "",
    recommendation_depth: int,
    max_cards: int,
    max_scroll_rounds: int,
    max_consecutive_empty_scrolls: int,
) -> tuple[list[DiscoveryCard], int, int]:
    """Extract and bounded-scroll one already-open recommendation overlay."""
    by_id: dict[str, DiscoveryCard] = {}
    rounds = 0
    empty = 0
    while True:
        before = len(by_id)
        for card in extract_cards(
            dialog,
            source_kind=source_kind,
            source_section=source_section,
            source_profile_public_identifier=source_profile_public_identifier,
            recommendation_depth=recommendation_depth,
        ):
            by_id.setdefault(card.public_identifier, card)
            if len(by_id) >= max_cards:
                return list(by_id.values()), rounds, empty

        if len(by_id) == before:
            empty += 1
        else:
            empty = 0
        if empty >= max_consecutive_empty_scrolls or rounds >= max_scroll_rounds:
            return list(by_id.values()), rounds, empty

        scroll_recommendation_container(dialog)
        rounds += 1
