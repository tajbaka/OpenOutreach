"""Idempotent UI-only Like action for an exact LinkedIn feed post."""
from __future__ import annotations

import logging
from enum import Enum

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from linkedin.feed_collection import post_url_for_activity_urn

logger = logging.getLogger(__name__)

REACTION_BUTTON_SELECTOR = 'button[aria-label^="Reaction button state:" i]'
_REACTION_STATE_PREFIX = "reaction button state:"
_REACTION_VERIFY_WAIT_MS = 10_000
_REACTION_VERIFY_POLL_MS = 250


class FeedLikeResult(str, Enum):
    LIKED = "liked"
    ALREADY_LIKED = "already_liked"
    PRESERVED_REACTION = "preserved_reaction"


class FeedLikeSendError(RuntimeError):
    """Raised when Like failed before a click could mutate LinkedIn."""


class FeedLikeUncertainError(FeedLikeSendError):
    """Raised when LinkedIn may have accepted the Like click."""


def ensure_feed_post_liked(
    session,
    *,
    post_url: str = "",
    activity_urn: str = "",
) -> FeedLikeResult:
    """Like an exact post unless the account already has any reaction."""
    target_url = (post_url or "").strip() or post_url_for_activity_urn(activity_urn)
    if not target_url:
        raise FeedLikeSendError("Feed post has no URL or activity URN")

    page = _new_page(session)
    close_page = page is not getattr(session, "page", None)
    click_attempted = False
    try:
        page.goto(target_url, wait_until="domcontentloaded", timeout=45_000)
        page.wait_for_timeout(2000)
        _raise_if_login_wall(page)

        button = _unique_visible_reaction_button(page)
        state = _reaction_state(button)
        if state == "like":
            return FeedLikeResult.ALREADY_LIKED
        if state != "no reaction":
            logger.info("Preserving existing LinkedIn reaction %r on %s", state, target_url)
            return FeedLikeResult.PRESERVED_REACTION

        click_attempted = True
        button.click(delay=150)
        if not _wait_for_reaction_state(page, "like"):
            raise FeedLikeUncertainError(
                "Clicked Like but could not verify the post reaction state",
            )
        logger.info("Feed post liked on %s", target_url)
        return FeedLikeResult.LIKED
    except FeedLikeUncertainError:
        raise
    except FeedLikeSendError as exc:
        if click_attempted:
            raise FeedLikeUncertainError(str(exc)) from exc
        raise
    except (PlaywrightError, PlaywrightTimeoutError, TimeoutError) as exc:
        if click_attempted:
            raise FeedLikeUncertainError(str(exc)) from exc
        raise FeedLikeSendError(str(exc)) from exc
    finally:
        if close_page:
            try:
                page.close()
            except Exception:
                pass


def _new_page(session):
    context = getattr(session, "context", None)
    if context is not None:
        return context.new_page()
    page = getattr(session, "page", None)
    if page is None:
        raise FeedLikeSendError("No browser page available for feed Like")
    return page


def _visible_reaction_buttons(page) -> list:
    locator = page.locator(REACTION_BUTTON_SELECTOR)
    try:
        count = min(locator.count(), 20)
    except PlaywrightError:
        return []
    visible = []
    for index in range(count):
        button = locator.nth(index)
        try:
            if button.is_visible():
                visible.append(button)
        except PlaywrightError:
            continue
    return visible


def _unique_visible_reaction_button(page):
    buttons = _visible_reaction_buttons(page)
    if len(buttons) != 1:
        raise FeedLikeSendError(
            f"Expected one visible post reaction button, found {len(buttons)}",
        )
    return buttons[0]


def _reaction_state(button) -> str:
    try:
        label = (button.get_attribute("aria-label") or "").strip().lower()
    except PlaywrightError as exc:
        raise FeedLikeSendError(str(exc)) from exc
    if not label.startswith(_REACTION_STATE_PREFIX):
        raise FeedLikeSendError("LinkedIn reaction button has no readable state")
    state = label[len(_REACTION_STATE_PREFIX):].strip()
    if not state:
        raise FeedLikeSendError("LinkedIn reaction button state is empty")
    return state


def _wait_for_reaction_state(
    page,
    expected_state: str,
    *,
    timeout_ms: int = _REACTION_VERIFY_WAIT_MS,
) -> bool:
    elapsed_ms = 0
    while elapsed_ms <= timeout_ms:
        buttons = _visible_reaction_buttons(page)
        if len(buttons) == 1 and _reaction_state(buttons[0]) == expected_state:
            return True
        if elapsed_ms == timeout_ms:
            break
        wait_ms = min(_REACTION_VERIFY_POLL_MS, timeout_ms - elapsed_ms)
        page.wait_for_timeout(wait_ms)
        elapsed_ms += wait_ms
    return False


def _raise_if_login_wall(page) -> None:
    current_url = getattr(page, "url", "") or ""
    if "/login" in current_url or "/checkpoint" in current_url:
        raise FeedLikeSendError(f"LinkedIn session is not authenticated: {current_url}")
