"""One-hop extraction from a visited profile's More profiles for you rail."""
from __future__ import annotations

from linkedin.exceptions import DiscoverySurfaceError

from .base import DiscoveryCard
from .recommendation_common import (
    RecommendationSourceResult,
    assert_authenticated,
    collect_dialog_cards,
    dismiss_dialog,
    extract_cards,
    recommendation_overlay_by_heading,
)

MORE_PROFILES_HEADING = "More profiles for you"
MORE_PROFILES_LINK_SELECTOR = 'a[href*="/overlay/browsemap-recommendations/"]'


def _profile_rail(page):
    headings = page.get_by_role("heading", name=MORE_PROFILES_HEADING, exact=True)
    visible = [heading for heading in headings.all() if heading.is_visible()]
    if not visible:
        return None
    if len(visible) > 1:
        raise DiscoverySurfaceError(
            "Profile exposed multiple visible More profiles for you headings",
        )
    rail = visible[0]
    for _depth in range(8):
        rail = rail.locator("xpath=..")
        if rail.count() == 0:
            break
        tag_name = rail.evaluate("node => node.tagName.toLowerCase()")
        if tag_name in {"body", "html", "main"}:
            break
        if rail.locator('a[href*="/in/"]').count() > 0:
            return rail
    raise DiscoverySurfaceError(
        "More profiles for you heading had no bounded rail container",
    )


def collect_profile_recommendations(
    session,
    *,
    source_profile_public_identifier: str,
    max_cards: int,
    max_scroll_rounds: int,
    max_consecutive_empty_scrolls: int,
) -> RecommendationSourceResult:
    """Collect depth-1 recommendations from the currently opened profile."""
    page = session.page
    assert_authenticated(page)
    source_profile_public_identifier = source_profile_public_identifier.strip().lower()
    if f"/in/{source_profile_public_identifier}" not in (page.url or "").lower():
        raise DiscoverySurfaceError(
            "Profile recommendation source does not match the currently opened profile",
        )

    rail = _profile_rail(page)
    if rail is None:
        return RecommendationSourceResult(cards=(), stop_reason="source_exhausted")

    by_id: dict[str, DiscoveryCard] = {}
    for card in extract_cards(
        rail,
        source_kind="profile_recommendation",
        source_section=MORE_PROFILES_HEADING,
        source_profile_public_identifier=source_profile_public_identifier,
        recommendation_depth=1,
    ):
        if card.public_identifier != source_profile_public_identifier:
            by_id.setdefault(card.public_identifier, card)
        if len(by_id) >= max_cards:
            return RecommendationSourceResult(
                cards=tuple(by_id.values()),
                sections_scanned=1,
                stop_reason="card_limit_reached",
                section_headings=(MORE_PROFILES_HEADING,),
            )

    show_all = rail.locator(MORE_PROFILES_LINK_SELECTOR)
    visible_show_all = [link for link in show_all.all() if link.is_visible()]
    if not visible_show_all:
        return RecommendationSourceResult(
            cards=tuple(by_id.values()),
            sections_scanned=1,
            stop_reason="source_exhausted",
            section_headings=(MORE_PROFILES_HEADING,),
        )
    if len(visible_show_all) != 1:
        raise DiscoverySurfaceError(
            "More profiles for you rail had multiple visible Show All links",
        )

    visible_show_all[0].click()
    page.wait_for_timeout(500)
    dialog = recommendation_overlay_by_heading(page, MORE_PROFILES_HEADING)
    if dialog is None:
        raise DiscoverySurfaceError(
            "More profiles for you Show All opened no supported dialog",
        )
    try:
        modal_cards, scroll_rounds, empty = collect_dialog_cards(
            dialog,
            source_kind="profile_recommendation",
            source_section=MORE_PROFILES_HEADING,
            source_profile_public_identifier=source_profile_public_identifier,
            recommendation_depth=1,
            max_cards=max_cards,
            max_scroll_rounds=max_scroll_rounds,
            max_consecutive_empty_scrolls=max_consecutive_empty_scrolls,
        )
    finally:
        dismiss_dialog(dialog)
    for card in modal_cards:
        if card.public_identifier != source_profile_public_identifier:
            by_id.setdefault(card.public_identifier, card)
        if len(by_id) >= max_cards:
            break
    stop_reason = (
        "card_limit_reached" if len(by_id) >= max_cards else "source_exhausted"
    )
    return RecommendationSourceResult(
        cards=tuple(by_id.values()),
        sections_scanned=1,
        scroll_rounds=scroll_rounds,
        consecutive_empty_scrolls=empty,
        stop_reason=stop_reason,
        section_headings=(MORE_PROFILES_HEADING,),
        overlays_opened=1,
    )
