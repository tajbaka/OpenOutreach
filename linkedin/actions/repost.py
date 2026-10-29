"""UI-only LinkedIn repost of one exact post, from the daemon's browser over CDP.

The worker attaches to the running daemon browser over loopback CDP only (never
AccountSession/StandaloneLinkedInSession, which launch and log in) and opens
its own targets directly at their URLs. Leaving ``attached_browser`` only
disconnects; the daemon child owns the browser.

Selectors are UNVERIFIED against live LinkedIn (written 2026-10-29). They are
aria/role/text based with several candidates, and every ambiguity fails before
the final click. The first live repost per operator must be supervised.

Error rules: anything raised before the final click is a ``RepostSendError``
(nothing was mutated, row ``failed``). Once the final click is attempted every
error is a ``RepostUncertainError`` (row ``uncertain``, never retried). A
repost is ``sent`` only with a verified new permalink.
"""
from __future__ import annotations

import logging
import re
import time
from contextlib import contextmanager
from urllib.parse import unquote, urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from linkedin.browser.nav import human_type
from linkedin.exceptions import (
    RepostBrowserUnavailableError,
    RepostCheckpointError,
    RepostCommentaryMismatchError,
    RepostComposerError,
    RepostControlNotFoundError,
    RepostLoginWallError,
    RepostOptionNotFoundError,
    RepostPostUnavailableError,
    RepostRejectedError,
    RepostSendError,
    RepostUncertainError,
    RepostUnverifiedError,
)
from linkedin.reposts import (
    COMMENTARY_TYPE_MAX_DELAY_MS,
    COMMENTARY_TYPE_MIN_DELAY_MS,
    normalize_urn,
    post_url_for_urn,
    urn_from_post_url,
)

logger = logging.getLogger(__name__)

# Unverified: LinkedIn is expected to redirect /in/me/ to the signed-in member.
ACTIVITY_URL = "https://www.linkedin.com/in/me/recent-activity/all/"

_CDP_CONNECT_TIMEOUT_MS = 15_000
_TARGET_TIMEOUT_MS = 30_000
_CONTROL_WAIT_MS = 15_000
_POLL_MS = 250
_TOAST_WAIT_MS = 15_000
_ACTIVITY_WAIT_MS = 20_000
_ACTIVITY_CARD_LIMIT = 10

# The post's own Repost menu trigger (not the "N reposts" count, not comments).
REPOST_TRIGGER_SELECTOR = ", ".join([
    'button[aria-label^="Repost" i]',
    '[role="button"][aria-label^="Repost" i]',
    'button.social-reshare-button',
    'button:has-text("Repost")',
    '[role="button"]:has-text("Repost")',
])
_TRIGGER_EXCLUDED_ANCESTORS = ", ".join([
    ".comments-comment-entity",
    ".comments-comment-item",
    '[data-id*="urn:li:comment"]',
    '[data-urn*="urn:li:comment"]',
    '[id^="replaceableComment_"]',
    '[role="dialog"]',
    "aside",
])
_COMMENT_ANCESTORS = ", ".join([
    ".comments-comment-entity",
    ".comments-comment-item",
    '[data-id*="urn:li:comment"]',
    '[data-urn*="urn:li:comment"]',
    '[id^="replaceableComment_"]',
])
MENU_ITEM_SELECTOR = ", ".join([
    '[role="menuitem"]',
    '[role="menu"] [role="button"]',
    '[role="menu"] button',
    ".artdeco-dropdown__item",
    '.artdeco-dropdown__content [role="button"]',
])
DIALOG_SELECTOR = ", ".join([
    '[role="dialog"]',
    ".share-box-v2__modal",
    ".artdeco-modal",
])
EDITOR_SELECTOR = ", ".join([
    'div.ql-editor[contenteditable="true"]',
    '[role="textbox"][contenteditable="true"]',
    '[contenteditable="true"][aria-label*="text editor" i]',
])
POST_BUTTON_SELECTOR = ", ".join([
    "button.share-actions__primary-action",
    'button:has-text("Post")',
    '[role="button"]:has-text("Post")',
])

_REPOST_LABEL_RE = re.compile(r"^repost\b")
_LOGIN_MARKERS = ("/login", "/checkpoint", "/authwall", "/uas/")
_SUCCESS_WORDS = ("success", "reposted", "was posted", "post shared")
_INVISIBLE = dict.fromkeys(map(ord, "\u200b\ufeff"), None)

# Page scripts. They return only booleans, URNs/links or classification text
# that is never logged or stored.
_BLOCKED_DOCUMENT_JS = """() => Boolean(document.querySelector(
    'iframe[src*="captcha"], form[action*="checkpoint"], #captcha-internal'))"""
_ELEMENT_CONTEXT_JS = """(el, [excluded, self]) => ({
    excluded: Boolean(excluded && el.closest(excluded)),
    nested: Boolean(self && el.parentElement && el.parentElement.closest(self)),
})"""
_CARD_URNS_JS = """(el) => {
    const container = el.closest('[data-urn], [data-id], [data-activity-urn]');
    if (!container) return [];
    const found = new Set();
    for (const name of ['data-urn', 'data-id', 'data-activity-urn']) {
        const value = container.getAttribute(name) || '';
        for (const m of value.matchAll(/urn:li:(?:activity|ugcPost|share):\\d+/g)) found.add(m[0]);
    }
    return Array.from(found);
}"""
# Rebuild typed text from editor blocks: <br> is a line break, each block is
# a line, and an empty <p><br></p> block is an empty line.
_EDITOR_TEXT_JS = """(el) => {
    const textOf = (node) => {
        let out = '';
        node.childNodes.forEach((child) => {
            if (child.nodeType === Node.TEXT_NODE) out += child.data;
            else if (child.nodeName === 'BR') out += '\\n';
            else out += textOf(child);
        });
        return out;
    };
    const blocks = Array.from(el.children).filter(
        (child) => /^(P|DIV|LI|H[1-6]|BLOCKQUOTE|PRE)$/.test(child.tagName));
    if (!blocks.length) return textOf(el);
    return blocks.map((block) => textOf(block).replace(/\\n$/, '')).join('\\n');
}"""
_TOASTS_JS = """() => {
    const selector = '[data-test-artdeco-toast-item-type], .artdeco-toast-item, [role="alert"], [role="status"]';
    const out = [];
    for (const el of document.querySelectorAll(selector)) {
        if (el.parentElement && el.parentElement.closest(selector)) continue;
        if (!el.checkVisibility({visibilityProperty: true, checkVisibilityCSS: true})) continue;
        const text = (el.innerText || '').replace(/\\s+/g, ' ').trim().toLowerCase().slice(0, 300);
        if (!text) continue;
        out.push({
            type: (el.getAttribute('data-test-artdeco-toast-item-type') || '').toLowerCase(),
            text,
            hrefs: Array.from(el.querySelectorAll('a[href]')).map((a) => a.href).slice(0, 10),
        });
    }
    return out;
}"""
_ACTIVITY_CARDS_JS = """(limit) => {
    const selector = ['activity', 'share', 'ugcPost'].flatMap((kind) => [
        `[data-urn^="urn:li:${kind}:"]`, `[data-id^="urn:li:${kind}:"]`]).join(', ');
    const pattern = /urn(?::|%3A)li(?::|%3A)(activity|ugcPost|share)(?::|%3A)(\\d+)/gi;
    const cards = [];
    for (const el of document.querySelectorAll(selector)) {
        if (el.parentElement && el.parentElement.closest(selector)) continue;
        const refs = new Set();
        for (const m of el.outerHTML.matchAll(pattern)) refs.add(`urn:li:${m[1]}:${m[2]}`);
        cards.push({urn: el.getAttribute('data-urn') || el.getAttribute('data-id') || '', refs: Array.from(refs)});
        if (cards.length >= limit) break;
    }
    return cards;
}"""


# ---------------------------------------------------------------------------
# Browser attachment and owned targets
# ---------------------------------------------------------------------------


@contextmanager
def attached_browser(port: int):
    """Attach to the daemon browser over loopback CDP; leaving only disconnects."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{int(port)}", timeout=_CDP_CONNECT_TIMEOUT_MS)
        except PlaywrightError as exc:
            raise RepostBrowserUnavailableError(error_type=type(exc).__name__) from None
        if not browser.contexts:
            raise RepostBrowserUnavailableError("No shared browser context")
        yield browser


def _is_linkedin_url(url: str) -> bool:
    parts = urlsplit(url or "")
    return parts.scheme == "https" and parts.hostname == "www.linkedin.com"


@contextmanager
def owned_page(browser, url: str, *, on_target=None, on_closed=None, timeout_ms: int = _TARGET_TIMEOUT_MS):
    """Create a target whose initial document is ``url`` and own its exact ID.

    Mirrors the feed collector: never about:blank then goto, never pick a page
    by URL or tab order, and close only this target.
    """
    context = browser.contexts[0]
    cdp = browser.new_browser_cdp_session()
    target_id = None
    try:
        target_id = cdp.send("Target.createTarget", {"url": url})["targetId"]
        if on_target is not None:
            on_target(target_id)
        deadline = time.monotonic() + timeout_ms / 1000
        checked = set()
        while time.monotonic() < deadline:
            for page in context.pages:
                if page in checked:
                    continue
                session = context.new_cdp_session(page)
                try:
                    info = session.send("Target.getTargetInfo")["targetInfo"]
                finally:
                    session.detach()
                checked.add(page)
                if info["targetId"] == target_id:
                    page.wait_for_url(
                        _is_linkedin_url, wait_until="domcontentloaded",
                        timeout=max(1, int((deadline - time.monotonic()) * 1000)),
                    )
                    page.set_default_timeout(5000)
                    yield page
                    return
            # Pump Playwright events while the new page attaches.
            cdp.send("Target.getTargetInfo", {"targetId": target_id})
            time.sleep(0.1)
        raise PlaywrightTimeoutError("Repost-owned target did not become available")
    finally:
        try:
            if target_id is not None:
                cdp.send("Target.closeTarget", {"targetId": target_id})
                if on_closed is not None:
                    on_closed(target_id)
        except (PlaywrightError, OSError):
            logger.warning("Repost-owned target cleanup unavailable; the supervisor will retry it")
        finally:
            try:
                cdp.detach()
            except PlaywrightError:
                logger.warning("Repost target-control transport already closed")


# ---------------------------------------------------------------------------
# Page checks and controls
# ---------------------------------------------------------------------------


def _normalize_text(value: str) -> str:
    return (value or "").replace("\r\n", "\n").replace("\u00a0", " ").translate(_INVISIBLE)


def _squash(value: str) -> str:
    return " ".join((value or "").split()).strip().lower()


def _raise_if_login_wall(page) -> None:
    current_url = getattr(page, "url", "") or ""
    if any(marker in current_url for marker in _LOGIN_MARKERS):
        raise RepostLoginWallError("LinkedIn session is not authenticated")


def _assert_post_page(page) -> None:
    _raise_if_login_wall(page)
    parts = urlsplit(page.url or "")
    if not _is_linkedin_url(page.url) or not parts.path.startswith("/feed/update/urn:li:"):
        raise RepostPostUnavailableError("Post page redirected away from the post")
    if page.evaluate(_BLOCKED_DOCUMENT_JS):
        raise RepostCheckpointError("LinkedIn checkpoint or captcha")


def _page_url_urns(page) -> set[str]:
    match = re.search(r"/feed/update/([^/?#]+)", unquote(urlsplit(page.url or "").path))
    urn = normalize_urn(match.group(1)) if match else None
    return {urn} if urn else set()


def _context(element, excluded: str, own_selector: str) -> dict:
    return element.evaluate(_ELEMENT_CONTEXT_JS, [excluded, own_selector]) or {}


def _visible_candidates(scope, selector: str, *, excluded: str = "", enabled: bool = False, limit: int = 20) -> list:
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
            context = _context(item, excluded, selector)
        except PlaywrightError:
            continue
        if context.get("excluded") or context.get("nested"):
            continue
        found.append(item)
    return found


def _label_and_text(item) -> tuple[str, str]:
    """A control that detaches while polling matches nothing this round."""
    try:
        return _squash(item.get_attribute("aria-label") or ""), _squash(item.inner_text() or "")
    except PlaywrightError:
        return "", ""


def _poll(page, find, timeout_ms: int):
    """Call ``find`` until it returns a value, using page time (testable)."""
    elapsed = 0
    while True:
        found = find()
        if found is not None or elapsed >= timeout_ms:
            return found
        wait = min(_POLL_MS, timeout_ms - elapsed)
        page.wait_for_timeout(wait)
        elapsed += wait


def _repost_trigger(page):
    def find():
        triggers = []
        for item in _visible_candidates(page, REPOST_TRIGGER_SELECTOR, excluded=_TRIGGER_EXCLUDED_ANCESTORS):
            label, text = _label_and_text(item)
            if text == "repost" or _REPOST_LABEL_RE.match(label):
                triggers.append(item)
        return triggers[0] if len(triggers) == 1 else None

    trigger = _poll(page, find, _CONTROL_WAIT_MS)
    if trigger is None:
        raise RepostControlNotFoundError("Expected one visible Repost control on the post")
    # Opening a menu is not a mutation; a control that is not declared as a
    # menu trigger might repost instantly, so it is refused before clicking.
    if trigger.get_attribute("aria-haspopup") is None and trigger.get_attribute("aria-expanded") is None:
        raise RepostControlNotFoundError("Repost control is not a menu trigger")
    return trigger


def _option_kind(item) -> str | None:
    label, text = _label_and_text(item)
    value = text or label
    if "with your thoughts" in value or "with your thoughts" in label:
        return "thoughts"
    if value.startswith("repost") and not any(word in value for word in ("undo", "remove", "thoughts")):
        return "plain"
    return None


def _menu_option(page, kind: str):
    def find():
        options = [
            item for item in _visible_candidates(page, MENU_ITEM_SELECTOR, excluded=_COMMENT_ANCESTORS)
            if _option_kind(item) == kind
        ]
        return options[0] if len(options) == 1 else None

    option = _poll(page, find, _CONTROL_WAIT_MS)
    if option is None:
        raise RepostOptionNotFoundError(f"Expected one '{kind}' Repost option")
    return option


def _composer(page):
    def find():
        pairs = []
        for dialog in _visible_candidates(page, DIALOG_SELECTOR):
            for editor in _visible_candidates(dialog, EDITOR_SELECTOR):
                pairs.append((dialog, editor))
        return pairs[0] if len(pairs) == 1 else None

    found = _poll(page, find, _CONTROL_WAIT_MS)
    if found is None:
        raise RepostComposerError("Expected one commentary editor in the repost composer")
    return found


def _editor_text(editor) -> str:
    return _normalize_text(editor.evaluate(_EDITOR_TEXT_JS) or "")


def _post_button(page, dialog):
    def find():
        buttons = []
        for item in _visible_candidates(dialog, POST_BUTTON_SELECTOR, enabled=True):
            label, text = _label_and_text(item)
            if text == "post" or label == "post":
                buttons.append(item)
        return buttons[0] if len(buttons) == 1 else None

    button = _poll(page, find, _CONTROL_WAIT_MS)
    if button is None:
        raise RepostComposerError("Expected one enabled Post button in the repost composer")
    return button


def _assert_commentary(editor, commentary: str) -> None:
    if _editor_text(editor) != _normalize_text(commentary):
        raise RepostCommentaryMismatchError("Editor text differs from the approved commentary")


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def _urn_from_href(href: str) -> str | None:
    if not _is_linkedin_url(href):
        return None
    match = re.search(r"/feed/update/([^/?#]+)", unquote(urlsplit(href).path))
    return normalize_urn(match.group(1)) if match else None


def _toast_permalink(page, originals: set[str]) -> str | None:
    """A success toast linking to a post other than the original is proof."""

    def find():
        for toast in page.evaluate(_TOASTS_JS) or []:
            if toast.get("type") == "error":
                raise RepostRejectedError("LinkedIn showed an error toast after the final click")
            text = toast.get("text") or ""
            if toast.get("type") != "success" and not any(word in text for word in _SUCCESS_WORDS):
                continue
            for href in toast.get("hrefs") or []:
                urn = _urn_from_href(href)
                if urn and urn not in originals:
                    return post_url_for_urn(urn)
        return None

    return _poll(page, find, _TOAST_WAIT_MS)


def _activity_cards(page) -> list[dict]:
    cards = []
    for card in page.evaluate(_ACTIVITY_CARDS_JS, _ACTIVITY_CARD_LIMIT) or []:
        urn = normalize_urn(card.get("urn"))
        if urn:
            refs = {ref for ref in map(normalize_urn, card.get("refs") or []) if ref}
            cards.append({"urn": urn, "refs": refs})
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
            return {card["urn"] for card in _activity_cards(page)}
    except (PlaywrightError, PlaywrightTimeoutError, TimeoutError) as exc:
        logger.warning("Repost activity baseline unavailable (%s); toast proof only", type(exc).__name__)
        return None


def _activity_permalink(open_page, originals: set[str], baseline: set[str]) -> str | None:
    """Newest activity card, absent before the click, that embeds the original post."""

    with open_page(ACTIVITY_URL) as page:
        if not _on_activity_page(page):
            return None

        def find():
            for card in _activity_cards(page):
                if card["urn"] in originals or card["urn"] in baseline:
                    continue
                if card["refs"] & originals:
                    return post_url_for_urn(card["urn"])
            return None

        found = _poll(page, find, _ACTIVITY_WAIT_MS // 2)
        if found is None:
            page.reload(wait_until="domcontentloaded", timeout=_TARGET_TIMEOUT_MS)
            found = _poll(page, find, _ACTIVITY_WAIT_MS // 2)
        return found


# ---------------------------------------------------------------------------
# The repost
# ---------------------------------------------------------------------------


def perform_repost(open_page, *, post_url: str, commentary: str, on_click_attempt) -> str:
    """Repost ``post_url`` once and return the verified new permalink.

    ``open_page(url)`` returns a context manager yielding a page owned by this
    run. ``on_click_attempt`` persists the attempt and must succeed before the
    final click; if it raises, nothing is clicked.
    """
    originals = {urn_from_post_url(post_url)}
    click_attempted = False
    try:
        baseline = _activity_baseline(open_page)
        with open_page(post_url) as page:
            _assert_post_page(page)
            originals |= _page_url_urns(page)
            page.wait_for_timeout(1500)
            trigger = _repost_trigger(page)
            originals |= {urn for urn in map(normalize_urn, trigger.evaluate(_CARD_URNS_JS) or []) if urn}
            trigger.click(delay=150)
            page.wait_for_timeout(700)

            if commentary:
                _menu_option(page, "thoughts").click(delay=150)
                dialog, editor = _composer(page)
                if _editor_text(editor).strip():
                    raise RepostComposerError("Repost composer was not empty")
                human_type(
                    editor, commentary,
                    min_delay=COMMENTARY_TYPE_MIN_DELAY_MS, max_delay=COMMENTARY_TYPE_MAX_DELAY_MS,
                )
                page.wait_for_timeout(800)
                _assert_commentary(editor, commentary)
                final_control = _post_button(page, dialog)
                # Re-read right before the click: typeahead can still rewrite text.
                _assert_commentary(editor, commentary)
            else:
                final_control = _menu_option(page, "plain")

            on_click_attempt()
            click_attempted = True
            final_control.click(delay=200)
            page.wait_for_timeout(1500)
            result_url = _toast_permalink(page, originals)

        if result_url is None and baseline is not None:
            result_url = _activity_permalink(open_page, originals, baseline)
        if result_url is None:
            raise RepostUnverifiedError("Final click attempted; no new permalink was verified")
        return result_url
    except RepostUncertainError:
        raise
    except RepostSendError as exc:
        if click_attempted:
            raise RepostUncertainError(str(exc), error_type=exc.error_type) from exc
        raise
    except (PlaywrightError, PlaywrightTimeoutError, TimeoutError) as exc:
        if click_attempted:
            raise RepostUncertainError("Browser error after the final click", error_type=type(exc).__name__) from exc
        raise RepostSendError("Browser error before the final click", error_type=type(exc).__name__) from exc
