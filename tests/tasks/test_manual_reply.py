from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone as dj_tz

from crm.models import Deal, Lead, Message
from linkedin.enums import ProfileState
from linkedin.models import ActionLog, Task
from linkedin.tasks.manual_reply import handle_manual_reply


def _task(lead, *, operator="testuser@example.com", message="Thanks for the note"):
    return Task.objects.create(
        task_type=Task.TaskType.MANUAL_REPLY,
        status=Task.Status.PENDING,
        scheduled_at=dj_tz.now() - timedelta(seconds=1),
        payload={
            "lead_id": lead.pk,
            "operator": operator,
            "message": message,
            "slack_response_url": "https://hooks.slack.test/actions/1/2/3",
        },
    )


@pytest.mark.django_db
@patch("linkedin.actions.message.send_raw_message", return_value=True)
@patch("linkedin.tasks.manual_reply.notify_manual_reply_sent")
def test_handle_manual_reply_sends_without_state_or_quota_side_effects(
    notify_sent,
    send_raw,
    fake_session,
):
    lead = Lead.objects.create(
        first_name="Alice",
        last_name="Manual",
        linkedin_url="https://www.linkedin.com/in/alice-manual-reply/",
        public_identifier="alice-manual-reply",
    )
    deal = Deal.objects.create(
        lead=lead,
        campaign=fake_session.campaign,
        state=ProfileState.CONNECTED,
    )
    task = _task(lead)

    handle_manual_reply(task, fake_session, qualifiers={})

    send_raw.assert_called_once()
    _, profile, message = send_raw.call_args.args
    assert profile["public_identifier"] == "alice-manual-reply"
    assert message == "Thanks for the note"
    assert send_raw.call_args.kwargs["operator"] == "testuser@example.com"
    assert send_raw.call_args.kwargs["external_id_kind"] == "manual-reply"
    assert send_raw.call_args.kwargs["prefer_direct"] is True
    assert send_raw.call_args.kwargs["allow_api_fallback"] is False
    assert send_raw.call_args.kwargs["raise_on_failure"] is True
    notify_sent.assert_called_once()
    deal.refresh_from_db()
    assert deal.state == ProfileState.CONNECTED
    assert ActionLog.objects.count() == 0


@pytest.mark.django_db
@patch("linkedin.actions.message.send_raw_message")
@patch("linkedin.tasks.manual_reply.notify_manual_reply_failed")
def test_handle_manual_reply_blocks_wrong_operator(notify_failed, send_raw, fake_session):
    lead = Lead.objects.create(
        linkedin_url="https://www.linkedin.com/in/alice-wrong-operator/",
        public_identifier="alice-wrong-operator",
    )
    task = _task(lead, operator="Chuka")

    with pytest.raises(ValueError, match="cannot be sent"):
        handle_manual_reply(task, fake_session, qualifiers={})

    send_raw.assert_not_called()
    notify_failed.assert_called_once()


@pytest.mark.django_db
@patch("linkedin.actions.message.send_raw_message", return_value=False)
@patch("linkedin.tasks.manual_reply.notify_manual_reply_failed")
def test_handle_manual_reply_reports_send_failure(notify_failed, send_raw, fake_session):
    lead = Lead.objects.create(
        linkedin_url="https://www.linkedin.com/in/alice-send-failure/",
        public_identifier="alice-send-failure",
    )
    task = _task(lead)

    with pytest.raises(RuntimeError, match="LinkedIn send failed"):
        handle_manual_reply(task, fake_session, qualifiers={})

    send_raw.assert_called_once()
    notify_failed.assert_called_once()


@pytest.mark.django_db
@patch("linkedin.actions.message.send_raw_message")
@patch("linkedin.tasks.manual_reply.notify_manual_reply_sent")
@patch("linkedin.tasks.manual_reply.notify_manual_reply_failed")
def test_handle_manual_reply_skips_already_persisted_manual_send(
    notify_failed,
    notify_sent,
    send_raw,
    fake_session,
):
    lead = Lead.objects.create(
        linkedin_url="https://www.linkedin.com/in/alice-manual-dedup/",
        public_identifier="alice-manual-dedup",
    )
    Message.objects.create(
        lead=lead,
        source=Message.Source.LINKEDIN,
        external_id=f"manual-reply:testuser@example.com:{lead.pk}:123",
        direction=Message.Direction.OUTBOUND,
        sender="testuser@example.com",
        body="Thanks for the note",
        sent_at=dj_tz.now(),
    )
    task = _task(lead, message="Thanks for the note")

    handle_manual_reply(task, fake_session, qualifiers={})

    send_raw.assert_not_called()
    notify_sent.assert_not_called()
    notify_failed.assert_not_called()
