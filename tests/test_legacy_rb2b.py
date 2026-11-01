"""Historical receipts remain effective after retiring the old send executor."""
from datetime import timedelta

import pytest
from django.utils import timezone

from crm.models import Lead, Message
from linkedin import legacy_rb2b
from linkedin.models import ActionLog, RB2BNotification, RB2BVisitor
from linkedin.realtime.lead_lookup import resolve_lead_for_realtime

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("status", ["attempting", "sent", "uncertain", "failed"])
def test_old_reservations_still_consume_all_shared_budgets(fake_session, status):
    profile = fake_session.linkedin_profile
    row = RB2BVisitor.objects.create(
        public_identifier="historical", linkedin_url="https://www.linkedin.com/in/historical/",
        linkedin_profile=profile, status=status, attempted_at=timezone.now())
    assert profile._daily_count("connect") == 1
    assert profile._weekly_count("connect") == 1
    assert profile._total_daily_count() == 1
    assert profile._daily_count("follow_up") == 0
    assert not ActionLog.objects.exists()
    row.refresh_from_db()
    assert row.status == status  # no rewriting/refunding old attempts


def test_old_reply_uses_exact_member_and_owner_and_existing_notice_ledger(fake_session, monkeypatch):
    profile = fake_session.linkedin_profile
    profile.linkedin_username = "ariantajbakh"
    profile.save(update_fields=["linkedin_username"])
    lead = Lead.objects.create(first_name="Historical", last_name="Person", public_identifier="historical")
    visitor = RB2BVisitor.objects.create(
        public_identifier="historical", linkedin_url="https://www.linkedin.com/in/historical/",
        linkedin_profile=profile, lead=lead, member_urn="urn:li:fsd_profile:historic", status="sent")
    assert resolve_lead_for_realtime(conversation_urn="", sender_member_urn=visitor.member_urn, operator="Arian") == lead
    assert resolve_lead_for_realtime(conversation_urn="", sender_member_urn=visitor.member_urn, operator="Chuka") is None
    message = Message.objects.create(lead=lead, source="linkedin", external_id="urn:li:msg:historic",
        direction="inbound", body="Synthetic reply", sent_at=timezone.now(), thread_external_id="urn:li:thread:historic")
    legacy_rb2b.queue_reply_notifications(lead, [message.pk])
    legacy_rb2b.queue_reply_notifications(lead, [message.pk])
    calls = []
    monkeypatch.setattr("linkedin.notifications.slack.notify_message_received", lambda **kw: calls.append(kw) or True)
    assert legacy_rb2b.flush_notifications() == 1
    assert legacy_rb2b.flush_notifications() == 0
    assert len(calls) == 1 and calls[0]["thread_external_id"] == message.thread_external_id
    assert RB2BNotification.objects.get().key == f"reply:{message.pk}"


def test_old_notice_drain_cannot_take_new_integrations_outbox(fake_session, monkeypatch):
    visitor = RB2BVisitor.objects.create(public_identifier="new", linkedin_url="https://www.linkedin.com/in/new/",
                                        linkedin_profile=fake_session.linkedin_profile)
    notice = RB2BNotification.objects.create(visitor=visitor, key="event:synthetic", kind="sent", detail="New callback")
    monkeypatch.setattr("linkedin.notifications.slack._slack_api", lambda *a, **kw: pytest.fail("New outbox stolen"))
    assert legacy_rb2b.flush_notifications() == 0
    notice.refresh_from_db()
    assert notice.sent_at is None and notice.attempts == 0
