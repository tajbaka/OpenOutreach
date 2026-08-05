from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.utils import timezone

from linkedin.models import LinkedInFeedComment, LinkedInFeedPost, Task


@pytest.mark.django_db
@patch("linkedin.management.commands.run_feed_comment_once.ensure_self_profile")
@patch("linkedin.management.commands.run_feed_comment_once.handle_feed_comment")
@patch("linkedin.management.commands.run_feed_comment_once.get_or_create_session")
def test_run_feed_comment_once_claims_only_matching_feed_comment(
    get_session,
    handle_comment,
    ensure_identity,
    fake_session,
    capsys,
):
    profile = fake_session.linkedin_profile
    profile.user.username = "athena"
    profile.user.save(update_fields=["username"])
    profile.linkedin_username = "athenaaghdami@gmail.com"
    profile.save(update_fields=["linkedin_username"])

    post = LinkedInFeedPost.objects.create(
        activity_urn="urn:li:activity:run-feed-comment-once",
        post_url="https://www.linkedin.com/feed/update/urn:li:activity:run-feed-comment-once/",
        content_hash="run-feed-comment-once",
    )
    feed_task = Task.objects.create(
        task_type=Task.TaskType.FEED_COMMENT,
        status=Task.Status.PENDING,
        scheduled_at=timezone.now() - timedelta(seconds=1),
        payload={"post_id": post.pk, "operator": "Athena", "message": "Approved."},
    )
    manual_task = Task.objects.create(
        task_type=Task.TaskType.MANUAL_REPLY,
        status=Task.Status.PENDING,
        scheduled_at=timezone.now() - timedelta(minutes=5),
        payload={"lead_id": 999, "operator": "Athena", "message": "Do not claim."},
    )

    session = MagicMock()
    session.campaigns = fake_session.campaigns
    get_session.return_value = session

    def complete_ledger(task, _session, qualifiers):
        assert qualifiers == {}
        LinkedInFeedComment.objects.create(
            task=task,
            post=post,
            operator="Athena",
            comment_text="Approved.",
            status=LinkedInFeedComment.Status.SENT,
        )

    handle_comment.side_effect = complete_ledger

    call_command("run_feed_comment_once", handle="athena")

    feed_task.refresh_from_db()
    manual_task.refresh_from_db()
    assert feed_task.status == Task.Status.COMPLETED
    assert manual_task.status == Task.Status.PENDING
    session.ensure_browser.assert_called_once()
    ensure_identity.assert_called_once_with(session)
    session.close.assert_called_once()
    assert "ledger_status=sent" in capsys.readouterr().out
