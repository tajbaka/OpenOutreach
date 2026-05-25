# linkedin/browser/login.py
"""Daemon's Playwright login + session management (persistent context).

The daemon uses `launch_persistent_context` so the realtime listener child
process can share the browser over CDP. The persistent context self-persists
cookies/localStorage to `data/profile-<safe_username>/` — no JSON jar load
or save step. The daemon picks which account to log in as via `LINKEDIN_USERNAME`
env (see `linkedin.conf.get_daemon_handle`).

`StandaloneLinkedInSession` (`linkedin/actions/standalone_session.py`) is NOT
touched here — it keeps its own `launch()` + JSON cookie flow.

Fresh-login window is up to 10 minutes — the visible browser stays open so
the operator can complete 2FA / security checkpoints by hand.
"""
from __future__ import annotations

import logging
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth
from termcolor import colored

from linkedin.browser.nav import goto_page, human_type
from linkedin.conf import (
    BROWSER_DEFAULT_TIMEOUT_MS,
    BROWSER_SLOW_MO,
    ENABLE_REALTIME_LISTENER,
    LISTENER_CDP_PORT,
)

logger = logging.getLogger(__name__)

LINKEDIN_LOGIN_URL = "https://www.linkedin.com/login"
LINKEDIN_FEED_URL = "https://www.linkedin.com/feed/"

SELECTORS = {
    "email": 'input#username',
    "password": 'input#password',
    "submit": 'button[type="submit"]',
}

LOGIN_EMAIL_SELECTORS = [
    'input#username',
    'input[name="session_key"]',
    'input[autocomplete*="username"]',
    'input[type="email"]',
    'input[aria-label*="Email" i]',
    'input[placeholder*="Email" i]',
]

LOGIN_PASSWORD_SELECTORS = [
    'input#password',
    'input[name="session_password"]',
    'input[autocomplete="current-password"]',
    'input[type="password"]',
]

LOGIN_SUBMIT_SELECTORS = [
    'button[type="submit"]',
    'button:has-text("Sign in")',
]

# 10-minute window for the operator to complete LinkedIn's 2FA / phone /
# security verification by hand in the visible browser. Matches the
# standalone session's timeout — daemon logins from a fresh fingerprint
# get challenged the same way standalone scripts do.
_LOGIN_WITH_2FA_TIMEOUT_MS = 10 * 60 * 1000


def _first_visible(page, selectors: list[str]):
    for selector in selectors:
        locator = page.locator(selector)
        for i in range(locator.count()):
            candidate = locator.nth(i)
            if candidate.is_visible():
                return candidate
    return None


def _require_visible(page, selectors: list[str], label: str):
    locator = _first_visible(page, selectors)
    if locator is None:
        raise RuntimeError(f"LinkedIn login {label} field not found on {page.url}")
    return locator


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

    email = _require_visible(page, LOGIN_EMAIL_SELECTORS, "email")
    password = _require_visible(page, LOGIN_PASSWORD_SELECTORS, "password")
    submit = _require_visible(page, LOGIN_SUBMIT_SELECTORS, "submit")

    human_type(email, lp.linkedin_username)
    session.wait()
    human_type(password, lp.linkedin_password)
    session.wait()

    # Click submit, then give the operator up to 10 minutes to complete
    # whatever LinkedIn challenges with (2FA, phone verification, captcha,
    # etc.). The standalone session has the same window; daemon login is
    # equally exposed to those challenges on first run / new fingerprint.
    submit.click()
    logger.info(
        "Login form submitted. If LinkedIn shows 2FA / verification, complete "
        "it manually in the browser window — waiting up to 10 minutes for "
        "/feed/ …"
    )
    page.wait_for_url("**/feed/**", timeout=_LOGIN_WITH_2FA_TIMEOUT_MS)
    logger.info(colored("Feed reached — login successful", "green", attrs=["bold"]))


def launch_browser(profile_dir, *, cdp_port=None, seed_cookies=None):
    """Launch Chromium as a persistent context rooted at `profile_dir`.

    A persistent context is the browser's *default* context — required so
    the realtime listener child process can see it via `connect_over_cdp`
    (a plain `new_context()` context is invisible to CDP-connecting peers).
    Cookies/localStorage persist in `profile_dir` automatically; there is no
    JSON storage_state to load or save.

    `cdp_port` (when set) exposes `--remote-debugging-port` so the listener
    can attach. `seed_cookies` (a Playwright cookies list) is added once on
    a first run to migrate an account off the legacy JSON cookie jar without
    forcing a re-login.

    Returns `(page, context, browser, playwright)`. NOTE: `browser` is
    `None` for a persistent context — closing the context closes the
    browser. Callers must tolerate a `None` browser.
    """
    playwright = sync_playwright().start()
    args = []
    if cdp_port:
        args.append(f"--remote-debugging-port={cdp_port}")
    context = playwright.chromium.launch_persistent_context(
        str(profile_dir),
        headless=False,
        slow_mo=BROWSER_SLOW_MO,
        args=args,
    )
    context.set_default_timeout(BROWSER_DEFAULT_TIMEOUT_MS)
    Stealth().apply_stealth_sync(context)
    if seed_cookies:
        try:
            context.add_cookies(seed_cookies)
            logger.info("Seeded %d cookies into new persistent profile", len(seed_cookies))
        except Exception as e:
            logger.warning("Cookie seeding failed (will fall back to login): %s", e)
    page = context.pages[0] if context.pages else context.new_page()
    return page, context, context.browser, playwright


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
    """Bring the session online with a persistent-context Chromium.

    Profile lives at `data/profile-<safe_username>/`. On the first run
    (profile dir absent) the legacy `data/cookies-<safe_username>.json` jar,
    if present, seeds the new context so the cutover skips a re-login.
    After launch, `/feed/` is checked; a bounce to login/checkpoint means
    the session is stale → interactive `playwright_login` (the persistent
    context records the result itself — no save step).
    """
    from linkedin.browser.cookie_store import cookie_path_for, load_cookies, profile_dir_for

    logger.debug("Configuring persistent-context browser for @%s", handle)

    linkedin_username = session.linkedin_profile.linkedin_username
    profile_dir = profile_dir_for(linkedin_username)
    cdp_port = LISTENER_CDP_PORT if ENABLE_REALTIME_LISTENER else None

    seed_cookies = None
    if not profile_dir.exists():
        legacy = load_cookies(cookie_path_for(linkedin_username))
        if legacy and legacy.get("cookies"):
            seed_cookies = legacy["cookies"]
            logger.info("First persistent-context run for @%s — seeding from legacy cookie jar", handle)

    try:
        session.page, session.context, session.browser, session.playwright = launch_browser(
            profile_dir, cdp_port=cdp_port, seed_cookies=seed_cookies,
        )
    except Exception:
        logger.error(
            "Failed to launch persistent-context browser for @%s at %s. "
            "If the profile is corrupt, remove that directory and restart; "
            "if it is locked, another Chromium is still using it.",
            handle, profile_dir,
        )
        raise

    if not _cookies_still_valid(session):
        logger.warning("Session for @%s not valid (landed on %s) — authenticating",
                        handle, urlparse(session.page.url).path)
        playwright_login(session)
        logger.info(colored("Login successful — persistent profile at %s", "green", attrs=["bold"]), profile_dir)

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

    print("Logged in! Close browser manually.")
    session.page.pause()
