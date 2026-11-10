"""Tests for the one-notice-per-inbound-message claim."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from crm.models import Lead, Message
from linkedin.realtime.reply_notice import (
    ALREADY_SENT,
    FAILED,
    OUTBOX,
    POSTED,
    deliver_reply_notice,
    reply_notice_sent,
)


def _inbound(lead, external_id="urn:li:msg:n1"):
    return Message.objects.create(
        lead=lead, source=Message.Source.LINKEDIN, external_id=external_id,
        direction=Message.Direction.INBOUND, sender="waylon krush",
        body="Let's talk", sent_at=timezone.now(), thread_external_id="conv-1",
    )


@pytest.fixture
def lead(db):
    return Lead.objects.create(
        first_name="Waylon", last_name="Krush",
        linkedin_url="https://www.linkedin.com/in/waylonkrush/",
    )


def test_posts_once_then_reports_already_sent(lead):
    msg = _inbound(lead)
    notify = MagicMock(return_value=True)

    assert deliver_reply_notice(msg, lead=lead, operator="Arian", notify=notify) == POSTED
    assert deliver_reply_notice(msg, lead=lead, operator="Arian", notify=notify) == ALREADY_SENT

    notify.assert_called_once_with(
        lead=lead, text="Let's talk", operator="Arian", thread_external_id="conv-1",
    )
    msg.refresh_from_db()
    assert reply_notice_sent(msg)


def test_failed_post_releases_claim_so_a_later_path_can_retry(lead):
    msg = _inbound(lead)

    assert deliver_reply_notice(
        msg, lead=lead, operator="Arian", notify=MagicMock(return_value=False),
    ) == FAILED
    msg.refresh_from_db()
    assert not reply_notice_sent(msg)

    retry = MagicMock(return_value=True)
    assert deliver_reply_notice(msg, lead=lead, operator="Arian", notify=retry) == POSTED
    retry.assert_called_once()


def test_claim_keeps_other_raw_fields(lead):
    msg = _inbound(lead)
    Message.objects.filter(pk=msg.pk).update(raw={"received_by": "Arian", "text": "Let's talk"})
    msg.refresh_from_db()

    deliver_reply_notice(msg, lead=lead, operator="Arian", notify=MagicMock(return_value=True))

    msg.refresh_from_db()
    assert msg.raw["received_by"] == "Arian"
    assert msg.raw["text"] == "Let's talk"


def test_default_notifier_is_resolved_at_call_time(lead):
    msg = _inbound(lead)
    with patch("linkedin.notifications.slack.notify_message_received", return_value=True) as notify:
        assert deliver_reply_notice(msg, lead=lead, operator="Arian") == POSTED
    notify.assert_called_once()


def test_legacy_outbox_notice_is_flushed_not_posted_directly(lead):
    msg = _inbound(lead)
    notify = MagicMock(return_value=True)
    with patch("linkedin.legacy_rb2b.owns_reply_notice", return_value=True), \
         patch("linkedin.legacy_rb2b.flush_notifications") as flush:
        assert deliver_reply_notice(msg, lead=lead, operator="Arian", notify=notify) == OUTBOX
    flush.assert_called_once()
    notify.assert_not_called()
