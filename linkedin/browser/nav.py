# linkedin/browser/nav.py
import logging
import random
from collections.abc import Callable
from urllib.parse import unquote

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from linkedin.conf import BROWSER_NAV_TIMEOUT_MS, FIXTURE_PAGES_DIR, HUMAN_TYPE_MIN_DELAY_MS, HUMAN_TYPE_MAX_DELAY_MS
from linkedin.exceptions import SkipProfile

logger = logging.getLogger(__name__)


def goto_page(session: "AccountSession",
              action,
              expected_url_pattern: str,
              timeout: int = BROWSER_NAV_TIMEOUT_MS,
              error_message: str = "",
              url_ok: Callable[[str], bool] | None = None,
              ):
    page = session.page
    try:
        action()
    except PlaywrightTimeoutError:
        logger.warning("Navigation action timed out; checking current URL anyway")
    if not page:
        return

    try:
        page.wait_for_url(lambda url: expected_url_pattern in unquote(url), timeout=timeout)
    except PlaywrightTimeoutError:
        pass  # we still continue and check URL below

    try:
        session.wait()
    except PlaywrightTimeoutError:
        logger.warning("Navigation load wait timed out; checking current URL anyway")

    current = unquote(page.url)
    if expected_url_pattern not in current:
        if "/404" in current:
            raise SkipProfile(f"Profile returned 404 → {current}")
        if not (url_ok and url_ok(current)):
            raise RuntimeError(f"{error_message} → expected '{expected_url_pattern}' | got '{current}'")

    logger.debug("Navigated to %s", page.url)


def find_first_visible(page, selectors: list[str]):
    """Try selectors in order, return first locator that is actually visible."""
    for selector in selectors:
        locator = page.locator(selector)
        if locator.count() > 0 and locator.first.is_visible():
            return locator.first
    return None


TOP_CARD_SELECTORS = [
    'section:has(div.top-card-background-hero-image)',
    'section[data-member-id]',
    'section.artdeco-card:has(> div.pv-top-card)',
    'section:has(> div[class*="pv-top-card"])',
    'section[componentkey*="com.linkedin.sdui.profile.card"]',
]


def find_top_card(session):
    top_card = find_first_visible(session.page, TOP_CARD_SELECTORS)
    if top_card is None:
        logger.warning("Top card not found on %s", session.page.url)
        raise SkipProfile("Top Card section not found")
    return top_card


def human_type(locator, text: str, min_delay: int = HUMAN_TYPE_MIN_DELAY_MS, max_delay: int = HUMAN_TYPE_MAX_DELAY_MS):
    """Type text with randomized per-keystroke delay to mimic human input.

    Multi-line safe: `\\n` characters are dispatched as `Shift+Enter` (a
    line break inside contenteditable / form inputs) rather than as a
    plain Enter keystroke (which LinkedIn's compose box interprets as
    Send-submit). Tommy Fauth incident, 2026-05-12: a multi-paragraph
    template sent 3 partial messages before the body because `\\n` in
    the text was being dispatched as raw Enter on each line break.
    """
    if "\n" not in text:
        # Fast path: no line breaks → straight per-key type, preserving the
        # exact randomized cadence the bot-detection avoidance relies on.
        delay = random.randint(min_delay, max_delay)
        timeout_ms = max(30_000, len(text) * delay + 15_000)
        locator.type(text, delay=delay, timeout=timeout_ms)
        return

    # Multi-line: focus the locator once, then alternate typing line text
    # with Shift+Enter for line breaks. `keyboard.press("Shift+Enter")`
    # dispatches a single modifier-held keystroke — same shape a human
    # types — without submitting the form.
    locator.click()
    lines = text.split("\n")
    page = locator.page
    for i, line in enumerate(lines):
        if line:
            page.keyboard.type(line, delay=random.randint(min_delay, max_delay))
        if i < len(lines) - 1:
            page.keyboard.press("Shift+Enter")


def dump_page_html(session: "AccountSession", profile: dict, ):
    filepath = FIXTURE_PAGES_DIR / f"{profile.get('public_identifier')}.html"
    html_content = session.page.content()
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html_content)
    logger.info("Saved ambiguous connection status page → %s", filepath)
