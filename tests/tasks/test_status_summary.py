from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from crm.models import Deal, Lead, Message
from linkedin.enums import ProfileState
from linkedin.models import ActionLog, Campaign, DaemonHeartbeat, LinkedInProfile, Task
from linkedin.tasks.status_summary import build_status_summary_rows, handle_status_summary


@pytest.mark.django_db
def test_build_status_summary_rows_groups_all_sender_counts(fake_session):
    fake_session.linkedin_profile.linkedin_username = "ariant@tryfedrampgpt.com"
    fake_session.linkedin_profile.save(update_fields=["linkedin_username"])
    other_user = User.objects.create_user(username="athena")
    other_campaign = Campaign.objects.create(name="Athena Campaign", user=other_user)
    other_profile = LinkedInProfile.objects.create(
        user=other_user,
        linkedin_username="athena@getboundera.com",
        linkedin_password="pw",
    )
    DaemonHeartbeat.objects.create(sender="Arian", last_alive=timezone.now())
    DaemonHeartbeat.objects.create(sender="Athena", last_alive=timezone.now())
    fake_session.linkedin_profile.record_action(ActionLog.ActionType.CONNECT, fake_session.campaign)
    other_profile.record_action(ActionLog.ActionType.FOLLOW_UP, other_campaign)

    lead = Lead.objects.create(
        linkedin_url="https://www.linkedin.com/in/alice-status/",
        public_identifier="alice-status",
    )
    Deal.objects.create(
        lead=lead,
        campaign=other_campaign,
        state=ProfileState.CONNECTED,
        connected_at=timezone.now(),
    )
    Deal.objects.create(
        lead=Lead.objects.create(
            linkedin_url="https://www.linkedin.com/in/bob-status/",
            public_identifier="bob-status",
        ),
        campaign=other_campaign,
        state=ProfileState.QUALIFIED,
    )
    Message.objects.create(
        lead=lead,
        source=Message.Source.GMAIL,
        direction=Message.Direction.OUTBOUND,
        sender="athena@getboundera.com",
        external_id="gmail-send:Athena:1:gmail_fallback:step-0:test",
        sent_at=timezone.now(),
        body="email",
    )
    Message.objects.create(
        lead=lead,
        source=Message.Source.LINKEDIN,
        direction=Message.Direction.OUTBOUND,
        sender="athena@getboundera.com",
        external_id="manual-reply:Athena:1:test",
        sent_at=timezone.now(),
        body="manual",
    )

    rows = {
        row["sender"]: row
        for row in build_status_summary_rows(since=timezone.now() - timedelta(hours=1))
    }

    assert rows["Arian"]["connects_today"] == 1
    assert rows["Athena"]["linkedin_followups_today"] == 1
    assert rows["Athena"]["email_followups_today"] == 1
    assert rows["Athena"]["manual_replies_today"] == 1
    assert rows["Athena"]["newly_connected"] == 1
    assert rows["Athena"]["qualified_remaining"] == 1


@pytest.mark.django_db
@patch("linkedin.tasks.status_summary.notify_status_summary")
def test_handle_status_summary_posts_and_reschedules(mock_notify, fake_session):
    fake_session.linkedin_profile.linkedin_username = "ariant@tryfedrampgpt.com"
    fake_session.linkedin_profile.save(update_fields=["linkedin_username"])
    DaemonHeartbeat.objects.create(sender="Arian", last_alive=timezone.now())
    task = Task.objects.create(
        task_type=Task.TaskType.STATUS_SUMMARY,
        status=Task.Status.RUNNING,
        scheduled_at=timezone.now(),
        started_at=timezone.now(),
        payload={"since": (timezone.now() - timedelta(hours=1)).isoformat()},
    )

    handle_status_summary(task, fake_session, qualifiers={})

    mock_notify.assert_called_once()
    next_task = Task.objects.get(
        task_type=Task.TaskType.STATUS_SUMMARY,
        status=Task.Status.PENDING,
    )
    assert next_task.scheduled_at > timezone.now() + timedelta(minutes=55)


@pytest.mark.django_db
def test_build_status_summary_rows_suppresses_offline_and_limited_senders(fake_session, monkeypatch):
    import linkedin.models as linkedin_models

    monkeypatch.setitem(linkedin_models._LIMIT_OVERRIDES, "connect_daily_limit", 1)
    fake_session.linkedin_profile.linkedin_username = "ariant@tryfedrampgpt.com"
    fake_session.linkedin_profile.connect_daily_limit = 1
    fake_session.linkedin_profile.save(update_fields=["linkedin_username", "connect_daily_limit"])
    fake_session.linkedin_profile.record_action(ActionLog.ActionType.CONNECT, fake_session.campaign)
    DaemonHeartbeat.objects.create(sender="Arian", last_alive=timezone.now())

    other_user = User.objects.create_user(username="athena-limited")
    Campaign.objects.create(name="Athena Campaign", user=other_user)
    LinkedInProfile.objects.create(
        user=other_user,
        linkedin_username="athena@getboundera.com",
        linkedin_password="pw",
    )

    rows = build_status_summary_rows(since=timezone.now() - timedelta(hours=1))

    assert rows == []


@pytest.mark.django_db
@patch("linkedin.tasks.status_summary.notify_status_summary")
def test_handle_status_summary_suppresses_empty_rows_but_reschedules(
    mock_notify,
    fake_session,
    monkeypatch,
):
    import linkedin.models as linkedin_models

    monkeypatch.setitem(linkedin_models._LIMIT_OVERRIDES, "connect_daily_limit", 1)
    fake_session.linkedin_profile.linkedin_username = "ariant@tryfedrampgpt.com"
    fake_session.linkedin_profile.connect_daily_limit = 1
    fake_session.linkedin_profile.save(update_fields=["linkedin_username", "connect_daily_limit"])
    fake_session.linkedin_profile.record_action(ActionLog.ActionType.CONNECT, fake_session.campaign)
    DaemonHeartbeat.objects.create(sender="Arian", last_alive=timezone.now())
    task = Task.objects.create(
        task_type=Task.TaskType.STATUS_SUMMARY,
        status=Task.Status.RUNNING,
        scheduled_at=timezone.now(),
        started_at=timezone.now(),
        payload={"since": (timezone.now() - timedelta(hours=1)).isoformat()},
    )

    handle_status_summary(task, fake_session, qualifiers={})

    mock_notify.assert_not_called()
    assert Task.objects.filter(
        task_type=Task.TaskType.STATUS_SUMMARY,
        status=Task.Status.PENDING,
    ).exists()
