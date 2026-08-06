from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from playwright.sync_api import Error as PlaywrightError

from linkedin.actions.feed_comment import (
    FeedCommentSendError,
    FeedCommentUncertainError,
    comment_on_feed_post,
)


def test_comment_action_types_marks_attempt_and_verifies_public_comment():
    page = MagicMock()
    button = MagicMock()
    editor = MagicMock()
    submit = MagicMock()
    events = []
    button.click.side_effect = lambda **_kwargs: events.append("open-editor")
    submit.click.side_effect = lambda **_kwargs: events.append("submit")

    with (
        patch(
            "linkedin.actions.feed_comment._first_visible",
            side_effect=[button, editor],
        ),
        patch(
            "linkedin.actions.feed_comment._wait_for_comment_submit",
            return_value=submit,
        ),
        patch(
            "linkedin.actions.feed_comment.human_type",
            side_effect=lambda *_args: events.append("type"),
        ) as human_type,
        patch("linkedin.actions.feed_comment._comment_visible", return_value=True),
    ):
        comment_on_feed_post(
            SimpleNamespace(page=page, context=None),
            post_url="https://www.linkedin.com/feed/update/urn:li:activity:91/",
            comment="Useful point.",
            on_submit_attempt=lambda: events.append("mark-attempt"),
        )

    page.goto.assert_called_once()
    human_type.assert_called_once_with(editor, "Useful point.")
    assert events == ["open-editor", "type", "mark-attempt", "submit"]


def test_comment_action_missing_editor_fails_before_submit_attempt():
    page = MagicMock()
    callback = MagicMock()

    with patch(
        "linkedin.actions.feed_comment._first_visible",
        side_effect=[MagicMock(), None],
    ):
        with pytest.raises(FeedCommentSendError, match="editor was not found"):
            comment_on_feed_post(
                SimpleNamespace(page=page, context=None),
                post_url="https://www.linkedin.com/feed/update/urn:li:activity:91/",
                comment="Useful point.",
                on_submit_attempt=callback,
            )

    callback.assert_not_called()


def test_comment_action_click_failure_after_attempt_is_uncertain():
    page = MagicMock()
    submit = MagicMock()
    submit.click.side_effect = PlaywrightError("click failed")
    callback = MagicMock()

    with (
        patch(
            "linkedin.actions.feed_comment._first_visible",
            side_effect=[MagicMock(), MagicMock()],
        ),
        patch(
            "linkedin.actions.feed_comment._wait_for_comment_submit",
            return_value=submit,
        ),
        patch("linkedin.actions.feed_comment.human_type"),
    ):
        with pytest.raises(FeedCommentUncertainError, match="click failed"):
            comment_on_feed_post(
                SimpleNamespace(page=page, context=None),
                post_url="https://www.linkedin.com/feed/update/urn:li:activity:91/",
                comment="Useful point.",
                on_submit_attempt=callback,
            )

    callback.assert_called_once()


def test_comment_submit_wait_accepts_delayed_comment_label_variant():
    from linkedin.actions.feed_comment import _wait_for_comment_submit

    page = MagicMock()
    editor = MagicMock()
    scope = MagicMock()
    submit = MagicMock()

    with (
        patch(
            "linkedin.actions.feed_comment._comment_submit_scopes",
            return_value=[scope],
        ),
        patch(
            "linkedin.actions.feed_comment._first_enabled_visible",
            side_effect=[None, submit],
        ) as find_submit,
    ):
        result = _wait_for_comment_submit(page, editor, timeout_ms=250)

    assert result is submit
    assert find_submit.call_count == 2
    page.wait_for_timeout.assert_called_once_with(250)
