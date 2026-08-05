"""UI-only LinkedIn feed comment action."""
from __future__ import annotations

import logging

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from linkedin.browser.nav import human_type
from linkedin.feed_collection import post_url_for_activity_urn

logger = logging.getLogger(__name__)

COMMENT_BUTTON_SELECTORS = [
    'button[aria-label*="Comment"]',
    'button:has-text("Comment")',
    '[role="button"][aria-label*="Comment"]',
]

COMMENT_EDITOR_SELECTORS = [
    'div[role="textbox"][contenteditable="true"][aria-label*="comment"]',
    'div.ql-editor[contenteditable="true"]',
    '.comments-comment-box__editor [contenteditable="true"]',
    '[contenteditable="true"][data-placeholder*="comment"]',
]

COMMENT_SUBMIT_SELECTORS = [
    'button.comments-comment-box__submit-button:not([disabled])',
    'button[aria-label*="Post comment"]:not([disabled])',
    'button[aria-label*="Comment"]:not([disabled]):has-text("Post")',
    'button:has-text("Post"):not([disabled])',
]


class FeedCommentSendError(RuntimeError):
    """Raised when the comment failed before a submit attempt."""


class FeedCommentUncertainError(FeedCommentSendError):
    """Raised when LinkedIn may have accepted the comment."""


def comment_on_feed_post(
    session,
    *,
    post_url: str = "",
    activity_urn: str = "",
    comment: str,
    on_submit_attempt=None,
) -> None:
    """Post a public LinkedIn comment through the visible UI only."""
    target_url = (post_url or "").strip() or post_url_for_activity_urn(activity_urn)
    if not target_url:
        raise FeedCommentSendError("Feed post has no URL or activity URN")

    page = _new_page(session)
    close_page = page is not getattr(session, "page", None)
    submit_attempted = False
    try:
        page.goto(target_url, wait_until="domcontentloaded", timeout=45_000)
        page.wait_for_timeout(2000)
        _raise_if_login_wall(page)

        comment_button = _first_visible(page, COMMENT_BUTTON_SELECTORS)
        if comment_button is not None:
            comment_button.click(delay=150)
            page.wait_for_timeout(700)

        editor = _first_visible(page, COMMENT_EDITOR_SELECTORS)
        if editor is None:
            raise FeedCommentSendError("LinkedIn comment editor was not found")

        human_type(editor, comment)
        page.wait_for_timeout(500)

        submit = _first_visible(page, COMMENT_SUBMIT_SELECTORS)
        if submit is None:
            raise FeedCommentSendError("LinkedIn comment submit button was not found")

        if on_submit_attempt is not None:
            on_submit_attempt()
        submit_attempted = True
        submit.click(delay=200)
        page.wait_for_timeout(2500)

        if not _comment_visible(page, comment):
            raise FeedCommentUncertainError(
                "Submitted comment but could not verify it on the post",
            )
        logger.info("Feed comment submitted on %s", target_url)
    except FeedCommentSendError:
        raise
    except (PlaywrightError, PlaywrightTimeoutError, TimeoutError) as exc:
        if submit_attempted:
            raise FeedCommentUncertainError(str(exc)) from exc
        raise FeedCommentSendError(str(exc)) from exc
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
        raise FeedCommentSendError("No browser page available for feed comment")
    return page


def _first_visible(page, selectors: list[str]):
    for selector in selectors:
        locator = page.locator(selector)
        try:
            count = min(locator.count(), 10)
        except PlaywrightError:
            continue
        for index in range(count):
            item = locator.nth(index)
            try:
                if item.is_visible():
                    return item
            except PlaywrightError:
                continue
    return None


def _raise_if_login_wall(page) -> None:
    current_url = getattr(page, "url", "") or ""
    if "/login" in current_url or "/checkpoint" in current_url:
        raise FeedCommentSendError(f"LinkedIn session is not authenticated: {current_url}")


def _comment_visible(page, comment: str) -> bool:
    snippet = " ".join((comment or "").split())[:120]
    if not snippet:
        return False
    try:
        body_text = page.locator("body").inner_text(timeout=5000)
    except (PlaywrightError, PlaywrightTimeoutError):
        return False
    return snippet in " ".join((body_text or "").split())
