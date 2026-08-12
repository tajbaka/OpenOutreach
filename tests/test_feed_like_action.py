from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from linkedin.actions.feed_like import (
    FeedLikeResult,
    FeedLikeSendError,
    FeedLikeUncertainError,
    REACTION_BUTTON_SELECTOR,
    _reaction_state,
    _wait_for_unique_reaction_button,
    ensure_feed_post_liked,
)


def _session(page):
    return SimpleNamespace(page=page, context=None)


def test_feed_like_clicks_only_from_no_reaction_and_verifies_like():
    page = MagicMock()
    button = MagicMock()
    button.get_attribute.return_value = "Reaction button state: no reaction"

    with (
        patch(
            "linkedin.actions.feed_like._wait_for_unique_reaction_button",
            return_value=button,
        ),
        patch(
            "linkedin.actions.feed_like._wait_for_reaction_state",
            return_value=True,
        ),
    ):
        result = ensure_feed_post_liked(
            _session(page),
            post_url="https://www.linkedin.com/feed/update/urn:li:activity:91/",
        )

    assert result == FeedLikeResult.LIKED
    button.click.assert_called_once_with(delay=150)


def test_feed_like_does_not_toggle_existing_like():
    page = MagicMock()
    button = MagicMock()
    button.get_attribute.return_value = "Reaction button state: Like"

    with patch(
        "linkedin.actions.feed_like._wait_for_unique_reaction_button",
        return_value=button,
    ):
        result = ensure_feed_post_liked(
            _session(page),
            post_url="https://www.linkedin.com/feed/update/urn:li:activity:91/",
        )

    assert result == FeedLikeResult.ALREADY_LIKED
    button.click.assert_not_called()


def test_feed_like_preserves_another_reaction():
    page = MagicMock()
    button = MagicMock()
    button.get_attribute.return_value = "Reaction button state: Celebrate"

    with patch(
        "linkedin.actions.feed_like._wait_for_unique_reaction_button",
        return_value=button,
    ):
        result = ensure_feed_post_liked(
            _session(page),
            post_url="https://www.linkedin.com/feed/update/urn:li:activity:91/",
        )

    assert result == FeedLikeResult.PRESERVED_REACTION
    button.click.assert_not_called()


def test_feed_like_marks_unverified_click_uncertain():
    page = MagicMock()
    button = MagicMock()
    button.get_attribute.return_value = "Reaction button state: no reaction"

    with (
        patch(
            "linkedin.actions.feed_like._wait_for_unique_reaction_button",
            return_value=button,
        ),
        patch(
            "linkedin.actions.feed_like._wait_for_reaction_state",
            return_value=False,
        ),
    ):
        with pytest.raises(FeedLikeUncertainError, match="could not verify"):
            ensure_feed_post_liked(
                _session(page),
                post_url="https://www.linkedin.com/feed/update/urn:li:activity:91/",
            )


def test_feed_like_requires_exactly_one_visible_reaction_button():
    page = MagicMock()
    page.locator.return_value.count.return_value = 0

    with pytest.raises(FeedLikeSendError, match="found 0"):
        ensure_feed_post_liked(
            _session(page),
            post_url="https://www.linkedin.com/feed/update/urn:li:activity:91/",
        )

    page.locator.assert_called_with(REACTION_BUTTON_SELECTOR)


def test_feed_like_waits_for_delayed_reaction_controls():
    page = MagicMock()
    button = MagicMock()

    with patch(
        "linkedin.actions.feed_like._visible_reaction_buttons",
        side_effect=[[], [], [button]],
    ):
        found = _wait_for_unique_reaction_button(page, timeout_ms=500)

    assert found is button
    assert page.wait_for_timeout.call_count == 2


def test_reaction_state_supports_unreacted_aria_pressed_markup():
    button = MagicMock()
    button.get_attribute.side_effect = lambda name: {
        "aria-label": "Like Mike Kim's post",
        "aria-pressed": "false",
        "data-reaction-type": None,
    }.get(name)
    button.inner_text.return_value = "Like"

    assert _reaction_state(button) == "no reaction"


def test_reaction_state_supports_selected_aria_pressed_markup():
    button = MagicMock()
    button.get_attribute.side_effect = lambda name: {
        "aria-label": "Unlike Mike Kim's post",
        "aria-pressed": "true",
        "data-reaction-type": None,
    }.get(name)
    button.inner_text.return_value = "Like"

    assert _reaction_state(button) == "like"


def test_reaction_state_rejects_markup_without_authoritative_state():
    button = MagicMock()
    button.get_attribute.side_effect = lambda name: {
        "aria-label": "Like Mike Kim's post",
        "aria-pressed": None,
        "data-reaction-type": None,
    }.get(name)
    button.inner_text.return_value = "Like"

    with pytest.raises(FeedLikeSendError, match="ambiguous"):
        _reaction_state(button)
