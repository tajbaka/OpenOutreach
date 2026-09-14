"""Offline DOM regression checks; no account, network or shared browser."""
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from linkedin.feed_collection import extract_posts_from_page


@pytest.fixture
def feed_page(monkeypatch):
    monkeypatch.setattr("linkedin.feed_collection.LINKEDIN_FEED_COLLECTION_ACTION_DELAY_MIN_SECONDS", 0)
    monkeypatch.setattr("linkedin.feed_collection.LINKEDIN_FEED_COLLECTION_ACTION_DELAY_MAX_SECONDS", 0)
    with sync_playwright() as pw:
        if not Path(pw.chromium.executable_path).exists():
            pytest.skip("Install Playwright Chromium for offline browser regression tests")
        browser = pw.chromium.launch(headless=True, executable_path=pw.chromium.executable_path)
        page = browser.new_page()
        page.set_content("""
            <div class="feed-shared-update-v2" id="post">
              <span class="update-components-actor__name">Alice Example</span>
              <span class="update-components-actor__sub-description">5h</span>
              <div class="update-components-text">Looking for FedRAMP support.</div>
              <button aria-label="Open control menu for post by Alice">Menu</button>
            </div>
            <script>
              window.menuClicks = 0;
              window.postUrn = 'urn:li:activity:123';
              document.querySelector('button').onclick = () => {
                window.menuClicks++;
                const menu = document.createElement('div');
                menu.setAttribute('role', 'menu');
                if (window.postUrn) {
                  const link = document.createElement('a');
                  link.href = 'https://www.linkedin.com/menu?targetUrn=' + encodeURIComponent(window.postUrn);
                  menu.append(link);
                }
                document.body.append(menu);
              };
              document.addEventListener('keydown', e => {
                if (e.key === 'Escape') document.querySelectorAll('[role=menu]').forEach(m => m.remove());
              });
            </script>
        """)
        try:
            yield page
        finally:
            browser.close()


def test_unchanged_card_reuses_url_but_recycled_card_gets_new_url(feed_page):
    first = extract_posts_from_page(feed_page)
    second = extract_posts_from_page(feed_page)
    assert first[0].activity_urn == second[0].activity_urn == "urn:li:activity:123"
    assert feed_page.evaluate("window.menuClicks") == 1

    feed_page.evaluate("""() => {
        document.querySelector('.update-components-text').textContent = 'A different FedRAMP post.';
        window.postUrn = 'urn:li:activity:456';
    }""")
    assert extract_posts_from_page(feed_page)[0].activity_urn == "urn:li:activity:456"
    assert feed_page.evaluate("window.menuClicks") == 2


def test_existing_activity_attribute_does_not_open_menu(feed_page):
    feed_page.evaluate("document.querySelector('#post').dataset.urn = 'urn:li:activity:789'")
    assert extract_posts_from_page(feed_page)[0].activity_urn == "urn:li:activity:789"
    assert feed_page.evaluate("window.menuClicks") == 0


def test_failed_menu_lookup_is_not_cached(feed_page):
    feed_page.evaluate("window.postUrn = ''")
    assert extract_posts_from_page(feed_page) == []
    feed_page.evaluate("window.postUrn = 'urn:li:activity:321'")
    assert extract_posts_from_page(feed_page)[0].activity_urn == "urn:li:activity:321"
    assert feed_page.evaluate("window.menuClicks") == 2
