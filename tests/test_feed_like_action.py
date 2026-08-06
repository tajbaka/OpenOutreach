from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from linkedin.actions.feed_like import (
    FeedLikeResult,
    FeedLikeSendError,
    FeedLikeUncertainError,
    REACTION_BUTTON_SELECTOR,
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
            "linkedin.actions.feed_like._unique_visible_reaction_button",
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
        "linkedin.actions.feed_like._unique_visible_reaction_button",
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
        "linkedin.actions.feed_like._unique_visible_reaction_button",
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
            "linkedin.actions.feed_like._unique_visible_reaction_button",
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
