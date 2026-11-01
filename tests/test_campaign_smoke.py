"""One synthetic visitor through normal enrollment, priority and sending logic.

Only LinkedIn/HTTP boundaries are faked. Run with smoke_campaign_priority.py,
which separately denies network, subprocesses, .env reads and PostgreSQL.
"""
from datetime import datetime, timedelta
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone

from crm.models import Deal, Message
from linkedin import campaign_callbacks, campaign_integrations, conf, daemon
from linkedin.enums import ProfileState
from linkedin.models import Campaign, CampaignSubmission, OutboundDelivery, Task
from linkedin.tasks.connect import ConnectStrategy, enqueue_follow_up, handle_connect
from linkedin.tasks.follow_up import handle_follow_up
from tests.tasks.test_message_program_runtime import AUDIENCE_KEY, _version


@pytest.mark.django_db
def test_fake_visitor_priority_invitation_callback_retry_and_sequence(fake_session, monkeypatch, record_property):
    fake_session.django_user.username = "ariantajbakh"
    fake_session.django_user.save(update_fields=["username"])
    fake_session.linkedin_profile.linkedin_username = "ariantajbakh"
    fake_session.linkedin_profile.save(update_fields=["linkedin_username"])
    high = fake_session.campaign
    high.connection_priority = 10
    high.save(update_fields=["connection_priority"])
    version = _version(high)
    low = Campaign.objects.create(name="Synthetic ordinary campaign", user=fake_session.django_user)
    old = Task.objects.create(task_type="connect", scheduled_at=timezone.now() - timedelta(minutes=5),
                               payload={"campaign_id": low.pk})
    old_date = old.scheduled_at
    monkeypatch.setattr(conf, "ENABLE_CAMPAIGN_INTEGRATIONS", True)
    monkeypatch.setattr(conf, "ENABLE_FOLLOW_UP", True)
    monkeypatch.setattr(conf, "OUTREACH_CALLBACK_ALLOWED_HOSTS", "receiver.example")
    monkeypatch.setattr(conf, "OUTREACH_CALLBACK_SECRET", "synthetic-secret-" * 4)
    monkeypatch.setattr("linkedin.daemon.ENABLE_CONNECT", True)
    monkeypatch.setattr("linkedin.tasks.connect.ENABLE_CONNECT", True)
    body = {"source": "synthetic-smoke", "reference_id": "visitor:fake", "campaign_id": high.pk,
            "owner_username": "ariantajbakh", "message_version_id": version.pk,
            "linkedin_url": "https://www.linkedin.com/in/smoke-person/", "first_name": "Synthetic",
            "last_name": "Person", "company_name": "Synthetic Company", "icp": AUDIENCE_KEY,
            "callback_url": "https://receiver.example/completed"}
    result = campaign_integrations.submit(body, apply=True)
    assert campaign_integrations.submit(body, apply=True) == result
    assert CampaignSubmission.objects.count() == 1
    # A Saturday night exercises the same normal handler through the narrow
    # campaign-hours exception; the older ordinary campaign stays parked.
    monkeypatch.setattr(daemon, "ENABLE_ACTIVE_HOURS", True)
    monkeypatch.setattr(daemon, "ENABLE_PACING_CATCH_UP", False)
    monkeypatch.setattr(daemon, "REST_DAYS", (5, 6))
    monkeypatch.setattr(daemon, "CONNECT_OUTSIDE_HOURS_CAMPAIGN_IDS", (high.pk,))
    monkeypatch.setattr(daemon, "_discovery_available_now", lambda *a, **kw: False)
    after_hours = datetime(2026, 10, 31, 22, tzinfo=ZoneInfo("America/Toronto"))
    scope = daemon._connection_campaign_ids_now(fake_session.linkedin_profile, now=after_hours)
    kinds = daemon._claimable_task_types_now(fake_session.linkedin_profile, now=after_hours)
    assert scope == (high.pk,) and kinds == {"connect"}
    assert daemon.seconds_until_active(fake_session.linkedin_profile, now=after_hours) == 0
    task = daemon._task_queue_for_connection_scope(scope).claim_next(
        operator="Arian", campaign_ids=[low.pk, high.pk], task_types=kinds,
    )
    assert task.payload["campaign_id"] == high.pk
    old.refresh_from_db()
    assert old.status == "pending" and old.scheduled_at == old_date

    deal = Deal.objects.select_related("lead").get(pk=result["deal_id"])
    candidate = {"public_identifier": "smoke-person", "url": body["linkedin_url"], "lead_id": deal.lead_id,
                 "profile": {"first_name": "Synthetic", "last_name": "Person", "headline": "Founder",
                             "positions": [{"company_name": "Synthetic Company"}]}}
    strategy = ConnectStrategy(find_candidate=lambda session: candidate, pre_connect=None, delay=10,
                               action_fraction=1.0, qualifier=Mock(explain=lambda *a, **kw: ""))
    monkeypatch.setattr("linkedin.tasks.connect.strategy_for", lambda *a: strategy)
    monkeypatch.setattr("linkedin.actions.status.get_connection_status", lambda *a, **kw: ProfileState.QUALIFIED)
    invitation = Mock(return_value=ProfileState.PENDING)
    monkeypatch.setattr("linkedin.actions.connect.send_connection_request", invitation)
    handle_connect(task, fake_session, {high.pk: strategy.qualifier})
    invitation.assert_called_once()
    assert invitation.call_args.kwargs["note"] == "Hi Synthetic, frozen connect for Synthetic Company."
    assert campaign_integrations.submit(body, apply=True)["status"] == "sent"

    assert campaign_callbacks.reconcile(operator="Arian") == 1
    callback = Mock(side_effect=[False, True])  # fake Slack receiver fails, then recovers
    monkeypatch.setattr(campaign_callbacks, "post", callback)
    assert campaign_callbacks.deliver(operator="Arian") == 0
    row = CampaignSubmission.objects.get()
    CampaignSubmission.objects.filter(pk=row.pk).update(callback_next_at=timezone.now() - timedelta(seconds=1))
    assert campaign_callbacks.deliver(operator="Arian") == 1
    assert callback.call_args_list[0] == callback.call_args_list[1]  # stable final event
    invitation.assert_called_once()  # notification retry never sends another invite

    # Fake LinkedIn thread echo + acceptance exercise the normal sequence. The
    # real send action would persist this thread; its mocked boundary did not.
    Message.objects.create(lead=deal.lead, source="linkedin", direction="outbound", sender="Arian",
                           external_id="synthetic-smoke-invite", thread_external_id="synthetic-thread",
                           body=invitation.call_args.kwargs["note"], sent_at=timezone.now() - timedelta(days=1))
    Deal.objects.filter(pk=deal.pk).update(state=ProfileState.CONNECTED, connected_at=timezone.now() - timedelta(days=1))
    followup = enqueue_follow_up(high.pk, "smoke-person", operator="Arian", icp=AUDIENCE_KEY, delay_seconds=0)
    message = Mock(return_value=True)
    monkeypatch.setattr("linkedin.actions.message.send_raw_message", message)
    monkeypatch.setattr("linkedin.actions.status.get_connection_status", lambda *a, **kw: ProfileState.CONNECTED)
    handle_follow_up(followup, fake_session, {})
    message.assert_called_once()
    delivery = OutboundDelivery.objects.get(pk=followup.payload["delivery_id"])
    assert delivery.status == OutboundDelivery.Status.SENT
    assert delivery.enrollment.message_version_id == version.pk
    assert delivery.frozen_body == "Frozen first for Synthetic at Synthetic Company."
    old.refresh_from_db()
    assert old.status == "pending" and old.scheduled_at == old_date
    for key, value in {"simulated_invitations": invitation.call_count,
                       "simulated_followups": message.call_count,
                       "simulated_callback_attempts": callback.call_count,
                       "real_outreach_sends": 0}.items():
        record_property(key, value)
