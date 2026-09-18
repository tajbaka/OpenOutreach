from unittest.mock import Mock

import pytest
from playwright.sync_api import Error, TimeoutError

from linkedin.browser.login import (
    _is_linkedin_feed_url,
    _submit_login_and_wait_for_feed,
)


@pytest.mark.parametrize("url,expected", [
    ("https://www.linkedin.com/feed/", True),
    ("https://linkedin.com/feed?x=1", True),
    ("https://www.linkedin.com/feed/update/urn:li:activity:1", True),
    ("https://www.linkedin.com/login?next=/feed/", False),
    ("https://www.linkedin.com/checkpoint/", False),
    ("https://www.linkedin.com/flagship-web/login", False),
    ("https://www.linkedin.com/feedfake", False),
    ("https://example.com/feed/", False),
    ("http://www.linkedin.com/feed/", False),
])
def test_feed_url_requires_linkedin_feed(url, expected):
    assert _is_linkedin_feed_url(url) is expected


def test_manual_login_finishes_while_submit_times_out():
    page = Mock(url="https://www.linkedin.com/login")
    submit = Mock()

    def complete_login():
        page.url = "https://www.linkedin.com/feed/"
        raise TimeoutError("Old submit locator disappeared")

    submit.click.side_effect = complete_login
    _submit_login_and_wait_for_feed(page, submit)
    page.wait_for_url.assert_called_once_with(
        _is_linkedin_feed_url, timeout=600000, wait_until="domcontentloaded",
    )


def test_already_on_feed_does_not_click_stale_submit():
    page = Mock(url="https://www.linkedin.com/feed/")
    submit = Mock()
    _submit_login_and_wait_for_feed(page, submit)
    submit.click.assert_not_called()
    page.wait_for_url.assert_called_once()


def test_normal_submit_keeps_manual_verification_wait():
    page = Mock(url="https://www.linkedin.com/login")
    submit = Mock()
    _submit_login_and_wait_for_feed(page, submit)
    submit.click.assert_called_once_with()
    page.wait_for_url.assert_called_once()


@pytest.mark.parametrize("url", [
    "https://www.linkedin.com/login",
    "https://www.linkedin.com/checkpoint/",
    "https://example.com/feed/",
])
def test_timeout_without_feed_is_not_hidden(url):
    page = Mock(url=url)
    submit = Mock()
    failure = TimeoutError("Submit timed out")
    submit.click.side_effect = failure
    with pytest.raises(TimeoutError) as caught:
        _submit_login_and_wait_for_feed(page, submit)
    assert caught.value is failure
    page.wait_for_url.assert_not_called()


def test_other_browser_errors_are_not_hidden():
    page = Mock(url="https://www.linkedin.com/login")
    submit = Mock()
    submit.click.side_effect = Error("Browser closed")
    with pytest.raises(Error, match="Browser closed"):
        _submit_login_and_wait_for_feed(page, submit)


def test_feed_wait_failure_still_propagates():
    page = Mock(url="https://www.linkedin.com/login")
    page.wait_for_url.side_effect = TimeoutError("Verification unfinished")
    with pytest.raises(TimeoutError, match="Verification unfinished"):
        _submit_login_and_wait_for_feed(page, Mock())
