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
            side_effect=[None, None, None, submit],
        ) as find_submit,
    ):
        result = _wait_for_comment_submit(page, editor, timeout_ms=250)

    assert result is submit
    assert find_submit.call_count == 4
    page.wait_for_timeout.assert_called_once_with(250)


def test_comment_submit_text_fallback_stays_near_editor():
    from linkedin.actions.feed_comment import _wait_for_comment_submit

    page = MagicMock()
    editor = MagicMock()
    scopes = [MagicMock() for _ in range(4)]

    with (
        patch(
            "linkedin.actions.feed_comment._comment_submit_scopes",
            return_value=scopes,
        ),
        patch(
            "linkedin.actions.feed_comment._first_enabled_visible",
            return_value=None,
        ) as find_submit,
    ):
        result = _wait_for_comment_submit(page, editor, timeout_ms=0)

    assert result is None
    assert [call.args[0] for call in find_submit.call_args_list[4:]] == scopes[:3]


def test_comment_verification_requires_rendered_comment_item():
    from linkedin.actions.feed_comment import _comment_visible

    page = MagicMock()
    candidates = page.locator.return_value
    candidates.count.return_value = 0

    assert _comment_visible(page, "Useful point.", timeout_ms=0) is False
    assert page.locator.call_args.args[0] != "body"


def test_comment_verification_accepts_visible_comment_item():
    from linkedin.actions.feed_comment import _comment_visible

    page = MagicMock()
    candidates = page.locator.return_value
    candidate = candidates.nth.return_value
    candidates.count.return_value = 1
    candidate.is_visible.return_value = True
    candidate.inner_text.return_value = "Arian 1m Useful point. Like Reply"

    assert _comment_visible(page, "Useful point.", timeout_ms=0) is True
