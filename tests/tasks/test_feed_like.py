from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from linkedin.actions.feed_like import FeedLikeResult, FeedLikeUncertainError
from linkedin.models import LinkedInFeedPost, Task
from linkedin.tasks.feed_like import handle_feed_like


def _post(suffix):
    return LinkedInFeedPost.objects.create(
        activity_urn=f"urn:li:activity:{suffix}",
        post_url=f"https://www.linkedin.com/feed/update/urn:li:activity:{suffix}/",
        content_hash=f"feed-like-{suffix}",
        author_name="Ada Lovelace",
        post_text="Evidence quality matters.",
    )


def _task(post, operator="testuser@example.com"):
    return Task.objects.create(
        task_type=Task.TaskType.FEED_LIKE,
        status=Task.Status.PENDING,
        scheduled_at=timezone.now() - timedelta(seconds=1),
        payload={
            "post_id": post.pk,
            "operator": operator,
            "slack_blocks": [{"type": "actions", "elements": []}],
        },
    )


@pytest.mark.django_db
@patch("linkedin.tasks.feed_like.notify_feed_like_complete")
@patch("linkedin.actions.feed_like.ensure_feed_post_liked")
def test_handle_feed_like_uses_idempotent_action(ensure_like, notify_complete, fake_session):
    post = _post("success")
    task = _task(post)
    ensure_like.return_value = FeedLikeResult.ALREADY_LIKED

    handle_feed_like(task, fake_session, qualifiers={})

    ensure_like.assert_called_once_with(
        fake_session,
        post_url=post.post_url,
        activity_urn=post.activity_urn,
    )
    notify_complete.assert_called_once_with(
        task.payload,
        result="already_liked",
        post_label="Ada Lovelace",
    )


@pytest.mark.django_db
@patch("linkedin.tasks.feed_like.notify_feed_like_failed")
@patch("linkedin.actions.feed_like.ensure_feed_post_liked")
def test_handle_feed_like_blocks_wrong_sender(ensure_like, notify_failed, fake_session):
    task = _task(_post("wrong-sender"), operator="Chuka")

    with pytest.raises(ValueError, match="cannot be sent"):
        handle_feed_like(task, fake_session, qualifiers={})

    ensure_like.assert_not_called()
    notify_failed.assert_called_once()


@pytest.mark.django_db
@patch("linkedin.tasks.feed_like.notify_feed_like_uncertain")
@patch("linkedin.actions.feed_like.ensure_feed_post_liked")
def test_handle_feed_like_reports_uncertain_without_toggling_again(
    ensure_like,
    notify_uncertain,
    fake_session,
):
    task = _task(_post("uncertain"))
    ensure_like.side_effect = FeedLikeUncertainError("could not verify")

    handle_feed_like(task, fake_session, qualifiers={})

    notify_uncertain.assert_called_once_with(task.payload, "could not verify")
