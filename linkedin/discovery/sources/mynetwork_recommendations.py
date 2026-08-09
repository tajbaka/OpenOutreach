"""Bounded extraction from LinkedIn's personalized My Network suggestions."""
from __future__ import annotations

import logging

from linkedin.browser.nav import goto_page
from linkedin.exceptions import DiscoverySurfaceError

from .base import DiscoveryCard
from .recommendation_common import (
    RecommendationSourceResult,
    assert_authenticated,
    cards_from_rows,
    collect_dialog_cards,
    dismiss_dialog,
    recommendation_overlay_by_heading,
)

logger = logging.getLogger(__name__)

MYNETWORK_GROW_URL = "https://www.linkedin.com/mynetwork/grow/"
HEADING_SELECTOR = "main h2, main h3, [role='main'] h2, [role='main'] h3"

_EXTRACT_SECTIONS_JS = r"""
(headings) => {
  const normalize = (value) => (value || "").replace(/\s+/g, " ").trim();
  const supported = (value) => (
    value === "Suggestions for you" || value.startsWith("People you may know")
  );
  const output = [];
  const seen = new Set();
  for (const heading of headings) {
    const title = normalize(heading.innerText || heading.textContent);
    if (!supported(title) || seen.has(title)) continue;

    let section = heading.parentElement;
    for (let depth = 0; section && depth < 10; depth += 1) {
      const profileLinks = section.querySelectorAll('a[href*="/in/"]');
      const supportedHeadings = Array.from(section.querySelectorAll("h2, h3"))
        .map((node) => normalize(node.innerText || node.textContent))
        .filter(supported);
      if (profileLinks.length > 0 && supportedHeadings.length === 1) break;
      section = section.parentElement;
    }
    if (!section || section === document.body) continue;

    const showAll = Array.from(section.querySelectorAll("a"))
      .find((link) => {
        const label = normalize(link.getAttribute("aria-label"));
        return label === `Show all suggestions for ${title}`;
      });
    const links = Array.from(section.querySelectorAll('a[href*="/in/"]'));
    const rows = links.map((link) => {
      let card = link;
      let node = link.parentElement;
      while (node && node !== section) {
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
      const headlineNode = card.querySelector(
        '[data-anonymize="headline"], [class*="subtitle"], '
        + '.artdeco-entity-lockup__subtitle',
      );
      const companyNode = card.querySelector('[data-anonymize="company-name"]');
      return {
        href: link.getAttribute("href") || "",
        name: normalize(link.innerText || link.textContent)
          || normalize(link.querySelector("img")?.getAttribute("alt"))
          || normalize(link.getAttribute("aria-label")),
        headline: normalize(headlineNode?.innerText),
        company_name: normalize(companyNode?.innerText),
        context: normalize(card.innerText || card.textContent)
          || normalize(link.getAttribute("aria-label")),
      };
    });
    output.push({
      heading: title,
      show_all_label: normalize(showAll?.getAttribute("aria-label")),
      rows,
    });
    seen.add(title);
  }
  return output;
}
"""


def is_supported_section_heading(value: str) -> bool:
    normalized = " ".join((value or "").split())
    return normalized == "Suggestions for you" or normalized.startswith(
        "People you may know",
    )


def _extract_sections(page) -> list[dict]:
    sections = page.locator(HEADING_SELECTOR).evaluate_all(_EXTRACT_SECTIONS_JS)
    return [
        section
        for section in sections
        if is_supported_section_heading(section.get("heading") or "")
    ]


def _section_show_all_link(page, heading_text: str, label: str):
    """Resolve Show All only through its exact recommendation section."""
    headings = page.get_by_role("heading", name=heading_text, exact=True)
    visible_headings = [heading for heading in headings.all() if heading.is_visible()]
    if len(visible_headings) != 1:
        raise DiscoverySurfaceError(
            f"Expected one visible recommendation heading {heading_text!r}; "
            f"found {len(visible_headings)}",
        )

    container = visible_headings[0]
    for _depth in range(10):
        container = container.locator("xpath=..")
        if container.count() == 0:
            break
        profile_links = container.locator('a[href*="/in/"]')
        scoped_links = container.get_by_role("link", name=label, exact=True)
        visible_links = [link for link in scoped_links.all() if link.is_visible()]
        if not visible_links:
            continue
        if profile_links.count() == 0:
            continue
        if len(visible_links) != 1:
            raise DiscoverySurfaceError(
                f"Recommendation section {heading_text!r} had an ambiguous "
                f"Show All structure for {label!r}",
            )
        return visible_links[0]

    raise DiscoverySurfaceError(
        f"Recommendation section {heading_text!r} had no scoped Show All "
        f"link {label!r}",
    )


def _click_show_all(page, heading_text: str, label: str):
    link = _section_show_all_link(page, heading_text, label)
    link.click()
    page.wait_for_timeout(500)
    dialog = recommendation_overlay_by_heading(page, heading_text)
    if dialog is None:
        raise DiscoverySurfaceError(
            f"Recommendation Show All {label!r} opened no supported dialog",
        )
    return dialog


def _scroll_page(page) -> None:
    page.mouse.move(700, 650)
    page.mouse.wheel(0, 2_400)
    page.keyboard.press("PageDown")
    page.wait_for_timeout(1_000)


def collect_mynetwork_recommendations(
    session,
    *,
    max_cards: int,
    max_sections: int,
    max_scroll_rounds: int,
    max_consecutive_empty_scrolls: int,
) -> RecommendationSourceResult:
    """Collect personalized, section-rooted profile cards without outbound clicks."""
    goto_page(
        session,
        lambda: session.page.goto(MYNETWORK_GROW_URL),
        "/mynetwork/grow",
        error_message="Could not open LinkedIn My Network recommendations",
    )
    page = session.page
    assert_authenticated(page)

    cards_by_id: dict[str, DiscoveryCard] = {}
    seen_sections: list[str] = []
    opened_show_all: set[str] = set()
    scroll_rounds = 0
    empty_scrolls = 0
    stop_reason = "source_exhausted"

    while True:
        before = len(cards_by_id)
        sections = _extract_sections(page)
        for section in sections:
            heading = section["heading"]
            if heading not in seen_sections:
                if len(seen_sections) >= max_sections:
                    stop_reason = "section_limit_reached"
                    break
                seen_sections.append(heading)
            for card in cards_from_rows(
                section.get("rows") or [],
                source_kind="mynetwork_recommendation",
                source_section=heading,
                recommendation_depth=0,
            ):
                cards_by_id.setdefault(card.public_identifier, card)
                if len(cards_by_id) >= max_cards:
                    stop_reason = "card_limit_reached"
                    break
            if stop_reason in {"section_limit_reached", "card_limit_reached"}:
                break

            show_all_label = section.get("show_all_label") or ""
            if show_all_label and heading not in opened_show_all:
                dialog = _click_show_all(page, heading, show_all_label)
                try:
                    remaining_scrolls = max(
                        0,
                        max_scroll_rounds - scroll_rounds,
                    )
                    modal_cards, used_rounds, _empty = collect_dialog_cards(
                        dialog,
                        source_kind="mynetwork_recommendation",
                        source_section=heading,
                        recommendation_depth=0,
                        max_cards=max_cards - len(cards_by_id),
                        max_scroll_rounds=remaining_scrolls,
                        max_consecutive_empty_scrolls=(
                            max_consecutive_empty_scrolls
                        ),
                    )
                finally:
                    dismiss_dialog(dialog)
                scroll_rounds += used_rounds
                for card in modal_cards:
                    cards_by_id.setdefault(card.public_identifier, card)
                opened_show_all.add(heading)
                if len(cards_by_id) >= max_cards:
                    stop_reason = "card_limit_reached"
                    break
                if scroll_rounds >= max_scroll_rounds:
                    stop_reason = "scroll_limit_reached"
                    break

        if stop_reason in {
            "section_limit_reached",
            "card_limit_reached",
            "scroll_limit_reached",
        }:
            break

        if len(cards_by_id) == before:
            empty_scrolls += 1
        else:
            empty_scrolls = 0
        if empty_scrolls >= max_consecutive_empty_scrolls:
            stop_reason = "source_exhausted"
            break
        if scroll_rounds >= max_scroll_rounds:
            stop_reason = "scroll_limit_reached"
            break
        _scroll_page(page)
        scroll_rounds += 1

    if not seen_sections:
        page_text = page.locator("body").inner_text(timeout=3_000)
        if any(
            token in page_text
            for token in ("Suggestions for you", "People you may know")
        ):
            raise DiscoverySurfaceError(
                "LinkedIn recommendation headings were visible, but no supported "
                "section container could be extracted",
            )
        logger.info("My Network exposed no supported recommendation sections")

    return RecommendationSourceResult(
        cards=tuple(cards_by_id.values()),
        sections_scanned=len(seen_sections),
        scroll_rounds=scroll_rounds,
        consecutive_empty_scrolls=empty_scrolls,
        stop_reason=stop_reason,
        section_headings=tuple(seen_sections),
        overlays_opened=len(opened_show_all),
    )
