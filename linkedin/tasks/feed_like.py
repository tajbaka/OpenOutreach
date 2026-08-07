"""Slack-triggered standalone LinkedIn feed Likes."""
from __future__ import annotations

import logging

from linkedin.models import LinkedInFeedPost
from linkedin.notifications.slack import (
    notify_feed_like_complete,
    notify_feed_like_failed,
    notify_feed_like_uncertain,
)
from linkedin.operators import resolve_operator

logger = logging.getLogger(__name__)


def handle_feed_like(task, session, qualifiers):
    """Ensure the selected sender has liked the exact collected post."""
    del qualifiers

    from linkedin.actions.feed_like import (
        FeedLikeSendError,
        FeedLikeUncertainError,
        ensure_feed_post_liked,
    )

    payload = task.payload or {}
    operator = (payload.get("operator") or "").strip()
    our_operator = resolve_operator(session.linkedin_profile.linkedin_username)
    try:
        if operator != our_operator:
            raise ValueError(
                f"Feed Like for {operator or 'unknown operator'} cannot be sent by {our_operator}"
            )
        post = LinkedInFeedPost.objects.filter(pk=payload.get("post_id")).first()
        if post is None:
            raise ValueError(f"Feed post {payload.get('post_id')} not found")
        if not (post.post_url or post.activity_urn):
            raise ValueError(f"Feed post {post.pk} has no URL or activity URN")

        try:
            result = ensure_feed_post_liked(
                session,
                post_url=post.post_url,
                activity_urn=post.activity_urn,
            )
        except FeedLikeUncertainError as exc:
            notify_feed_like_uncertain(payload, str(exc))
            logger.warning(
                "feed_like uncertain for post=%s operator=%s task=%s: %s",
                post.pk,
                our_operator,
                task.pk,
                exc,
            )
            return
        except FeedLikeSendError:
            raise

        notify_feed_like_complete(
            payload,
            result=result.value,
            post_label=post.author_name or str(post.pk),
        )
        logger.info(
            "feed_like complete for post=%s operator=%s task=%s result=%s",
            post.pk,
            our_operator,
            task.pk,
            result.value,
        )
    except Exception as exc:
        notify_feed_like_failed(payload, str(exc))
        raise
