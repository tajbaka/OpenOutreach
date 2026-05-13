# linkedin/browser/login.py
"""Daemon's Playwright login + cookie cross-check.

Mirrors `StandaloneLinkedInSession` (`linkedin/actions/standalone_session.py`)
since 2026-05-12: cookies live on disk at `data/cookies-<safe_username>.json`
keyed by LinkedIn username, not in the DB. The daemon picks which account
to log in as via `LINKEDIN_USERNAME` env (see `linkedin.conf.get_daemon_handle`).
Per-username cookie files mean two accounts never collide on disk, and
flipping which account the daemon runs as is a `.env` edit + restart.

Cross-check after restoring `storage_state`: navigate to `/feed/`,
check the URL path. If LinkedIn bounced us to `/login` / `/checkpoint`
the cookie is stale → fresh login. Same mechanism as the standalone
sessions, no special API call.

Fresh-login window is up to 10 minutes — the visible browser stays
open so the operator can complete 2FA / security checkpoints by hand.
Once `/feed/` loads, cookies are auto-saved and subsequent runs skip
the form entirely.
"""
from __future__ import annotations

import logging
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth
from termcolor import colored

from linkedin.browser.cookie_store import (
    clear_cookies,
    cookie_path_for,
    load_cookies,
    save_cookies,
)
from linkedin.browser.nav import goto_page, human_type
from linkedin.conf import (
    BROWSER_DEFAULT_TIMEOUT_MS,
    BROWSER_LOGIN_TIMEOUT_MS,
    BROWSER_SLOW_MO,
)

logger = logging.getLogger(__name__)

LINKEDIN_LOGIN_URL = "https://www.linkedin.com/login"
LINKEDIN_FEED_URL = "https://www.linkedin.com/feed/"

SELECTORS = {
    "email": 'input#username',
    "password": 'input#password',
    "submit": 'button[type="submit"]',
}

# 10-minute window for the operator to complete LinkedIn's 2FA / phone /
# security verification by hand in the visible browser. Matches the
# standalone session's timeout — daemon logins from a fresh fingerprint
# get challenged the same way standalone scripts do.
_LOGIN_WITH_2FA_TIMEOUT_MS = 10 * 60 * 1000


def playwright_login(session: "AccountSession"):
    page = session.page
    lp = session.linkedin_profile
    logger.info(colored("Fresh login sequence starting", "cyan") + f" for @{session.handle}")

    goto_page(
        session,
        action=lambda: page.goto(LINKEDIN_LOGIN_URL),
        expected_url_pattern="/login",
        error_message="Failed to load login page",
    )

    human_type(page.locator(SELECTORS["email"]), lp.linkedin_username)
    session.wait()
    human_type(page.locator(SELECTORS["password"]), lp.linkedin_password)
    session.wait()

    # Click submit, then give the operator up to 10 minutes to complete
    # whatever LinkedIn challenges with (2FA, phone verification, captcha,
    # etc.). The standalone session has the same window; daemon login is
    # equally exposed to those challenges on first run / new fingerprint.
    page.locator(SELECTORS["submit"]).click()
    logger.info(
        "Login form submitted. If LinkedIn shows 2FA / verification, complete "
        "it manually in the browser window — waiting up to 10 minutes for "
        "/feed/ …"
    )
    page.wait_for_url("**/feed/**", timeout=_LOGIN_WITH_2FA_TIMEOUT_MS)
    logger.info(colored("Feed reached — login successful", "green", attrs=["bold"]))


def launch_browser(storage_state=None):
    logger.debug("Launching Playwright")
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(headless=False, slow_mo=BROWSER_SLOW_MO)
    context = browser.new_context(storage_state=storage_state)
    context.set_default_timeout(BROWSER_DEFAULT_TIMEOUT_MS)
    Stealth().apply_stealth_sync(context)
    page = context.new_page()
    return page, context, browser, playwright


def _cookies_still_valid(session) -> bool:
    """Did LinkedIn bounce us off /feed/? If yes, the cookies are stale."""
    session.page.goto(LINKEDIN_FEED_URL)
    session.page.wait_for_load_state("load")
    path = urlparse(session.page.url).path
    return not (
        path.startswith("/uas/login")
        or path.startswith("/login")
        or path.startswith("/checkpoint")
    )


def start_browser_session(session: "AccountSession", handle: str):
    """Bring the session online: load cached cookies, validate, re-login if stale.

    Mirrors `StandaloneLinkedInSession.start()`. Cookie store is the
    per-username JSON file at `data/cookies-<safe_username>.json`;
    LinkedInProfile row is still used for credentials + rate-limit
    bookkeeping, but no longer holds `cookie_data`.
    """
    logger.debug("Configuring browser for @%s", handle)

    cookie_path = cookie_path_for(session.linkedin_profile.linkedin_username)
    storage_state = load_cookies(cookie_path)

    if storage_state:
        logger.info("Loading saved session for @%s from %s", handle, cookie_path)

    try:
        session.page, session.context, session.browser, session.playwright = launch_browser(
            storage_state=storage_state,
        )
    except Exception:
        if not storage_state:
            raise
        logger.warning("Saved browser state for @%s failed to load — falling back to fresh login", handle)
        clear_cookies(cookie_path)
        session.page, session.context, session.browser, session.playwright = launch_browser(
            storage_state=None,
        )
        storage_state = None

    if not storage_state:
        playwright_login(session)
        save_cookies(session.context.storage_state(), cookie_path)
        logger.info(colored("Login successful – cookies cached at %s", "green", attrs=["bold"]), cookie_path)
    elif not _cookies_still_valid(session):
        logger.warning(
            "Saved session expired for @%s (landed on %s) — re-authenticating",
            handle, urlparse(session.page.url).path,
        )
        clear_cookies(cookie_path)
        playwright_login(session)
        save_cookies(session.context.storage_state(), cookie_path)
        logger.info(colored("Re-login successful – cookies refreshed", "green", attrs=["bold"]))

    session.page.wait_for_load_state("load")
    logger.info(colored("Browser ready", "green", attrs=["bold"]))


if __name__ == "__main__":
    import sys

    logging.getLogger().handlers.clear()
    logging.basicConfig(
        level=logging.DEBUG,
        format='%(levelname)-8s │ %(message)s',
    )

    if len(sys.argv) != 2:
        print("Usage: python -m linkedin.browser.login <handle>")
        sys.exit(1)

    handle = sys.argv[1]

    from linkedin.browser.registry import get_or_create_session
    session = get_or_create_session(handle=handle)

    session.ensure_browser()

    start_browser_session(session=session, handle=handle)
    print("Logged in! Close browser manually.")
    session.page.pause()
