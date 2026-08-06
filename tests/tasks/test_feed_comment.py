from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from linkedin.actions.feed_comment import FeedCommentSendError, FeedCommentUncertainError
from linkedin.actions.feed_like import FeedLikeResult, FeedLikeSendError
from linkedin.models import LinkedInFeedComment, LinkedInFeedPost, Task
from linkedin.tasks.feed_comment import handle_feed_comment


@pytest.fixture(autouse=True)
def feed_like():
    with patch("linkedin.actions.feed_like.ensure_feed_post_liked") as ensure_like:
        ensure_like.return_value = FeedLikeResult.ALREADY_LIKED
        yield ensure_like


def _post(suffix="default"):
    return LinkedInFeedPost.objects.create(
        activity_urn=f"urn:li:activity:{suffix}",
        post_url=f"https://www.linkedin.com/feed/update/urn:li:activity:{suffix}/",
        content_hash=f"feed-comment-{suffix}",
        author_name="Ada Lovelace",
        post_text="Evidence quality matters.",
    )


def _task(post, *, operator="testuser@example.com", message="Useful point."):
    return Task.objects.create(
        task_type=Task.TaskType.FEED_COMMENT,
        status=Task.Status.PENDING,
        scheduled_at=timezone.now() - timedelta(seconds=1),
        payload={
            "post_id": post.pk,
            "operator": operator,
            "message": message,
            "slack_channel_id": "C123",
            "slack_message_ts": "171234.567",
            "slack_status_message_ts": "171235.000",
        },
    )


@pytest.mark.django_db
@patch("linkedin.tasks.feed_comment.notify_feed_comment_sent")
@patch("linkedin.actions.feed_comment.comment_on_feed_post")
def test_handle_feed_comment_posts_and_marks_durable_ledger(
    send_comment,
    notify_sent,
    fake_session,
    feed_like,
):
    post = _post("success")
    task = _task(post)

    def send_side_effect(*_args, **kwargs):
        kwargs["on_submit_attempt"]()

    send_comment.side_effect = send_side_effect

    handle_feed_comment(task, fake_session, qualifiers={})

    send_comment.assert_called_once()
    assert send_comment.call_args.kwargs["post_url"] == post.post_url
    assert send_comment.call_args.kwargs["comment"] == "Useful point."
    ledger = LinkedInFeedComment.objects.get(task=task)
    assert ledger.status == LinkedInFeedComment.Status.SENT
    assert ledger.submit_attempted_at is not None
    assert ledger.commented_at is not None
    feed_like.assert_called_once_with(
        fake_session,
        post_url=post.post_url,
        activity_urn=post.activity_urn,
    )
    notify_sent.assert_called_once()
    assert notify_sent.call_args.args[0]["slack_status_message_ts"] == "171235.000"
    assert notify_sent.call_args.kwargs == {
        "post_label": "Ada Lovelace",
        "like_result": "already_liked",
    }


@pytest.mark.django_db
@patch("linkedin.tasks.feed_comment.notify_feed_comment_failed")
@patch("linkedin.actions.feed_comment.comment_on_feed_post")
def test_handle_feed_comment_blocks_wrong_sender(
    send_comment,
    notify_failed,
    fake_session,
):
    task = _task(_post("wrong-sender"), operator="Chuka")

    with pytest.raises(ValueError, match="cannot be sent"):
        handle_feed_comment(task, fake_session, qualifiers={})

    send_comment.assert_not_called()
    notify_failed.assert_called_once()
    assert not LinkedInFeedComment.objects.filter(task=task).exists()


@pytest.mark.django_db
@patch("linkedin.tasks.feed_comment.notify_feed_comment_skipped")
@patch("linkedin.actions.feed_comment.comment_on_feed_post")
def test_handle_feed_comment_skips_same_comment_already_sent(
    send_comment,
    notify_skipped,
    fake_session,
):
    post = _post("already-sent")
    LinkedInFeedComment.objects.create(
        post=post,
        operator="testuser@example.com",
        comment_text="  Useful   point. ",
        status=LinkedInFeedComment.Status.SENT,
        commented_at=timezone.now(),
    )
    task = _task(post, message="Useful point.")

    handle_feed_comment(task, fake_session, qualifiers={})

    send_comment.assert_not_called()
    ledger = LinkedInFeedComment.objects.get(task=task)
    assert ledger.status == LinkedInFeedComment.Status.SKIPPED
    notify_skipped.assert_called_once()


@pytest.mark.django_db
@patch("linkedin.tasks.feed_comment.notify_feed_comment_uncertain")
@patch("linkedin.actions.feed_comment.comment_on_feed_post")
def test_handle_feed_comment_fails_closed_for_uncertain_prior_attempt(
    send_comment,
    notify_uncertain,
    fake_session,
):
    post = _post("prior-uncertain")
    LinkedInFeedComment.objects.create(
        post=post,
        operator="testuser@example.com",
        comment_text="Useful point.",
        status=LinkedInFeedComment.Status.UNCERTAIN,
        submit_attempted_at=timezone.now(),
    )
    task = _task(post)

    handle_feed_comment(task, fake_session, qualifiers={})

    send_comment.assert_not_called()
    ledger = LinkedInFeedComment.objects.get(task=task)
    assert ledger.status == LinkedInFeedComment.Status.UNCERTAIN
    notify_uncertain.assert_called_once()


@pytest.mark.django_db
@patch("linkedin.tasks.feed_comment.notify_feed_comment_uncertain")
@patch("linkedin.actions.feed_comment.comment_on_feed_post")
def test_handle_feed_comment_marks_uncertain_after_submit_attempt(
    send_comment,
    notify_uncertain,
    fake_session,
):
    post = _post("send-uncertain")
    task = _task(post)

    def uncertain(*_args, **kwargs):
        kwargs["on_submit_attempt"]()
        raise FeedCommentUncertainError("Could not verify comment")

    send_comment.side_effect = uncertain

    handle_feed_comment(task, fake_session, qualifiers={})

    ledger = LinkedInFeedComment.objects.get(task=task)
    assert ledger.status == LinkedInFeedComment.Status.UNCERTAIN
    assert ledger.submit_attempted_at is not None
    notify_uncertain.assert_called_once()


@pytest.mark.django_db
@patch("linkedin.tasks.feed_comment.notify_feed_comment_uncertain")
@patch("linkedin.actions.feed_comment.comment_on_feed_post")
def test_handle_feed_comment_recovered_task_does_not_resubmit(
    send_comment,
    notify_uncertain,
    fake_session,
):
    post = _post("recovered-task")
    task = _task(post)
    ledger = LinkedInFeedComment.objects.create(
        task=task,
        post=post,
        operator="testuser@example.com",
        comment_text="Useful point.",
        status=LinkedInFeedComment.Status.RUNNING,
        submit_attempted_at=timezone.now(),
    )

    handle_feed_comment(task, fake_session, qualifiers={})

    send_comment.assert_not_called()
    ledger.refresh_from_db()
    assert ledger.status == LinkedInFeedComment.Status.UNCERTAIN
    notify_uncertain.assert_called_once()


@pytest.mark.django_db
@patch("linkedin.tasks.feed_comment.notify_feed_comment_failed")
@patch("linkedin.actions.feed_comment.comment_on_feed_post")
def test_handle_feed_comment_pre_submit_failure_is_retryable_failed_state(
    send_comment,
    notify_failed,
    fake_session,
):
    post = _post("pre-submit-failure")
    task = _task(post)
    send_comment.side_effect = FeedCommentSendError("Editor not found")

    with pytest.raises(FeedCommentSendError, match="Editor not found"):
        handle_feed_comment(task, fake_session, qualifiers={})

    ledger = LinkedInFeedComment.objects.get(task=task)
    assert ledger.status == LinkedInFeedComment.Status.FAILED
    assert ledger.submit_attempted_at is None
    notify_failed.assert_called_once()


@pytest.mark.django_db
@patch("linkedin.tasks.feed_comment.notify_feed_comment_sent")
@patch("linkedin.actions.feed_comment.comment_on_feed_post")
def test_handle_feed_comment_posts_when_auto_like_fails(
    send_comment,
    notify_sent,
    fake_session,
    feed_like,
):
    post = _post("like-failure")
    task = _task(post)
    feed_like.side_effect = FeedLikeSendError("Reaction button missing")

    def send_side_effect(*_args, **kwargs):
        kwargs["on_submit_attempt"]()

    send_comment.side_effect = send_side_effect

    handle_feed_comment(task, fake_session, qualifiers={})

    ledger = LinkedInFeedComment.objects.get(task=task)
    assert ledger.status == LinkedInFeedComment.Status.SENT
    notify_sent.assert_called_once()
    assert notify_sent.call_args.kwargs == {
        "post_label": "Ada Lovelace",
        "like_result": "failed",
    }
