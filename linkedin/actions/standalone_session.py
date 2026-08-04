"""Standalone authenticated session for env-var-driven LinkedIn scrapers.

Decoupled from the daemon's `LinkedInProfile` / `AccountSession` machinery —
credentials come from env vars only and cookies live in a JSON file rather
than the DB. This lets a scraper account be different from (and operationally
independent of) the outreach account that runs `make run`.

Default env vars (Sales Nav, the original consumer):
    SALES_NAV_LINKEDIN_USERNAME
    SALES_NAV_LINKEDIN_PASSWORD

Cookies are cached at `data/cookies-<safe_username>.json`, keyed by the
LinkedIn username itself rather than by the consumer slot. This means
swapping accounts inside the same env-var slot (e.g. changing
BACKFILL_LINKEDIN_USERNAME from Chuka to Arian) no longer collides with the
previous account's cached session — each username keeps its own cookie file
and can be flipped in and out of any slot without re-authenticating.

Past bug (2026-05-11): cookies used to be keyed by slot label
(`backfill_cookies.json`, `sales_nav_cookies.json`). Swapping the username
inside a slot silently kept the old account's cookies; the validity check
passes (the feed loads as the old account) but the session is running under
the wrong identity. Per-username filenames fix that — a mismatched username
just means no cached file exists, so the session forces a fresh login.
"""
from __future__ import annotations

import logging
import os
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

from linkedin.browser.cookie_store import (
    clear_cookies,
    cookie_path_for,
    load_cookies,
    profile_dir_for,
    save_cookies,
)
from linkedin.browser.login import (
    LINKEDIN_FEED_URL,
    LINKEDIN_LOGIN_URL,
    LOGIN_EMAIL_SELECTORS,
    LOGIN_PASSWORD_SELECTORS,
    LOGIN_SUBMIT_SELECTORS,
    _require_visible,
    launch_browser,
)
from linkedin.browser.nav import human_type
from linkedin.conf import (
    BROWSER_DEFAULT_TIMEOUT_MS,
    BROWSER_LOGIN_TIMEOUT_MS,
    BROWSER_SLOW_MO,
)
from linkedin.exceptions import AuthenticationError

logger = logging.getLogger(__name__)

# Default env vars — Sales Nav was the original consumer. Other consumers
# pass their own env-var names to the constructor; the cookie path is
# always derived from the resolved username (see
# `linkedin.browser.cookie_store.cookie_path_for`).
ENV_USERNAME = "SALES_NAV_LINKEDIN_USERNAME"
ENV_PASSWORD = "SALES_NAV_LINKEDIN_PASSWORD"


class StandaloneLinkedInSession:
    """Generic env-var-driven LinkedIn session, decoupled from the daemon.

    Used by one-off scrapers that need to log in as an account distinct from
    the daemon's `LinkedInProfile` rows — current consumers:
      - `manage.py export_sales_list` (Sales Nav → CSV)
      - `manage.py import_connections` (Connections page → DB backfill)

    Each consumer passes its own env-var names + cookie path so multiple
    independent scrapers can run with different credentials and cached
    sessions. Exposes `.page` and `.context` (used by `PlaywrightLinkedinAPI`)
    and a `.wait()` method matching `AccountSession`'s contract so reuse from
    `linkedin.browser.nav` keeps working.
    """

    def __init__(
        self,
        *,
        env_username: str = ENV_USERNAME,
        env_password: str = ENV_PASSWORD,
        label: str = "Sales Nav",
        use_persistent_profile: bool = False,
    ):
        self._env_username_name = env_username
        self._env_password_name = env_password
        self._label = label
        self._use_persistent_profile = use_persistent_profile

        self.username = os.getenv(env_username, "").strip()
        self.password = os.getenv(env_password, "")
        if not self.username or not self.password:
            raise AuthenticationError(
                f"{label} credentials missing — set {env_username} "
                f"and {env_password} in .env"
            )
        # Cookie path derived from username so any account swap inside the
        # same env slot is conflict-free — see module docstring.
        self._cookie_path = cookie_path_for(self.username)
        self.page = None
        self.context = None
        self.browser = None
        self.playwright = None

    def start(self) -> None:
        if self._use_persistent_profile:
            self._launch_persistent()
            if not self._cookies_still_valid():
                logger.warning(
                    "%s: persistent session expired - re-authenticating",
                    self._label,
                )
                self._login()
            logger.info(
                "%s: reused persistent profile for %s",
                self._label,
                self.username,
            )
            return

        storage_state = load_cookies(self._cookie_path)
        self._launch(storage_state)

        if storage_state:
            if self._cookies_still_valid():
                logger.info("%s: reused cached session for %s", self._label, self.username)
                return
            logger.warning("%s: cached cookies expired — re-authenticating", self._label)
            clear_cookies(self._cookie_path)
            self.close()
            self._launch(storage_state=None)

        self._login()
        save_cookies(self.context.storage_state(), self._cookie_path)
        logger.info("%s: login successful, cookies cached at %s", self._label, self._cookie_path)

    def ensure_browser(self) -> None:
        """Compat shim for code paths that expect AccountSession's interface."""
        if self.page is None:
            self.start()

    @property
    def linkedin_profile(self):
        """Stub for compat with daemon helpers (e.g. `_our_display_name`).

        Exposes `linkedin_username` so persisted Messages can label outbound
        threads correctly. No DB row is created — this is purely a duck-type
        match for `LinkedInProfile`.
        """
        username = self.username
        class _LinkedInProfileStub:
            linkedin_username = username
            first_name = ""
            last_name = ""
        return _LinkedInProfileStub()

    def wait(self) -> None:
        """Match AccountSession.wait() contract — used by reused nav helpers."""
        from linkedin.browser.session import random_sleep
        from linkedin.conf import MAX_DELAY, MIN_DELAY

        random_sleep(MIN_DELAY, MAX_DELAY)
        self.page.wait_for_load_state("load")

    def close(self) -> None:
        if self.context:
            try:
                self.context.close()
            except Exception:
                pass
        if self.browser:
            try:
                self.browser.close()
            except Exception:
                pass
        if self.playwright:
            try:
                self.playwright.stop()
            except Exception:
                pass
        self.page = self.context = self.browser = self.playwright = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    # -- internals -------------------------------------------------------

    def _launch(self, storage_state: dict | None) -> None:
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(
            headless=False, slow_mo=BROWSER_SLOW_MO,
        )
        self.context = self.browser.new_context(storage_state=storage_state)
        self.context.set_default_timeout(BROWSER_DEFAULT_TIMEOUT_MS)
        Stealth().apply_stealth_sync(self.context)
        self.page = self.context.new_page()

    def _launch_persistent(self) -> None:
        profile_dir = profile_dir_for(self.username)
        self.page, self.context, self.browser, self.playwright = launch_browser(
            profile_dir,
        )

    def _cookies_still_valid(self) -> bool:
        self.page.goto(LINKEDIN_FEED_URL)
        self.page.wait_for_load_state("load")
        path = urlparse(self.page.url).path
        return not (
            path.startswith("/uas/login")
            or path.startswith("/login")
            or path.startswith("/checkpoint")
        )

    # Long timeout to allow human-handled 2FA / verification challenges in the
    # visible browser. Standalone sessions (non-daemon) often log in from a
    # fresh fingerprint, which LinkedIn challenges. Once /feed/ is reached
    # cookies are auto-saved and subsequent runs skip login entirely.
    _LOGIN_WITH_2FA_TIMEOUT_MS = 10 * 60 * 1000  # 10 minutes

    def _login(self) -> None:
        logger.info("%s: logging in as %s", self._label, self.username)
        self.page.goto(LINKEDIN_LOGIN_URL)
        self.page.wait_for_load_state("load")

        email = _require_visible(self.page, LOGIN_EMAIL_SELECTORS, "email")
        password = _require_visible(self.page, LOGIN_PASSWORD_SELECTORS, "password")
        submit = _require_visible(self.page, LOGIN_SUBMIT_SELECTORS, "submit")

        human_type(email, self.username)
        human_type(password, self.password)
        submit.click()

        logger.info(
            "%s: form submitted. If LinkedIn shows 2FA / verification, "
            "complete it manually in the browser window. Waiting up to "
            "10 minutes for the LinkedIn feed to load …",
            self._label,
        )
        self.page.wait_for_url("**/feed/**", timeout=self._LOGIN_WITH_2FA_TIMEOUT_MS)
        logger.info("%s: feed reached — login successful, cookies will be cached.", self._label)
