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
    'button.comments-comment-box__submit-button',
    'button[aria-label*="Post comment" i]',
    'button[type="submit"]',
    '[role="button"][aria-label*="Post comment" i]',
]

COMMENT_SUBMIT_TEXT_FALLBACK_SELECTORS = [
    'button[aria-label="Comment" i]',
    'button:has-text("Post")',
    'button:has-text("Comment")',
    '[role="button"][aria-label="Comment" i]',
    '[role="button"]:has-text("Post")',
    '[role="button"]:has-text("Comment")',
]

COMMENT_ITEM_SELECTORS = [
    ".comments-comment-item",
    ".comments-comments-list__comment-item",
    '[componentkey^="replaceableComment_urn:li:comment"]',
    '[data-id^="urn:li:comment"]',
    '[data-urn^="urn:li:comment"]',
    "section.comment:has(.comment__text)",
]

_COMMENT_SUBMIT_WAIT_MS = 10_000
_COMMENT_SUBMIT_POLL_MS = 250
_COMMENT_SUBMIT_TEXT_SCOPE_LIMIT = 6
_COMMENT_VERIFY_WAIT_MS = 15_000
_COMMENT_VERIFY_POLL_MS = 250


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

        submit = _wait_for_comment_submit(page, editor)
        if submit is None:
            raise FeedCommentSendError(
                "LinkedIn comment submit button was not found after waiting for "
                "an enabled Post/Comment control in the comment composer"
            )

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


def _wait_for_comment_submit(page, editor, timeout_ms: int = _COMMENT_SUBMIT_WAIT_MS):
    """Wait for an enabled submit control inside the active comment composer."""
    elapsed_ms = 0
    while elapsed_ms <= timeout_ms:
        scopes = _comment_submit_scopes(editor)
        for scope in scopes:
            submit = _first_enabled_visible(scope, COMMENT_SUBMIT_SELECTORS)
            if submit is not None:
                return submit
        for scope in scopes[:_COMMENT_SUBMIT_TEXT_SCOPE_LIMIT]:
            submit = _first_enabled_visible(
                scope,
                COMMENT_SUBMIT_TEXT_FALLBACK_SELECTORS,
            )
            if submit is not None:
                return submit
        if elapsed_ms == timeout_ms:
            break
        wait_ms = min(_COMMENT_SUBMIT_POLL_MS, timeout_ms - elapsed_ms)
        page.wait_for_timeout(wait_ms)
        elapsed_ms += wait_ms
    return None


def _comment_submit_scopes(editor) -> list:
    """Return nearby composer ancestors, from narrowest to broadest."""
    scopes = []
    for depth in range(1, 7):
        ancestor = editor.locator(f"xpath=ancestor::*[{depth}]")
        try:
            if ancestor.count() == 0:
                break
        except PlaywrightError:
            break
        scopes.append(ancestor.first)
    return scopes


def _first_enabled_visible(scope, selectors: list[str]):
    for selector in selectors:
        locator = scope.locator(selector)
        try:
            count = min(locator.count(), 10)
        except PlaywrightError:
            continue
        for index in range(count):
            item = locator.nth(index)
            try:
                if item.is_visible() and item.is_enabled():
                    return item
            except PlaywrightError:
                continue
    return None


def _raise_if_login_wall(page) -> None:
    current_url = getattr(page, "url", "") or ""
    if "/login" in current_url or "/checkpoint" in current_url:
        raise FeedCommentSendError(f"LinkedIn session is not authenticated: {current_url}")


def _comment_visible(
    page,
    comment: str,
    *,
    timeout_ms: int = _COMMENT_VERIFY_WAIT_MS,
) -> bool:
    """Wait until the submitted text appears in a rendered comment item."""
    snippet = " ".join((comment or "").split())[:120]
    if not snippet:
        return False

    elapsed_ms = 0
    while elapsed_ms <= timeout_ms:
        if _visible_comment_item_contains(page, snippet):
            return True
        if elapsed_ms == timeout_ms:
            break
        wait_ms = min(_COMMENT_VERIFY_POLL_MS, timeout_ms - elapsed_ms)
        page.wait_for_timeout(wait_ms)
        elapsed_ms += wait_ms
    return False


def _visible_comment_item_contains(page, snippet: str) -> bool:
    candidates = page.locator(", ".join(COMMENT_ITEM_SELECTORS))
    try:
        count = min(candidates.count(), 100)
    except PlaywrightError:
        return False
    for index in range(count):
        candidate = candidates.nth(index)
        try:
            if not candidate.is_visible():
                continue
            text = candidate.inner_text(timeout=1000)
        except (PlaywrightError, PlaywrightTimeoutError):
            continue
        if snippet in " ".join((text or "").split()):
            return True
    return False
