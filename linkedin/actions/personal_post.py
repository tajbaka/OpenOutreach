"""UI-only personal LinkedIn post (text, image or document), from the daemon's browser over CDP.

Arian or Eddy posting an approved post from their own profile (user decision,
2026-11-02). Like the repost action, the worker attaches to the running daemon
browser over loopback CDP only and opens its own targets at their URLs.

Unverified live: the composer, media and document selectors were written on
2026-11-02 without a live LinkedIn check. They are aria/role/text based with
several candidates, every ambiguity fails before the final click, and the
first live post for each operator (text, image and document) should be
watched; see docs/linkedin-posts.md.

Error rules: anything raised before the final click is a ``PostSendError``
(nothing was posted, row ``failed``). Once the final click is attempted every
error is a ``PostUncertainError`` (row ``uncertain``, never retried). A post
is ``sent`` only with a verified new permalink.
"""
from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from linkedin.actions.repost import (
    _BLOCKED_DOCUMENT_JS,
    _EDITOR_TEXT_JS,
    _ELEMENT_CONTEXT_JS,
    _TOASTS_JS,
    _is_linkedin_url,
    _label_and_text,
    _normalize_text,
    _poll,
    _squash,
    _urn_from_href,
    ACTIVITY_URL,
    DIALOG_SELECTOR,
    EDITOR_SELECTOR,
    owned_page,
)
from linkedin.browser.nav import human_type
from linkedin.exceptions import (
    PostAttachmentError,
    PostBrowserUnavailableError,
    PostCheckpointError,
    PostCommentaryMismatchError,
    PostComposerError,
    PostControlNotFoundError,
    PostLoginWallError,
    PostRejectedError,
    PostSendError,
    PostUncertainError,
    PostUnverifiedError,
)
from linkedin.reposts import COMMENTARY_TYPE_MAX_DELAY_MS, COMMENTARY_TYPE_MIN_DELAY_MS, normalize_urn, post_url_for_urn

logger = logging.getLogger(__name__)

__all__ = ["attached_browser", "owned_page", "perform_post", "FEED_URL"]

FEED_URL = "https://www.linkedin.com/feed/"

_CDP_CONNECT_TIMEOUT_MS = 15_000
_CONTROL_WAIT_MS = 15_000
_MEDIA_WAIT_MS = 60_000
_TOAST_WAIT_MS = 30_000
_ACTIVITY_WAIT_MS = 20_000
_ACTIVITY_CARD_LIMIT = 10
_MAX_EDITOR_STEPS = 3

# Native ancestor checks only (Playwright's :has-text() is not valid CSS).
_CONTROL_ANCESTORS = 'button, [role="button"], [role="menuitem"]'
_MESSAGE_OVERLAY = '[data-view-name="message-overlay-conversation-bubble-item"]'
_OUTSIDE_FEED_BOX = ", ".join(['[role="dialog"]', "aside", _MESSAGE_OVERLAY])

START_POST_SELECTOR = ", ".join([
    "button.share-box-feed-entry__trigger",
    'button[aria-label^="Start a post" i]',
    '[role="button"][aria-label^="Start a post" i]',
    'button:has-text("Start a post")',
    '[role="button"]:has-text("Start a post")',
])
IMAGE_BUTTON_SELECTOR = ", ".join([
    'button[aria-label*="Add media" i]',
    '[role="button"][aria-label*="Add media" i]',
    'button[aria-label*="Add a photo" i]',
    '[role="button"][aria-label*="Add a photo" i]',
])
DOCUMENT_BUTTON_SELECTOR = ", ".join([
    'button[aria-label*="Add a document" i]',
    '[role="button"][aria-label*="Add a document" i]',
    'button:has-text("Add a document")',
    '[role="button"]:has-text("Add a document")',
    '[role="menuitem"]:has-text("Add a document")',
])
MORE_BUTTON_SELECTOR = ", ".join([
    'button[aria-label="More" i]',
    'button[aria-label*="More options" i]',
    '[role="button"][aria-label="More" i]',
])
CHOOSE_FILE_SELECTOR = ", ".join([
    'button:has-text("Choose file")',
    '[role="button"]:has-text("Choose file")',
    'label:has-text("Choose file")',
])
TITLE_INPUT_SELECTOR = ", ".join([
    'input[aria-label*="title" i]',
    'input[name*="title" i]',
    'input[id*="title" i]',
    'input[placeholder*="title" i]',
])
EDITOR_STEP_SELECTOR = ", ".join([
    'button:has-text("Next")',
    'button:has-text("Done")',
    '[role="button"]:has-text("Next")',
    '[role="button"]:has-text("Done")',
])
POST_BUTTON_SELECTOR = ", ".join([
    "button.share-actions__primary-action",
    'button:has-text("Post")',
    '[role="button"]:has-text("Post")',
])
IMAGE_PREVIEW_SELECTOR = ", ".join([
    'img[src^="blob:"]',
    '[class*="image-preview"] img',
    '[class*="share-media"] img',
    "[data-test-image-preview] img",
])
DOCUMENT_PREVIEW_SELECTOR = ", ".join([
    '[class*="document-preview"]',
    '[class*="share-document"]',
    "[data-test-document-preview]",
    'iframe[title*="document" i]',
])

_LOGIN_MARKERS = ("/login", "/checkpoint", "/authwall", "/uas/")
_SUCCESS_WORDS = ("success", "was posted", "post shared", "posted", "view post")
_FINGERPRINT_CHARS = 80

# The member's own activity, with a text sample per card to match the new post.
_ACTIVITY_POSTS_JS = """(limit) => {
    const selector = ['activity', 'share', 'ugcPost'].flatMap((kind) => [
        `[data-urn^="urn:li:${kind}:"]`, `[data-id^="urn:li:${kind}:"]`]).join(', ');
    const cards = [];
    for (const el of document.querySelectorAll(selector)) {
        if (el.parentElement && el.parentElement.closest(selector)) continue;
        cards.push({
            urn: el.getAttribute('data-urn') || el.getAttribute('data-id') || '',
            text: (el.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 1500),
        });
        if (cards.length >= limit) break;
    }
    return cards;
}"""


# ---------------------------------------------------------------------------
# Browser attachment
# ---------------------------------------------------------------------------


@contextmanager
def attached_browser(port: int):
    """Attach to the daemon browser over loopback CDP; leaving only disconnects."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{int(port)}", timeout=_CDP_CONNECT_TIMEOUT_MS)
        except PlaywrightError as exc:
            raise PostBrowserUnavailableError(error_type=type(exc).__name__) from None
        if not browser.contexts:
            raise PostBrowserUnavailableError("No shared browser context")
        yield browser


# ---------------------------------------------------------------------------
# Page checks and controls
# ---------------------------------------------------------------------------


def _raise_if_login_wall(page) -> None:
    current_url = getattr(page, "url", "") or ""
    if any(marker in current_url for marker in _LOGIN_MARKERS):
        raise PostLoginWallError("LinkedIn session is not authenticated")


def _assert_feed_page(page) -> None:
    _raise_if_login_wall(page)
    if not _is_linkedin_url(page.url) or not urlsplit(page.url).path.startswith("/feed"):
        raise PostControlNotFoundError("Feed page redirected away from the feed")
    if page.evaluate(_BLOCKED_DOCUMENT_JS):
        raise PostCheckpointError("LinkedIn checkpoint or captcha")


def _candidates(scope, selector: str, *, excluded: str = "", enabled: bool = False, limit: int = 20) -> list:
    """Visible (and optionally enabled) matches, skipping excluded and nested ones."""
    locator = scope.locator(selector)
    try:
        count = min(locator.count(), limit)
    except PlaywrightError:
        return []
    found = []
    for index in range(count):
        item = locator.nth(index)
        try:
            if not item.is_visible() or (enabled and not item.is_enabled()):
                continue
            context = item.evaluate(_ELEMENT_CONTEXT_JS, [excluded, _CONTROL_ANCESTORS]) or {}
        except PlaywrightError:
            continue
        if context.get("excluded") or context.get("nested"):
            continue
        found.append(item)
    return found


def _single(scope, page, selector: str, accept, error, message: str, *, excluded: str = "", enabled: bool = False,
            wait_ms: int = _CONTROL_WAIT_MS):
    """Exactly one visible control whose (label, text) ``accept`` approves, or ``error``."""

    def find():
        matches = [
            item for item in _candidates(scope, selector, excluded=excluded, enabled=enabled)
            if accept(*_label_and_text(item))
        ]
        return matches[0] if len(matches) == 1 else None

    found = _poll(page, find, wait_ms)
    if found is None:
        raise error(message)
    return found


def _start_post_trigger(page):
    return _single(
        page, page, START_POST_SELECTOR, lambda label, text: "start a post" in label or text.startswith("start a post"),
        PostControlNotFoundError, "Expected one visible Start a post control on the feed", excluded=_OUTSIDE_FEED_BOX,
    )


def _composer(page):
    def find():
        pairs = []
        for dialog in _candidates(page, DIALOG_SELECTOR, excluded=_MESSAGE_OVERLAY):
            for editor in _candidates(dialog, EDITOR_SELECTOR):
                pairs.append((dialog, editor))
        return pairs[0] if len(pairs) == 1 else None

    found = _poll(page, find, _CONTROL_WAIT_MS)
    if found is None:
        raise PostComposerError("Expected one text editor in the post composer")
    return found


def _editor_text(editor) -> str:
    return _normalize_text(editor.evaluate(_EDITOR_TEXT_JS) or "")


def _assert_commentary(editor, commentary: str) -> None:
    if _editor_text(editor) != _normalize_text(commentary):
        raise PostCommentaryMismatchError("Editor text differs from the approved post text")


def _has_preview(dialog, kind: str, title: str = "") -> bool:
    if kind == "image":
        return bool(_candidates(dialog, IMAGE_PREVIEW_SELECTOR))
    if _candidates(dialog, DOCUMENT_PREVIEW_SELECTOR):
        return True
    # The document card shows its title; an exact title in the composer is proof too.
    try:
        return bool(title) and _squash(title) in _squash(dialog.inner_text() or "")
    except PlaywrightError:
        return False


def _choose_file(page, control, path) -> None:
    """Open the file picker from ``control`` and give it exactly the approved file."""
    with page.expect_file_chooser(timeout=_CONTROL_WAIT_MS) as chooser:
        control.click(delay=150)
    chooser.value.set_files(str(path))


def _finish_media_editor(page, kind: str, title: str = ""):
    """Step through LinkedIn's Next/Done screens until the composer shows the attachment."""
    steps = 0

    def back_in_composer():
        try:
            dialog, editor = _composer_now(page)
        except PostComposerError:
            return None
        return (dialog, editor) if _has_preview(dialog, kind, title) else None

    while True:
        found = _poll(page, back_in_composer, 2000)
        if found is not None:
            return found
        if steps >= _MAX_EDITOR_STEPS:
            raise PostAttachmentError("The attachment did not appear in the post composer")
        step = _single(
            page, page, EDITOR_STEP_SELECTOR, lambda label, text: text in ("next", "done") or label in ("next", "done"),
            PostAttachmentError, "Expected one Next or Done step after choosing the file", enabled=True,
            wait_ms=_MEDIA_WAIT_MS,
        )
        step.click(delay=150)
        steps += 1
        page.wait_for_timeout(1500)


def _composer_now(page):
    """The composer if it is showing now (one dialog with one editor), without waiting."""
    pairs = [
        (dialog, editor)
        for dialog in _candidates(page, DIALOG_SELECTOR, excluded=_MESSAGE_OVERLAY)
        for editor in _candidates(dialog, EDITOR_SELECTOR)
    ]
    if len(pairs) != 1:
        raise PostComposerError("Composer not showing")
    return pairs[0]


def _attach_image(page, dialog, path):
    control = _single(
        dialog, page, IMAGE_BUTTON_SELECTOR, lambda label, text: "media" in label or "photo" in label,
        PostAttachmentError, "Expected one Add media control in the composer",
    )
    _choose_file(page, control, path)
    return _finish_media_editor(page, "image")


def _attach_document(page, dialog, path, title: str):
    is_document = lambda label, text: "add a document" in label or "add a document" in text  # noqa: E731
    in_composer = [item for item in _candidates(dialog, DOCUMENT_BUTTON_SELECTOR) if is_document(*_label_and_text(item))]
    if len(in_composer) > 1:
        raise PostAttachmentError("Expected one Add a document control in the composer")
    if in_composer:
        control = in_composer[0]
    else:
        # Otherwise it sits behind the composer's More menu, which may open outside the dialog.
        _single(
            dialog, page, MORE_BUTTON_SELECTOR, lambda label, text: label.startswith("more"),
            PostAttachmentError, "Expected one More control in the composer",
        ).click(delay=150)
        page.wait_for_timeout(700)
        control = _single(
            page, page, DOCUMENT_BUTTON_SELECTOR, is_document, PostAttachmentError, "Expected one Add a document control",
        )
    control.click(delay=150)
    page.wait_for_timeout(700)
    chooser = _single(
        page, page, CHOOSE_FILE_SELECTOR, lambda label, text: "choose file" in text or "choose file" in label,
        PostAttachmentError, "Expected one Choose file control for the document",
    )
    _choose_file(page, chooser, path)
    field = _single(
        page, page, TITLE_INPUT_SELECTOR, lambda label, text: True, PostAttachmentError,
        "Expected one document title field", wait_ms=_MEDIA_WAIT_MS,
    )
    if (field.input_value() or "").strip():
        field.fill("")
    human_type(field, title, min_delay=COMMENTARY_TYPE_MIN_DELAY_MS, max_delay=COMMENTARY_TYPE_MAX_DELAY_MS)
    page.wait_for_timeout(500)
    if _normalize_text(field.input_value() or "") != _normalize_text(title):
        raise PostAttachmentError("Document title differs from the approved title")
    return _finish_media_editor(page, "document", title)


def _post_button(page, dialog):
    return _single(
        dialog, page, POST_BUTTON_SELECTOR, lambda label, text: text == "post" or label == "post",
        PostComposerError, "Expected one enabled Post button in the composer", enabled=True,
    )


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def _toast_permalink(page) -> str | None:
    """A success toast linking to a post is proof; an error toast after the click is uncertain."""

    def find():
        for toast in page.evaluate(_TOASTS_JS) or []:
            if toast.get("type") == "error":
                raise PostRejectedError("LinkedIn showed an error toast after the final click")
            text = toast.get("text") or ""
            if toast.get("type") != "success" and not any(word in text for word in _SUCCESS_WORDS):
                continue
            for href in toast.get("hrefs") or []:
                urn = _urn_from_href(href)
                if urn:
                    return post_url_for_urn(urn)
        return None

    return _poll(page, find, _TOAST_WAIT_MS)


def _fingerprint(commentary: str, title: str) -> str:
    """Text the new post's activity card must contain: the post's opening, or a document's title."""
    return _squash(commentary)[:_FINGERPRINT_CHARS] or _squash(title)[:_FINGERPRINT_CHARS]


def _activity_posts(page) -> list[dict]:
    cards = []
    for card in page.evaluate(_ACTIVITY_POSTS_JS, _ACTIVITY_CARD_LIMIT) or []:
        urn = normalize_urn(card.get("urn"))
        if urn:
            cards.append({"urn": urn, "text": _squash(card.get("text") or "")})
    return cards


def _on_activity_page(page) -> bool:
    _raise_if_login_wall(page)
    return _is_linkedin_url(page.url) and "/recent-activity/" in urlsplit(page.url).path


def _activity_baseline(open_page) -> set[str] | None:
    """Pre-click card URNs on the member's own activity; None disables that proof."""
    try:
        with open_page(ACTIVITY_URL) as page:
            if not _on_activity_page(page):
                return None
            page.wait_for_timeout(2000)
            return {card["urn"] for card in _activity_posts(page)}
    except (PlaywrightError, PlaywrightTimeoutError, TimeoutError) as exc:
        logger.warning("Post activity baseline unavailable (%s); toast proof only", type(exc).__name__)
        return None


def _activity_permalink(open_page, baseline: set[str], fingerprint: str) -> str | None:
    """The one new activity card, absent before the click, carrying the post's own text."""
    if not fingerprint:
        return None

    with open_page(ACTIVITY_URL) as page:
        if not _on_activity_page(page):
            return None

        def find():
            new = [card for card in _activity_posts(page) if card["urn"] not in baseline and fingerprint in card["text"]]
            return post_url_for_urn(new[0]["urn"]) if len(new) == 1 else None

        found = _poll(page, find, _ACTIVITY_WAIT_MS // 2)
        if found is None:
            page.reload(wait_until="domcontentloaded", timeout=30_000)
            found = _poll(page, find, _ACTIVITY_WAIT_MS // 2)
        return found


# ---------------------------------------------------------------------------
# The post
# ---------------------------------------------------------------------------


def perform_post(open_page, *, commentary: str, media: dict | None, on_click_attempt) -> str:
    """Publish one post from the member's own profile and return its verified permalink.

    ``media`` is None, or ``{"kind": "image"|"document", "path": <verified local file>,
    "title": <document title>, ...}``. ``open_page(url)`` returns a context
    manager yielding a page owned by this run. ``on_click_attempt`` persists
    the attempt and must succeed before the final click; if it raises, nothing
    is clicked.
    """
    kind = (media or {}).get("kind") or ""
    title = (media or {}).get("title") or ""
    click_attempted = False
    try:
        baseline = _activity_baseline(open_page)
        with open_page(FEED_URL) as page:
            _assert_feed_page(page)
            page.wait_for_timeout(1500)
            _start_post_trigger(page).click(delay=150)
            page.wait_for_timeout(800)
            dialog, editor = _composer(page)
            # A restored draft (text or attachment) is refused, never posted.
            if _editor_text(editor).strip() or _candidates(dialog, IMAGE_PREVIEW_SELECTOR) or _candidates(
                dialog, DOCUMENT_PREVIEW_SELECTOR
            ):
                raise PostComposerError("Post composer was not empty")

            if kind == "image":
                dialog, editor = _attach_image(page, dialog, media["path"])
            elif kind == "document":
                dialog, editor = _attach_document(page, dialog, media["path"], title)
            if _editor_text(editor).strip():
                raise PostComposerError("Post composer gained text before typing")

            if commentary:
                human_type(
                    editor, commentary,
                    min_delay=COMMENTARY_TYPE_MIN_DELAY_MS, max_delay=COMMENTARY_TYPE_MAX_DELAY_MS,
                )
                page.wait_for_timeout(800)
            _assert_commentary(editor, commentary)
            final_control = _post_button(page, dialog)
            # Re-read right before the click: typeahead can still rewrite text,
            # and the attachment must still be there.
            _assert_commentary(editor, commentary)
            if kind and not _has_preview(dialog, kind, title):
                raise PostAttachmentError("The attachment disappeared before Post")

            on_click_attempt()
            click_attempted = True
            final_control.click(delay=200)
            page.wait_for_timeout(2000)
            result_url = _toast_permalink(page)

        if result_url is None and baseline is not None:
            result_url = _activity_permalink(open_page, baseline, _fingerprint(commentary, title))
        if result_url is None:
            raise PostUnverifiedError("Final click attempted; no new permalink was verified")
        return result_url
    except PostUncertainError:
        raise
    except PostSendError as exc:
        if click_attempted:
            raise PostUncertainError(str(exc), error_type=exc.error_type) from exc
        raise
    except (PlaywrightError, PlaywrightTimeoutError, TimeoutError) as exc:
        if click_attempted:
            raise PostUncertainError("Browser error after the final click", error_type=type(exc).__name__) from exc
        raise PostSendError("Browser error before the final click", error_type=type(exc).__name__) from exc
