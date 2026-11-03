"""Opted-in campaigns use the normal connect lane outside hours, never others."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from linkedin import daemon
from linkedin.models import Campaign, Task
from linkedin.tasks import connect

pytestmark = pytest.mark.django_db


@pytest.fixture
def schedule(monkeypatch):
    for name, value in {
        "ENABLE_ACTIVE_HOURS": True, "ENABLE_CONNECT": True,
        "ENABLE_PACING_CATCH_UP": False, "ACTIVE_START_HOUR": 9,
        "ACTIVE_END_HOUR": 17, "ACTIVE_TIMEZONE": "America/Toronto",
        "REST_DAYS": (5, 6),
    }.items():
        monkeypatch.setattr(daemon, name, value)
    monkeypatch.setattr(daemon, "_discovery_available_now", lambda *a, **kw: False)
    return datetime(2026, 10, 31, 22, tzinfo=ZoneInfo("America/Toronto"))


def pending(campaign, age=60, kind="connect"):
    return Task.objects.create(task_type=kind, scheduled_at=timezone.now() - timedelta(seconds=age),
                               payload={"campaign_id": campaign.pk, "operator": "Arian", "public_id": "synthetic-person"})


@pytest.mark.parametrize("day,hour", [(28, 3), (28, 22), (31, 12), (31, 22)])
def test_only_explicit_owned_active_campaign_connects_outside_hours(
    fake_session, monkeypatch, schedule, day, hour,
):
    profile = fake_session.linkedin_profile
    approved = fake_session.campaign
    ordinary = Campaign.objects.create(name="ordinary hours", user=profile.user)
    disabled = Campaign.objects.create(name="disabled", user=profile.user, status="disabled")
    foreign = Campaign.objects.create(name="foreign", user=User.objects.create(username="foreign"))
    Campaign.objects.filter(pk__in=[approved.pk, disabled.pk, foreign.pk]).update(allow_connections_outside_hours=True)
    now = schedule.replace(day=day, hour=hour)
    rows = [pending(c) for c in (ordinary, disabled, foreign)]
    expected = pending(approved, age=20)
    followup = pending(approved, age=100, kind="follow_up")
    dates = dict(Task.objects.values_list("pk", "scheduled_at"))
    scope = daemon._connection_campaign_ids_now(profile, now=now)
    assert scope == (approved.pk,)
    assert daemon.seconds_until_active(profile, now=now) == 0
    kinds = daemon._claimable_task_types_now(profile, now=now)
    assert kinds == {"connect"}
    queue = daemon._task_queue_for_connection_scope(scope)
    assert queue.seconds_to_next(task_types=kinds) == 0
    assert queue.claim_next(task_types=kinds).pk == expected.pk
    assert queue.seconds_to_next(task_types=kinds) is None
    assert queue.claim_next(task_types=kinds) is None
    assert Task.objects.filter(pk__in=[r.pk for r in rows] + [followup.pk], status="pending").count() == 4
    assert dict(Task.objects.values_list("pk", "scheduled_at")) == dates


@pytest.mark.parametrize("reason", ["empty", "foreign", "disabled", "connect_disabled"])
def test_no_eligible_exception_keeps_original_sleep(fake_session, monkeypatch, schedule, reason):
    c = fake_session.campaign
    if reason != "empty":
        c.allow_connections_outside_hours = True
        c.save(update_fields=["allow_connections_outside_hours"])
    if reason == "foreign":
        c.user = User.objects.create(username="other")
        c.save(update_fields=["user"])
    if reason == "disabled":
        c.status = "disabled"
        c.save(update_fields=["status"])
    if reason == "connect_disabled":
        monkeypatch.setattr(daemon, "ENABLE_CONNECT", False)
    assert daemon._connection_campaign_ids_now(fake_session.linkedin_profile, now=schedule) == ()
    assert daemon.seconds_until_active(fake_session.linkedin_profile, now=schedule) > 0
    assert daemon._claimable_task_types_now(fake_session.linkedin_profile, now=schedule) == set()


def test_exception_does_not_retime_future_task_or_enable_followups(fake_session, monkeypatch, schedule):
    c = fake_session.campaign
    c.allow_connections_outside_hours = True
    c.save(update_fields=["allow_connections_outside_hours"])
    future = pending(c, age=-600)
    pending(c, age=100, kind="follow_up")
    scope = daemon._connection_campaign_ids_now(fake_session.linkedin_profile, now=schedule)
    queue = daemon._task_queue_for_connection_scope(scope)
    kinds = daemon._claimable_task_types_now(fake_session.linkedin_profile, now=schedule)
    assert queue.claim_next(task_types=kinds) is None
    assert queue.seconds_to_next(task_types=kinds) == pytest.approx(600, abs=2)
    future.refresh_from_db()
    assert future.status == "pending"


def test_saved_toggle_updates_claim_and_wakeup_on_the_next_poll(fake_session, schedule):
    campaign = fake_session.campaign
    profile = fake_session.linkedin_profile
    task = pending(campaign)

    for enabled in (False, True, False):
        Campaign.objects.filter(pk=campaign.pk).update(allow_connections_outside_hours=enabled)
        scope = daemon._connection_campaign_ids_now(profile, now=schedule)
        kinds = daemon._claimable_task_types_now(profile, now=schedule)
        assert scope == ((campaign.pk,) if enabled else ())
        assert (Task.TaskType.CONNECT in kinds) is enabled
        queue = daemon._task_queue_for_connection_scope(scope)
        assert queue.seconds_to_next(task_types={Task.TaskType.CONNECT}) == (0 if enabled else None)

    task.refresh_from_db()
    assert task.status == Task.Status.PENDING


def test_normal_hours_keep_existing_followup_order(fake_session, monkeypatch, schedule):
    c = fake_session.campaign
    c.allow_connections_outside_hours = True
    c.save(update_fields=["allow_connections_outside_hours"])
    followup = pending(c, age=100, kind="follow_up")
    pending(c)
    now = schedule.replace(day=28, hour=12)
    scope = daemon._connection_campaign_ids_now(fake_session.linkedin_profile, now=now)
    assert scope is None
    kinds = daemon._claimable_task_types_now(fake_session.linkedin_profile, now=now)
    assert "follow_up" in kinds
    assert daemon._task_queue_for_connection_scope(scope).claim_next(task_types=kinds).pk == followup.pk


def test_existing_catch_up_keeps_its_original_scope(fake_session, monkeypatch, schedule):
    monkeypatch.setattr(daemon, "_catch_up_task_types", lambda *a, **kw: {"connect", "follow_up"})
    scope = daemon._connection_campaign_ids_now(fake_session.linkedin_profile, now=schedule)
    assert scope is None
    assert daemon._claimable_task_types_now(fake_session.linkedin_profile, now=schedule) == {"connect", "follow_up"}


def test_exception_coexists_with_discovery_without_widening_connect_scope(fake_session, monkeypatch, schedule):
    c = fake_session.campaign
    c.allow_connections_outside_hours = True
    c.save(update_fields=["allow_connections_outside_hours"])
    monkeypatch.setattr(daemon, "_discovery_available_now", lambda *a, **kw: True)
    assert daemon._claimable_task_types_now(fake_session.linkedin_profile, now=schedule) == {"connect", "discovery"}
    assert daemon._connection_campaign_ids_now(fake_session.linkedin_profile, now=schedule) == (c.pk,)


def test_shared_quota_still_blocks_opted_in_handler(fake_session, monkeypatch, schedule):
    c = fake_session.campaign
    monkeypatch.setattr(connect, "ENABLE_CONNECT", True)
    c.allow_connections_outside_hours = True
    c.save(update_fields=["allow_connections_outside_hours"])
    monkeypatch.setattr(connect, "strategy_for", lambda *a: None)
    monkeypatch.setattr(fake_session.linkedin_profile, "can_execute", lambda action: False)
    sent = []
    monkeypatch.setattr("linkedin.actions.connect.send_connection_request", lambda *a, **kw: sent.append(kw))
    task = pending(c)
    task.mark_running()
    connect.handle_connect(task, fake_session, {})
    assert sent == []
    assert Task.objects.filter(task_type="connect", status="pending", payload__campaign_id=c.pk).count() == 1


@pytest.mark.parametrize("now,seconds", [
    (datetime(2026, 10, 31, 22, tzinfo=ZoneInfo("America/Toronto")), 7200),
    (datetime(2026, 11, 1, 0, tzinfo=ZoneInfo("America/Toronto")), 25 * 3600),
])
def test_quota_retry_uses_local_midnight_including_dst(monkeypatch, now, seconds):
    monkeypatch.setattr(connect, "ACTIVE_TIMEZONE", "America/Toronto")
    monkeypatch.setattr(connect.timezone, "localtime", lambda **kw: now)
    monkeypatch.setattr(connect, "_seconds_until_next_active_start", lambda: 999999)
    assert connect._connection_limit_delay(Campaign(allow_connections_outside_hours=True)) == seconds
    assert connect._connection_limit_delay(Campaign()) == 999999
