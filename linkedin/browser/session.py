# linkedin/browser/session.py
from __future__ import annotations

import logging
import random
import time

from linkedin.conf import MIN_DELAY, MAX_DELAY

logger = logging.getLogger(__name__)


def random_sleep(min_val, max_val):
    delay = random.uniform(min_val, max_val)
    logger.debug(f"Pause: {delay:.2f}s")
    time.sleep(delay)


class AccountSession:
    def __init__(self, handle: str):
        from linkedin.models import LinkedInProfile

        self.handle = handle.strip().lower()

        self.linkedin_profile = LinkedInProfile.objects.select_related(
            "user",
        ).get(user__username=self.handle)
        self.django_user = self.linkedin_profile.user

        # Active campaign — set by the daemon before each lane execution
        self.campaign = None

        # Playwright objects – created on first access or after crash
        self.page = None
        self.context = None
        self.browser = None
        self.playwright = None

    @property
    def campaigns(self):
        """All campaigns this user belongs to."""
        from linkedin.models import Campaign
        from linkedin.conf import ENABLE_FREEMIUM_CAMPAIGN

        campaigns = Campaign.objects.filter(user=self.django_user)
        if not ENABLE_FREEMIUM_CAMPAIGN:
            campaigns = campaigns.filter(is_freemium=False)
        return campaigns

    def ensure_browser(self):
        """Launch or recover the persistent-context browser. Call before using .page.

        A persistent context self-maintains its session on disk, so there is
        no cookie-refresh step — a live page needs nothing; a closed/absent
        page triggers a relaunch (which re-opens the same profile dir).
        """
        from linkedin.browser.login import start_browser_session

        if not self.page or self.page.is_closed():
            logger.debug("Launching/recovering persistent-context browser for %s", self.handle)
            start_browser_session(session=self, handle=self.handle)

    def wait(self, min_delay=MIN_DELAY, max_delay=MAX_DELAY):
        random_sleep(min_delay, max_delay)
        self.page.wait_for_load_state("load")

    def close(self):
        if self.context:
            try:
                self.context.close()
                if self.browser:
                    self.browser.close()
                if self.playwright:
                    self.playwright.stop()
                logger.info("Browser closed gracefully (%s)", self.handle)
            except Exception as e:
                logger.debug("Error closing browser: %s", e)
            finally:
                self.page = self.context = self.browser = self.playwright = None

        logger.info("Account session closed → %s", self.handle)

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def __repr__(self) -> str:
        return f"<AccountSession {self.handle}>"
