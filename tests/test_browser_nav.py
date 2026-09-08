from unittest.mock import Mock, patch

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from linkedin.browser.nav import TOP_CARD_SELECTORS, find_top_card, goto_page, human_type


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


def test_goto_page_accepts_session_wait_timeout_when_url_matches():
    page = Mock()
    page.url = "https://www.linkedin.com/in/example/"
    page.wait_for_url.return_value = None
    session = Mock(page=page)
    session.wait.side_effect = PlaywrightTimeoutError("load state timed out")

    goto_page(
        session,
        action=Mock(),
        expected_url_pattern="/in/example",
        error_message="Failed to navigate",
    )

    session.wait.assert_called_once()


def test_human_type_extends_timeout_for_long_single_line_text():
    locator = Mock()
    text = "x" * 250

    with patch("linkedin.browser.nav.random.randint", return_value=250):
        human_type(locator, text)

    locator.type.assert_called_once_with(text, delay=250, timeout=77_500)


def test_human_type_keeps_minimum_timeout_for_short_single_line_text():
    locator = Mock()

    with patch("linkedin.browser.nav.random.randint", return_value=80):
        human_type(locator, "short")

    locator.type.assert_called_once_with("short", delay=80, timeout=30_000)


def test_top_card_prefers_exact_sdui_topcard_container():
    selector = '[componentkey^="com.linkedin.sdui.profile.card."][componentkey$="Topcard"]'
    assert TOP_CARD_SELECTORS[0] == selector
    page = Mock()
    card = page.locator.return_value
    card.count.return_value = 1
    card.first.is_visible.return_value = True
    assert find_top_card(Mock(page=page)) is card.first
    page.locator.assert_called_once_with(selector)
