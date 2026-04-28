"""Standalone authenticated session for env-var-driven LinkedIn scrapers.

Decoupled from the daemon's `LinkedInProfile` / `AccountSession` machinery —
credentials come from env vars only and cookies live in a JSON file rather
than the DB. This lets a scraper account be different from (and operationally
independent of) the outreach account that runs `make run`.

Default env vars (Sales Nav, the original consumer):
    SALES_NAV_LINKEDIN_USERNAME
    SALES_NAV_LINKEDIN_PASSWORD
Default cookie cache: `data/sales_nav_cookies.json`.

Other consumers (e.g., manage.py import_connections) pass their own env-var
names and cookie filename to the constructor. Delete the cookie file to
force a fresh login.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

from linkedin.browser.login import LINKEDIN_FEED_URL, LINKEDIN_LOGIN_URL, SELECTORS
from linkedin.browser.nav import human_type
from linkedin.conf import (
    BROWSER_DEFAULT_TIMEOUT_MS,
    BROWSER_LOGIN_TIMEOUT_MS,
    BROWSER_SLOW_MO,
    ROOT_DIR,
)
from linkedin.exceptions import AuthenticationError

logger = logging.getLogger(__name__)

# Defaults — Sales Nav was the original consumer. Other consumers pass their
# own env-var names + cookie path to the constructor.
COOKIE_PATH = ROOT_DIR / "data" / "sales_nav_cookies.json"

ENV_USERNAME = "SALES_NAV_LINKEDIN_USERNAME"
ENV_PASSWORD = "SALES_NAV_LINKEDIN_PASSWORD"


def _load_cookies(path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Failed to read cached cookies at %s: %s", path, e)
        return None


def _save_cookies(state: dict, path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state), encoding="utf-8")


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
        cookie_path=COOKIE_PATH,
        label: str = "Sales Nav",
    ):
        self._env_username_name = env_username
        self._env_password_name = env_password
        self._cookie_path = cookie_path
        self._label = label

        self.username = os.getenv(env_username, "").strip()
        self.password = os.getenv(env_password, "")
        if not self.username or not self.password:
            raise AuthenticationError(
                f"{label} credentials missing — set {env_username} "
                f"and {env_password} in .env"
            )
        self.page = None
        self.context = None
        self.browser = None
        self.playwright = None

    def start(self) -> None:
        storage_state = _load_cookies(self._cookie_path)
        self._launch(storage_state)

        if storage_state:
            if self._cookies_still_valid():
                logger.info("%s: reused cached session for %s", self._label, self.username)
                return
            logger.warning("%s: cached cookies expired — re-authenticating", self._label)
            try:
                self._cookie_path.unlink()
            except FileNotFoundError:
                pass
            self.close()
            self._launch(storage_state=None)

        self._login()
        _save_cookies(self.context.storage_state(), self._cookie_path)
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

        human_type(self.page.locator(SELECTORS["email"]), self.username)
        human_type(self.page.locator(SELECTORS["password"]), self.password)
        self.page.locator(SELECTORS["submit"]).click()

        logger.info(
            "%s: form submitted. If LinkedIn shows 2FA / verification, "
            "complete it manually in the browser window. Waiting up to "
            "10 minutes for the LinkedIn feed to load …",
            self._label,
        )
        self.page.wait_for_url("**/feed/**", timeout=self._LOGIN_WITH_2FA_TIMEOUT_MS)
        logger.info("%s: feed reached — login successful, cookies will be cached.", self._label)
