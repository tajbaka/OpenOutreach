"""Slack-triggered public LinkedIn feed comments."""
from __future__ import annotations

import logging
import re

from django.utils import timezone

from linkedin.models import LinkedInFeedComment, LinkedInFeedPost
from linkedin.notifications.slack import (
    notify_feed_comment_failed,
    notify_feed_comment_sent,
    notify_feed_comment_skipped,
    notify_feed_comment_uncertain,
)
from linkedin.operators import resolve_operator

logger = logging.getLogger(__name__)

_WHITESPACE_RE = re.compile(r"\s+")


def handle_feed_comment(task, session, qualifiers):
    """Post a Slack-composed public comment from the sender's daemon account."""
    del qualifiers

    from linkedin.actions.feed_comment import (
        FeedCommentSendError,
        FeedCommentUncertainError,
        comment_on_feed_post,
    )

    payload = task.payload or {}
    operator = (payload.get("operator") or "").strip()
    message = (payload.get("message") or "").strip()
    our_operator = resolve_operator(session.linkedin_profile.linkedin_username)
    notification_payload = _notification_payload(payload)
    like_result = ""

    try:
        if operator != our_operator:
            raise ValueError(
                f"Feed comment for {operator or 'unknown operator'} cannot be sent by {our_operator}"
            )
        if not message:
            raise ValueError("Feed comment message is empty")

        post = LinkedInFeedPost.objects.filter(pk=payload.get("post_id")).first()
        if post is None:
            raise ValueError(f"Feed post {payload.get('post_id')} not found")
        if not (post.post_url or post.activity_urn):
            raise ValueError(f"Feed post {post.pk} has no URL or activity URN")

        comment = _prepare_ledger(
            task=task,
            post=post,
            operator=our_operator,
            account_username=(
                payload.get("account_username")
                or session.linkedin_profile.linkedin_username
            ),
            message=message,
            notification_payload=notification_payload,
        )
        if _resume_guard(comment, notification_payload):
            return
        duplicate_result = _duplicate_guard(comment)
        if duplicate_result is not None:
            _apply_duplicate_result(comment, duplicate_result, notification_payload)
            return

        like_result = _ensure_post_liked(
            session,
            post=post,
            operator=our_operator,
            task_id=task.pk,
        )

        try:
            comment_on_feed_post(
                session,
                post_url=post.post_url,
                activity_urn=post.activity_urn,
                comment=message,
                on_submit_attempt=lambda: _mark_submit_attempted(comment),
            )
        except FeedCommentUncertainError as exc:
            _mark_uncertain(comment, str(exc))
            notify_feed_comment_uncertain(notification_payload, str(exc))
            logger.warning(
                "feed_comment uncertain for post=%s operator=%s task=%s: %s",
                post.pk,
                our_operator,
                task.pk,
                exc,
            )
            return
        except FeedCommentSendError as exc:
            _mark_failed(comment, str(exc))
            raise

        comment.status = LinkedInFeedComment.Status.SENT
        comment.commented_at = timezone.now()
        comment.error = ""
        comment.save(update_fields=["status", "commented_at", "error", "updated_at"])
        notify_feed_comment_sent(
            notification_payload,
            post_label=post.author_name or f"post {post.pk}",
            like_result=like_result,
        )
        logger.info(
            "feed_comment sent for post=%s operator=%s task=%s",
            post.pk,
            our_operator,
            task.pk,
        )
    except Exception as exc:
        if "comment" in locals():
            _mark_failed(comment, str(exc))
        notify_feed_comment_failed(notification_payload, str(exc))
        raise


def _ensure_post_liked(session, *, post, operator: str, task_id: int) -> str:
    """Best-effort Like lane; a reaction failure never blocks the approved comment."""
    from linkedin.actions.feed_like import (
        FeedLikeSendError,
        FeedLikeUncertainError,
        ensure_feed_post_liked,
    )

    try:
        return ensure_feed_post_liked(
            session,
            post_url=post.post_url,
            activity_urn=post.activity_urn,
        ).value
    except FeedLikeUncertainError as exc:
        logger.warning(
            "feed Like uncertain for post=%s operator=%s task=%s: %s",
            post.pk,
            operator,
            task_id,
            exc,
        )
        return "uncertain"
    except FeedLikeSendError as exc:
        logger.warning(
            "feed Like failed for post=%s operator=%s task=%s: %s",
            post.pk,
            operator,
            task_id,
            exc,
        )
        return "failed"


def _prepare_ledger(
    *,
    task,
    post: LinkedInFeedPost,
    operator: str,
    account_username: str,
    message: str,
    notification_payload: dict,
) -> LinkedInFeedComment:
    comment = LinkedInFeedComment.objects.filter(task=task).first()
    if comment is None:
        comment = LinkedInFeedComment.objects.create(
            task=task,
            post=post,
            operator=operator,
            account_username=account_username,
            comment_text=message,
            status=LinkedInFeedComment.Status.QUEUED,
            slack_channel_id=notification_payload.get("slack_channel_id", ""),
            slack_message_ts=notification_payload.get("slack_message_ts", ""),
            slack_response_url=notification_payload.get("slack_response_url", ""),
            slack_user_id=notification_payload.get("slack_user_id", ""),
        )

    resumed_after_submit = comment.submit_attempted_at is not None
    comment.post = post
    comment.operator = operator
    comment.account_username = account_username
    comment.comment_text = message
    update_fields = ["post", "operator", "account_username", "comment_text"]
    if not resumed_after_submit:
        comment.status = LinkedInFeedComment.Status.RUNNING
        comment.error = ""
        update_fields.extend(["status", "error"])
    comment.save(update_fields=[*update_fields, "updated_at"])
    return comment


def _resume_guard(comment: LinkedInFeedComment, notification_payload: dict) -> bool:
    """Fail closed when this exact task resumes after a possible UI mutation."""
    if comment.status == LinkedInFeedComment.Status.SENT:
        notify_feed_comment_skipped(
            notification_payload,
            "This task was already recorded as sent.",
        )
        return True
    if comment.submit_attempted_at is None:
        return False
    reason = "A prior run reached LinkedIn submit; verify the post before retrying."
    _mark_uncertain(comment, reason)
    notify_feed_comment_uncertain(notification_payload, reason)
    return True


def _duplicate_guard(comment: LinkedInFeedComment) -> tuple[str, str] | None:
    wanted = _normalize_comment(comment.comment_text)
    prior = (
        LinkedInFeedComment.objects
        .filter(post=comment.post, operator=comment.operator)
        .exclude(pk=comment.pk)
        .order_by("-created_at")
    )
    for existing in prior:
        if _normalize_comment(existing.comment_text) != wanted:
            continue
        if existing.status == LinkedInFeedComment.Status.SENT:
            return (
                LinkedInFeedComment.Status.SKIPPED,
                "Same comment was already recorded as sent.",
            )
        if existing.status in {
            LinkedInFeedComment.Status.RUNNING,
            LinkedInFeedComment.Status.UNCERTAIN,
        }:
            return (
                LinkedInFeedComment.Status.UNCERTAIN,
                "Same comment has an in-flight or uncertain prior attempt.",
            )
        if (
            existing.status == LinkedInFeedComment.Status.FAILED
            and existing.submit_attempted_at is not None
        ):
            return (
                LinkedInFeedComment.Status.UNCERTAIN,
                "Prior failed attempt may have submitted; verify the post before retrying.",
            )
    return None


def _apply_duplicate_result(
    comment: LinkedInFeedComment,
    result: tuple[str, str],
    notification_payload: dict,
) -> None:
    status, reason = result
    comment.status = status
    comment.error = reason
    comment.save(update_fields=["status", "error", "updated_at"])
    if status == LinkedInFeedComment.Status.UNCERTAIN:
        notify_feed_comment_uncertain(notification_payload, reason)
    else:
        notify_feed_comment_skipped(notification_payload, reason)
    logger.info(
        "feed_comment duplicate guard post=%s operator=%s status=%s: %s",
        comment.post_id,
        comment.operator,
        status,
        reason,
    )


def _mark_failed(comment: LinkedInFeedComment, error: str) -> None:
    if comment.status == LinkedInFeedComment.Status.SENT:
        return
    comment.status = LinkedInFeedComment.Status.FAILED
    comment.error = error[:4000]
    comment.save(update_fields=["status", "error", "updated_at"])


def _mark_uncertain(comment: LinkedInFeedComment, error: str) -> None:
    now = timezone.now()
    comment.status = LinkedInFeedComment.Status.UNCERTAIN
    comment.submit_attempted_at = now
    comment.error = error[:4000]
    comment.save(
        update_fields=[
            "status", "submit_attempted_at", "error", "updated_at",
        ],
    )


def _mark_submit_attempted(comment: LinkedInFeedComment) -> None:
    comment.submit_attempted_at = timezone.now()
    comment.save(update_fields=["submit_attempted_at", "updated_at"])


def _notification_payload(payload: dict) -> dict:
    return {
        "slack_channel_id": payload.get("slack_channel_id", ""),
        "slack_message_ts": payload.get("slack_message_ts", ""),
        "slack_response_url": payload.get("slack_response_url", ""),
        "slack_user_id": payload.get("slack_user_id", ""),
        "slack_status_message_ts": payload.get("slack_status_message_ts", ""),
    }


def _normalize_comment(value: str) -> str:
    return _WHITESPACE_RE.sub(" ", (value or "").strip()).lower()
