"""Standalone authenticated session for the Sales Nav scraper.

Decoupled from the daemon's `LinkedInProfile` / `AccountSession` machinery —
credentials come from env vars only and cookies live in a JSON file rather
than the DB. This lets the Sales Nav account be different from (and
operationally independent of) the outreach account that runs `make run`.

Required env vars:
    SALES_NAV_LINKEDIN_USERNAME
    SALES_NAV_LINKEDIN_PASSWORD

Cookies are cached at `data/sales_nav_cookies.json` so subsequent runs reuse
the session. Delete the file to force a fresh login.
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

COOKIE_PATH = ROOT_DIR / "data" / "sales_nav_cookies.json"

ENV_USERNAME = "SALES_NAV_LINKEDIN_USERNAME"
ENV_PASSWORD = "SALES_NAV_LINKEDIN_PASSWORD"


def _load_cookies() -> dict | None:
    if not COOKIE_PATH.exists():
        return None
    try:
        return json.loads(COOKIE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Failed to read cached Sales Nav cookies: %s", e)
        return None


def _save_cookies(state: dict) -> None:
    COOKIE_PATH.parent.mkdir(parents=True, exist_ok=True)
    COOKIE_PATH.write_text(json.dumps(state), encoding="utf-8")


class SalesNavSession:
    """Minimal session object compatible with `PlaywrightLinkedinAPI`.

    Exposes `.page` and `.context` (the only attributes the API client
    reads) plus a `.wait()` method matching `AccountSession`'s contract
    so anything reused from `linkedin.browser.nav` keeps working.
    """

    def __init__(self):
        self.username = os.getenv(ENV_USERNAME, "").strip()
        self.password = os.getenv(ENV_PASSWORD, "")
        if not self.username or not self.password:
            raise AuthenticationError(
                f"Sales Nav credentials missing — set {ENV_USERNAME} "
                f"and {ENV_PASSWORD} in .env"
            )
        self.page = None
        self.context = None
        self.browser = None
        self.playwright = None

    def start(self) -> None:
        storage_state = _load_cookies()
        self._launch(storage_state)

        if storage_state:
            if self._cookies_still_valid():
                logger.info("Sales Nav: reused cached session for %s", self.username)
                return
            logger.warning("Sales Nav: cached cookies expired — re-authenticating")
            try:
                COOKIE_PATH.unlink()
            except FileNotFoundError:
                pass
            self.close()
            self._launch(storage_state=None)

        self._login()
        _save_cookies(self.context.storage_state())
        logger.info("Sales Nav: login successful, cookies cached at %s", COOKIE_PATH)

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

    def _login(self) -> None:
        logger.info("Sales Nav: logging in as %s", self.username)
        self.page.goto(LINKEDIN_LOGIN_URL)
        self.page.wait_for_load_state("load")

        human_type(self.page.locator(SELECTORS["email"]), self.username)
        human_type(self.page.locator(SELECTORS["password"]), self.password)
        self.page.locator(SELECTORS["submit"]).click()

        self.page.wait_for_url("**/feed/**", timeout=BROWSER_LOGIN_TIMEOUT_MS)
