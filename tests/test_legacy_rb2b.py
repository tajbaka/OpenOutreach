"""Historical receipts remain effective after retiring the old send executor."""
from datetime import timedelta

import pytest
from django.utils import timezone

from crm.models import Lead, Message
from linkedin import legacy_rb2b
from linkedin.models import ActionLog, RB2BNotification, RB2BVisitor, Task
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


def test_legacy_member_urn_is_a_fallback_for_any_receiving_sender(fake_session):
    """The visitor row's exact URN identifies the person; it never scopes or hides replies."""
    lead = Lead.objects.create(first_name="Historical", last_name="Person", public_identifier="historical")
    visitor = RB2BVisitor.objects.create(
        public_identifier="historical", linkedin_url="https://www.linkedin.com/in/historical/",
        linkedin_profile=fake_session.linkedin_profile, lead=lead, member_urn="urn:li:fsd_profile:historic",
        status="sent")
    assert resolve_lead_for_realtime(conversation_urn="", sender_member_urn=visitor.member_urn) == lead
    # Ordinary resolution always wins over the legacy fallback.
    enriched = Lead.objects.create(first_name="Enriched", last_name="Person", public_identifier="enriched",
                                   description='{"urn": "urn:li:fsd_profile:historic"}')
    assert resolve_lead_for_realtime(conversation_urn="", sender_member_urn=visitor.member_urn) == enriched
    assert resolve_lead_for_realtime(conversation_urn="", sender_member_urn="urn:li:fsd_profile:other") is None


def test_reply_notice_is_queued_once_with_its_receiving_sender(fake_session, monkeypatch):
    lead = Lead.objects.create(first_name="Historical", last_name="Person", public_identifier="historical")
    RB2BVisitor.objects.create(
        public_identifier="historical", linkedin_url="https://www.linkedin.com/in/historical/",
        linkedin_profile=fake_session.linkedin_profile, lead=lead, member_urn="urn:li:fsd_profile:historic",
        status="sent")
    message = Message.objects.create(lead=lead, source="linkedin", external_id="urn:li:msg:historic",
        direction="inbound", body="Synthetic reply", sent_at=timezone.now(), thread_external_id="urn:li:thread:historic")
    unproven = Message.objects.create(lead=lead, source="linkedin", external_id="urn:li:msg:unproven",
        direction="inbound", body="Who received this?", sent_at=timezone.now(), thread_external_id="urn:li:thread:x")
    legacy_rb2b.queue_reply_notifications(lead, [message.pk], receiver="Chuka")
    legacy_rb2b.queue_reply_notifications(lead, [message.pk], receiver="Chuka")
    legacy_rb2b.queue_reply_notifications(lead, [unproven.pk])  # no provable sender: not queued
    assert legacy_rb2b.owns_reply_notice(message) and not legacy_rb2b.owns_reply_notice(unproven)
    calls = []
    monkeypatch.setattr("linkedin.notifications.slack.notify_message_received", lambda **kw: calls.append(kw) or True)
    assert legacy_rb2b.flush_notifications() == 1
    assert legacy_rb2b.flush_notifications() == 0
    assert [(c["operator"], c["thread_external_id"]) for c in calls] == [("Chuka", message.thread_external_id)]
    assert RB2BNotification.objects.get().key == f"reply:{message.pk}"


@pytest.mark.parametrize("thread_evidence,expected", [("chukwuka agu", "Chuka"), ("", "")])
def test_a_notice_queued_before_attribution_uses_only_the_threads_proven_owner(
        fake_session, monkeypatch, thread_evidence, expected):
    lead = Lead.objects.create(first_name="Historical", last_name="Person", public_identifier="historical")
    visitor = RB2BVisitor.objects.create(
        public_identifier="historical", linkedin_url="https://www.linkedin.com/in/historical/",
        linkedin_profile=fake_session.linkedin_profile, lead=lead, status="sent")
    thread = "urn:li:thread:historic"
    if thread_evidence:
        Message.objects.create(lead=lead, source="linkedin", external_id="urn:li:msg:ours", direction="outbound",
                               sender=thread_evidence, body="Hello", sent_at=timezone.now(), thread_external_id=thread)
    reply = Message.objects.create(lead=lead, source="linkedin", external_id="urn:li:msg:reply", direction="inbound",
                                   body="Reply", sent_at=timezone.now(), thread_external_id=thread)
    RB2BNotification.objects.create(key=f"reply:{reply.pk}", visitor=visitor, message=reply, kind="reply")
    calls = []
    monkeypatch.setattr("linkedin.notifications.slack.notify_message_received", lambda **kw: calls.append(kw) or True)
    assert legacy_rb2b.flush_notifications() == 1
    assert [c["operator"] for c in calls] == [expected]  # never the hard-coded legacy sender


def test_a_failed_notice_is_retried_by_the_background_worker(fake_session, monkeypatch):
    lead = Lead.objects.create(first_name="Historical", last_name="Person", public_identifier="historical")
    visitor = RB2BVisitor.objects.create(
        public_identifier="historical", linkedin_url="https://www.linkedin.com/in/historical/",
        linkedin_profile=fake_session.linkedin_profile, lead=lead, status="sent")
    reply = Message.objects.create(lead=lead, source="linkedin", external_id="urn:li:msg:reply", direction="inbound",
                                   body="Reply", sent_at=timezone.now(), thread_external_id="urn:li:thread:t")
    notice = RB2BNotification.objects.create(key=f"reply:{reply.pk}", visitor=visitor, message=reply, kind="reply",
                                             detail="Arian")
    outcomes = iter([False, True])
    calls = []
    monkeypatch.setattr("linkedin.notifications.slack.notify_message_received",
                        lambda **kw: calls.append(kw["operator"]) or next(outcomes))
    assert legacy_rb2b.flush_notifications() == 0  # Slack failed: kept with a retry time
    notice.refresh_from_db()
    assert notice.sent_at is None and notice.attempts == 1 and notice.next_attempt_at > timezone.now()
    RB2BNotification.objects.filter(pk=notice.pk).update(next_attempt_at=timezone.now() - timedelta(seconds=1))
    worker = legacy_rb2b.NotificationRetryWorker(interval_seconds=0)
    real_flush = legacy_rb2b.flush_notifications

    def flush_once():
        try:
            real_flush()
        finally:
            worker.stop()  # one cycle, whatever happens
    monkeypatch.setattr(legacy_rb2b, "flush_notifications", flush_once)
    # The worker's own thread recycles its DB connection; inside a test
    # transaction that would abort it, so run the one cycle on this connection.
    monkeypatch.setattr(legacy_rb2b, "close_old_connections", lambda: None)
    worker.run()
    notice.refresh_from_db()
    assert notice.sent_at is not None and calls == ["Arian", "Arian"]
    assert not Task.objects.exists()  # delivery only: no outreach task revived


def test_the_retry_worker_starts_only_while_legacy_notices_are_possible(fake_session):
    assert not legacy_rb2b.legacy_notices_possible()
    visitor = RB2BVisitor.objects.create(public_identifier="intake-only", linkedin_url="https://www.linkedin.com/in/intake-only/",
                                         linkedin_profile=fake_session.linkedin_profile)
    RB2BNotification.objects.create(visitor=visitor, key="event:integration", kind="sent")
    assert not legacy_rb2b.legacy_notices_possible()  # the integration's own outbox is not ours
    RB2BNotification.objects.create(visitor=visitor, key="visitor:legacy", kind="held")
    assert legacy_rb2b.legacy_notices_possible()
    RB2BNotification.objects.filter(key="visitor:legacy").update(sent_at=timezone.now())
    assert not legacy_rb2b.legacy_notices_possible()
    RB2BVisitor.objects.filter(pk=visitor.pk).update(lead=Lead.objects.create(public_identifier="linked"))
    assert legacy_rb2b.legacy_notices_possible()


def test_old_notice_drain_cannot_take_new_integrations_outbox(fake_session, monkeypatch):
    visitor = RB2BVisitor.objects.create(public_identifier="new", linkedin_url="https://www.linkedin.com/in/new/",
                                        linkedin_profile=fake_session.linkedin_profile)
    notice = RB2BNotification.objects.create(visitor=visitor, key="event:synthetic", kind="sent", detail="New callback")
    monkeypatch.setattr("linkedin.notifications.slack._slack_api", lambda *a, **kw: pytest.fail("New outbox stolen"))
    assert legacy_rb2b.flush_notifications() == 0
    notice.refresh_from_db()
    assert notice.sent_at is None and notice.attempts == 0


def _replying_lead(monkeypatch, *, receiver, legacy, member=True, urn_in_profile=True):
    """A Pending existing-campaign Deal whose lead replies on `receiver`'s listener."""
    from types import SimpleNamespace
    from unittest.mock import Mock
    from django.contrib.auth.models import User
    from crm.models import Deal
    from linkedin.enums import ProfileState
    from linkedin.models import Campaign, LinkedInProfile
    from linkedin.realtime import handler
    username = {"Chuka": "chukyjack", "Arian": "ariantajbakh"}[receiver]
    owner = User.objects.create(username=username)
    profile = LinkedInProfile.objects.create(user=owner, linkedin_username=username, linkedin_password="x", active=True)
    campaign = Campaign.objects.create(name=f"Existing {receiver} campaign", user=owner, status="active")
    urn = "urn:li:fsd_profile:ReplyReview"
    lead = Lead.objects.create(first_name="Ruth", last_name="Reply", public_identifier="ruth-reply",
                               linkedin_url="https://www.linkedin.com/in/ruth-reply/",
                               description=f'{{"urn": "{urn}"}}' if urn_in_profile else "")
    deal = Deal.objects.create(lead=lead, campaign=campaign, state=ProfileState.PENDING)
    if legacy:
        if receiver != "Arian":
            arian = User.objects.create(username="ariantajbakh")
            profile = LinkedInProfile.objects.create(user=arian, linkedin_username="ariantajbakh",
                                                     linkedin_password="x", active=True)
        RB2BVisitor.objects.create(public_identifier="ruth-reply", linkedin_url=lead.linkedin_url,
                                   linkedin_profile=profile, lead=lead, member_urn=urn if member else "",
                                   status="held")
    notice = Mock(return_value=True)
    monkeypatch.setattr(handler, "notify_message_received", notice)
    monkeypatch.setattr("linkedin.notifications.slack.notify_message_received", notice)
    monkeypatch.setattr(handler, "_maybe_enqueue_enrichment", Mock())
    parsed = SimpleNamespace(entity_urn="urn:li:msg:reply-review", conversation_urn=f"urn:li:conv:{receiver}",
                             sender_name="Ruth Reply", sender_member_urn=urn, text="Please stop",
                             timestamp=timezone.now().strftime("%Y-%m-%d %H:%M"))
    monkeypatch.setattr(handler, "parse_realtime_event", lambda _event: parsed)
    return lead, deal, notice, parsed


@pytest.mark.parametrize("receiver", ["Chuka", "Arian"])
@pytest.mark.parametrize("legacy,member,urn_in_profile", [
    (False, True, True),   # ordinary contact: the unchanged control
    (True, True, True),
    (True, False, True),   # legacy row without a stored URN
    (True, True, False),   # RB2B-sourced lead found only through the visitor URN
])
def test_a_reply_is_saved_stopped_stamped_and_notified_once_as_its_receiver(
        monkeypatch, receiver, legacy, member, urn_in_profile):
    from crm.models import Deal
    from linkedin.enums import ProfileState
    from linkedin.realtime import handler
    from linkedin.tasks.stop_checks import automation_stop_reason
    lead, deal, notice, parsed = _replying_lead(monkeypatch, receiver=receiver, legacy=legacy, member=member,
                                                urn_in_profile=urn_in_profile)
    handler._handle({}, operator=receiver)
    handler._handle({}, operator=receiver)  # a duplicate event changes nothing
    deal.refresh_from_db()
    reply = Message.objects.get(lead=lead, direction="inbound")
    assert reply.raw["received_by"] == receiver
    assert automation_stop_reason(deal)
    assert deal.state == ProfileState.CONNECTED and deal.last_reply_at is not None
    assert [c.kwargs["operator"] for c in notice.call_args_list] == [receiver]
    assert [c.kwargs["thread_external_id"] for c in notice.call_args_list] == [parsed.conversation_urn]
    assert legacy_rb2b.owns_reply_notice(reply) is legacy  # legacy notices stay durable


@pytest.mark.parametrize("persist", ["receiver", "outbound_senders"])
def test_backfill_emits_one_notice_attributed_to_the_receiving_sender(monkeypatch, persist):
    from linkedin.db.messages import persist_thread
    from linkedin.management.commands.backfill_messages import _notify_new_inbound_messages
    lead, _, notice, event = _replying_lead(monkeypatch, receiver="Chuka", legacy=True)
    parsed = [{"entity_urn": event.entity_urn, "sender": event.sender_name, "text": event.text,
               "timestamp": event.timestamp}]
    attribution = {"receiver": "chukyjack"} if persist == "receiver" else {"outbound_senders": {"Chuka"}}
    persist_thread(lead=lead, parsed=parsed, thread_external_id=event.conversation_urn, **attribution)
    _notify_new_inbound_messages(lead=lead, parsed=parsed, existing_external_ids=set(), operator="Chuka")
    legacy_rb2b.flush_notifications()
    assert [c.kwargs["operator"] for c in notice.call_args_list] == ["Chuka"]


def test_backfill_without_a_provable_receiver_keeps_its_own_single_notice(monkeypatch):
    from linkedin.db.messages import persist_thread
    from linkedin.management.commands.backfill_messages import _notify_new_inbound_messages
    lead, _, notice, event = _replying_lead(monkeypatch, receiver="Chuka", legacy=True)
    parsed = [{"entity_urn": event.entity_urn, "sender": event.sender_name, "text": event.text,
               "timestamp": event.timestamp}]
    persist_thread(lead=lead, parsed=parsed, thread_external_id=event.conversation_urn)
    _notify_new_inbound_messages(lead=lead, parsed=parsed, existing_external_ids=set(), operator="Chuka")
    legacy_rb2b.flush_notifications()
    assert [c.kwargs["operator"] for c in notice.call_args_list] == ["Chuka"]
    assert not RB2BNotification.objects.exists()


def test_get_conversation_records_the_fetching_account_as_receiver(fake_session, monkeypatch):
    from linkedin.actions import conversations
    fake_session.linkedin_profile.linkedin_username = "chukyjack"
    lead = Lead.objects.create(first_name="Ruth", last_name="Reply", public_identifier="ruth-reply",
                               linkedin_url="https://www.linkedin.com/in/ruth-reply/")
    raw = [{"entity_urn": "urn:li:msg:fetched", "sender": "Ruth Reply", "text": "Hi", "timestamp": "2026-11-01 15:00"}]
    monkeypatch.setattr(fake_session, "ensure_browser", lambda: None, raising=False)
    monkeypatch.setattr(conversations, "PlaywrightLinkedinAPI", lambda **kw: object())
    monkeypatch.setattr("linkedin.db.leads.resolve_urn", lambda *a, **k: "urn:li:fsd_profile:x", raising=False)
    monkeypatch.setattr(conversations, "find_conversation_urn", lambda *a, **k: "urn:li:conv:fetched", raising=False)
    monkeypatch.setattr(conversations, "fetch_messages", lambda *a, **k: {})
    monkeypatch.setattr(conversations, "parse_messages", lambda payload: raw)
    persisted = []
    monkeypatch.setattr("linkedin.db.messages.persist_thread", lambda **kw: persisted.append(kw))
    conversations.get_conversation(fake_session, "ruth-reply")
    assert persisted and persisted[0]["receiver"] == "chukyjack"
