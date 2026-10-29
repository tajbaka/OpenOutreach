"""Bounded, read-only browser recovery between daemon tasks.

Never replay a handler: a transport error during a potentially mutating task
requires review. Exhaustion holds this process alive so supervisor crash
restarts cannot turn a persistent outage into another task-failure loop.
"""
from __future__ import annotations

import logging
import time

from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError

from linkedin.browser.login import LINKEDIN_FEED_URL, _is_linkedin_feed_url

logger = logging.getLogger(__name__)

_NETWORK_ERRORS = (
    "net::ERR_INTERNET_DISCONNECTED",
    "net::ERR_NETWORK_CHANGED",
    "net::ERR_CONNECTION_CLOSED",
    "net::ERR_CONNECTION_RESET",
    "net::ERR_CONNECTION_REFUSED",
    "net::ERR_CONNECTION_TIMED_OUT",
    "net::ERR_NAME_NOT_RESOLVED",
    "Page.evaluate: TypeError: Failed to fetch",
)


def is_browser_network_error(error: BaseException) -> bool:
    # Do not treat selector, parsing, database, or arbitrary JS errors as an
    # outage. In particular, a generic task timeout isn't a safe-send signal.
    return isinstance(error, PlaywrightError) and any(
        marker in str(error) for marker in _NETWORK_ERRORS
    )


def _page_is_offline(page) -> bool:
    # locator.evaluate bounds attachment as well as execution. An error page
    # may retain the requested LinkedIn page.url, so inspect its document.
    return page.locator("html").evaluate(
        """node => {
            const doc = node.ownerDocument;
            return !navigator.onLine
                || doc.documentURI.startsWith('chrome-error://')
                || Boolean(doc.querySelector('#main-frame-error .error-code'));
        }""",
        timeout=5000,
    )


def _page_is_rate_limited(page) -> bool:
    # Match Chrome's actual error surface, never profile/post text containing
    # "429". ERR_HTTP_RESPONSE_CODE_FAILURE alone doesn't identify a status.
    return any(
        " ".join(text.split()).upper() == "HTTP ERROR 429"
        for text in page.locator("#main-frame-error .error-code").all_text_contents()
    )


class BrowserRecovery:
    RETRY_DELAYS = (15, 30, 60)
    POLL_SECONDS = 15

    def __init__(self, *, sender: str, notify):
        self.sender = sender
        self.notify = notify
        self.pending = False
        self.blocked = False
        self.attempts = 0
        self.retry_at = 0.0

    def _hold(self, detail: str) -> None:
        if self.blocked:
            return
        self.blocked = True
        logger.error("LinkedIn task processing paused: %s", detail)
        self.notify(
            sender=self.sender,
            title=f"{self.sender}'s LinkedIn task processing paused",
            detail=detail,
        )

    def _schedule(self) -> None:
        self.pending = True
        if self.attempts >= len(self.RETRY_DELAYS):
            self._hold(
                "Browser recovery failed after three attempts. No further "
                "daemon tasks will be claimed. Check connectivity and the "
                "LinkedIn session, then explicitly restart the worker."
            )
            return
        delay = self.RETRY_DELAYS[self.attempts]
        self.retry_at = time.monotonic() + delay
        logger.warning("LinkedIn browser unavailable; recovery attempt %d in %ss", self.attempts + 1, delay)

    def _hold_rate_limit(self) -> None:
        self._hold(
            "LinkedIn returned HTTP 429 (rate limiting). Daemon task claims "
            "and automatic browser recovery are paused; no reload or task "
            "retry will be attempted. Allow the restriction to clear and "
            "review any failed task's send evidence before restarting the "
            "worker. LinkedIn's reset time is unknown."
        )

    def task_failed(self, error: BaseException, *, page, read_only: bool, task_id: int) -> bool:
        if (
            isinstance(error, PlaywrightError)
            and "net::ERR_HTTP_RESPONSE_CODE_FAILURE" in str(error)
            and _page_is_rate_limited(page)
        ):
            self._hold_rate_limit()
            return True
        if not is_browser_network_error(error):
            return False
        if read_only:
            self._schedule()
        else:
            self._hold(
                f"Browser transport failed during Task {task_id}. Its send "
                "outcome has not been established; the failed task was not "
                "requeued. Review its delivery evidence and reconcile any "
                "uncertain send before explicitly restarting the worker."
            )
        return True

    def task_succeeded(self) -> None:
        # A successful feed load alone must not replenish the budget if the
        # next real browser task immediately fails again.
        self.attempts = 0

    def can_claim(self, page) -> bool:
        if self.blocked:
            return False
        if not self.pending:
            try:
                if not _page_is_offline(page):
                    return True
                if _page_is_rate_limited(page):
                    self._hold_rate_limit()
                    return False
            except PlaywrightError as exc:
                if not isinstance(exc, PlaywrightTimeoutError) and not is_browser_network_error(exc):
                    raise
            self._schedule()
            return False

        if time.monotonic() < self.retry_at:
            return False
        self.attempts += 1
        try:
            # Only this daemon's working page is navigated. No shared context,
            # listener tab, cookies, session identity, or send API is touched.
            response = page.goto(LINKEDIN_FEED_URL, wait_until="domcontentloaded", timeout=30000)
            offline = _page_is_offline(page)
        except PlaywrightError as exc:
            if "net::ERR_HTTP_RESPONSE_CODE_FAILURE" in str(exc) and _page_is_rate_limited(page):
                self._hold_rate_limit()
                return False
            if not isinstance(exc, PlaywrightTimeoutError) and not is_browser_network_error(exc):
                raise
            self._schedule()
            return False
        if (response is not None and response.status == 429) or (offline and _page_is_rate_limited(page)):
            self._hold_rate_limit()
            return False
        if offline or response is None or not response.ok:
            self._schedule()
            return False
        if not _is_linkedin_feed_url(page.url):
            self._hold(
                "Browser recovery did not reach the HTTPS LinkedIn feed. "
                "Check login/checkpoint state before explicitly restarting "
                "the worker. No further daemon tasks will be claimed."
            )
            return False
        self.pending = False
        logger.info("LinkedIn browser recovered; task processing can resume")
        return True
