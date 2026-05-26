from unittest.mock import Mock

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from linkedin.browser.nav import goto_page


def test_goto_page_accepts_action_timeout_when_url_matches():
    page = Mock()
    page.url = "https://www.linkedin.com/in/example/"
    page.wait_for_url.return_value = None
    session = Mock(page=page)
    session.wait.return_value = None

    goto_page(
        session,
        action=Mock(side_effect=PlaywrightTimeoutError("Page.goto timed out")),
        expected_url_pattern="/in/example",
        error_message="Failed to navigate",
    )

    session.wait.assert_called_once()
