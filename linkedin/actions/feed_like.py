"""Idempotent UI-only Like action for an exact LinkedIn feed post."""
from __future__ import annotations

import logging
from enum import Enum

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from linkedin.feed_collection import post_url_for_activity_urn

logger = logging.getLogger(__name__)

REACTION_BUTTON_SELECTOR = ", ".join(
    [
        'button[aria-label^="Reaction button state:" i]',
        'button[aria-label^="React " i][aria-pressed]',
        'button[aria-label^="Like" i][aria-pressed]',
        'button[aria-label^="Unlike" i][aria-pressed]',
        'button.react-button__trigger[aria-pressed]',
    ]
)
_REACTION_STATE_PREFIX = "reaction button state:"
_REACTION_DISCOVERY_WAIT_MS = 15_000
_REACTION_VERIFY_WAIT_MS = 10_000
_REACTION_VERIFY_POLL_MS = 250
_COMMENT_REACTION_ANCESTOR_SELECTOR = ", ".join(
    [
        ".comments-comment-entity",
        '[data-id*="urn:li:comment"]',
        '[data-urn*="urn:li:comment"]',
        '[id^="replaceableComment_"]',
    ]
)
_KNOWN_REACTIONS = {"like", "celebrate", "support", "love", "insightful", "funny"}


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
        _raise_if_login_wall(page)

        button = _wait_for_unique_reaction_button(page)
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
            if button.is_visible() and not _is_comment_reaction_button(button):
                visible.append(button)
        except PlaywrightError:
            continue
    return visible


def _is_comment_reaction_button(button) -> bool:
    return bool(
        button.evaluate(
            "(el, selector) => Boolean(el.closest(selector))",
            _COMMENT_REACTION_ANCESTOR_SELECTOR,
        )
    )


def _wait_for_unique_reaction_button(
    page,
    *,
    timeout_ms: int = _REACTION_DISCOVERY_WAIT_MS,
):
    elapsed_ms = 0
    last_count = 0
    while elapsed_ms <= timeout_ms:
        buttons = _visible_reaction_buttons(page)
        last_count = len(buttons)
        if last_count == 1:
            return buttons[0]
        if elapsed_ms == timeout_ms:
            break
        wait_ms = min(_REACTION_VERIFY_POLL_MS, timeout_ms - elapsed_ms)
        page.wait_for_timeout(wait_ms)
        elapsed_ms += wait_ms

    diagnostics = _reaction_button_diagnostics(page)
    raise FeedLikeSendError(
        "Expected one visible primary-post reaction button after "
        f"{timeout_ms / 1000:g}s, found {last_count}; "
        f"url={getattr(page, 'url', '')}; candidates={diagnostics}"
    )


def _reaction_state(button) -> str:
    try:
        label = (button.get_attribute("aria-label") or "").strip().lower()
        pressed = (button.get_attribute("aria-pressed") or "").strip().lower()
        reaction_type = (button.get_attribute("data-reaction-type") or "").strip().lower()
        text = (button.inner_text() or "").strip().lower()
    except PlaywrightError as exc:
        raise FeedLikeSendError(str(exc)) from exc

    if label.startswith(_REACTION_STATE_PREFIX):
        state = label[len(_REACTION_STATE_PREFIX):].strip()
        if state:
            return state

    if pressed == "false":
        return "no reaction"
    if pressed == "true":
        if reaction_type in _KNOWN_REACTIONS:
            return reaction_type
        if label.startswith("unlike"):
            return "like"
        if label.startswith("react "):
            reaction = label.removeprefix("react ").split()[0]
            if reaction in _KNOWN_REACTIONS:
                return reaction
        for value in (label, text):
            reaction = value.split()[0] if value else ""
            if reaction in _KNOWN_REACTIONS:
                return reaction

    raise FeedLikeSendError(
        "LinkedIn reaction button state is ambiguous "
        f"(aria-label={label!r}, aria-pressed={pressed!r})"
    )


def _reaction_button_diagnostics(page) -> str:
    try:
        locator = page.locator(REACTION_BUTTON_SELECTOR)
        count = min(locator.count(), 10)
    except PlaywrightError:
        return "unavailable"

    rows = []
    for index in range(count):
        button = locator.nth(index)
        try:
            rows.append(
                {
                    "label": (button.get_attribute("aria-label") or "")[:120],
                    "pressed": button.get_attribute("aria-pressed") or "",
                    "visible": button.is_visible(),
                    "comment": _is_comment_reaction_button(button),
                }
            )
        except PlaywrightError:
            rows.append({"detached": True})
    return repr(rows)


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
