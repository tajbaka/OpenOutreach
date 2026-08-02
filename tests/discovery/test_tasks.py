import json
from datetime import timedelta

import pytest
from django.utils import timezone

from linkedin import conf, icp_outbound
from linkedin.discovery.collector import (
    _hard_stop_before_visit,
    _scan_stop_reason,
    enqueue_discovery,
    reconcile_discovery_tasks,
    fresh_discovery_payload,
    handle_discovery,
    save_discovery_profile,
)
from linkedin.discovery.screening import DiscoveryScreenDecision
from linkedin.discovery.sources.base import DiscoveryCard
from linkedin.models import LinkedInDiscoveryLead, Task


def _configure(monkeypatch, tmp_path):
    monkeypatch.setattr(conf, "ENABLE_PROFILE_DISCOVERY", True)
    monkeypatch.setattr(conf, "ENABLE_ACTIVE_HOURS", True)
    monkeypatch.setattr(conf, "LLM_API_KEY", "test-key")
    monkeypatch.setattr(conf, "AI_MODEL", "test-model")
    monkeypatch.setattr(conf, "ACTIVE_TIMEZONE", "UTC")
    monkeypatch.setattr(conf, "ACTIVE_END_HOUR", 17)
    monkeypatch.setattr(conf, "DISCOVERY_TIMEZONE", "UTC")
    monkeypatch.setattr(conf, "DISCOVERY_WEEKDAY_START_HOUR", 18)
    monkeypatch.setattr(conf, "DISCOVERY_WEEKDAY_END_HOUR", 21)
    monkeypatch.setattr(conf, "DISCOVERY_RUN_ON_REST_DAYS", True)
    monkeypatch.setattr(conf, "DISCOVERY_REST_DAY_START_HOUR", 11)
    monkeypatch.setattr(conf, "DISCOVERY_REST_DAY_END_HOUR", 16)
    monkeypatch.setattr(conf, "DISCOVERY_MAX_CARDS_PER_RUN", 200)
    monkeypatch.setattr(conf, "DISCOVERY_MAX_PAGES_PER_RUN", 10)
    monkeypatch.setattr(conf, "DISCOVERY_MAX_PROFILE_VISITS_PER_RUN", 40)
    monkeypatch.setattr(conf, "DISCOVERY_MAX_CONSECUTIVE_NO_MATCHES", 75)
    monkeypatch.setattr(conf, "DISCOVERY_MAX_RUN_MINUTES", 120)
    monkeypatch.setattr(conf, "DISCOVERY_PROFILE_DELAY_MIN_SECONDS", 20)
    monkeypatch.setattr(conf, "DISCOVERY_PROFILE_DELAY_MAX_SECONDS", 45)
    path = tmp_path / "icp_messages.json"
    path.write_text(
        json.dumps(
            {
                "testuser@example.com": {
                    "CSPs": {
                        "discovery": {
                            "enabled": True,
                            "profile": "Cloud security leaders",
                            "search_queries": ["FedRAMP CISO"],
                        },
                    },
                },
            },
        ),
    )
    monkeypatch.setattr(icp_outbound, "_MESSAGES_PATH", path)
    monkeypatch.setattr(
        "linkedin.discovery.collector.discovery_window_open",
        lambda now=None: True,
    )
    monkeypatch.setattr(
        "linkedin.discovery.collector.discovery_window_end",
        lambda now=None: timezone.now() + timedelta(hours=1),
    )


def _card(public_identifier="discovered-jane"):
    return DiscoveryCard(
        public_identifier=public_identifier,
        linkedin_url=f"https://www.linkedin.com/in/{public_identifier}/",
        name="Jane Doe",
        headline="VP Security",
        company_name="Example Cloud",
    )


def _api_profile(public_identifier="discovered-jane"):
    return {
        "public_identifier": public_identifier,
        "url": f"https://www.linkedin.com/in/{public_identifier}/",
        "urn": f"urn:li:fsd_profile:{public_identifier}",
        "first_name": "Jane",
        "last_name": "Doe",
        "full_name": "Jane Doe",
        "headline": "VP Security",
        "location_name": "Toronto",
        "positions": [{"company_name": "Example Cloud"}],
    }


class _API:
    def __init__(self, session):
        self.session = session

    def get_profile(self, public_identifier):
        return _api_profile(public_identifier), {"raw": True}


@pytest.mark.django_db
def test_discovery_task_scans_visits_and_saves(
    fake_session,
    monkeypatch,
    tmp_path,
):
    from crm.models import Deal, Lead, Message

    _configure(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "linkedin.discovery.collector.collect_people_search_cards",
        lambda *args, **kwargs: [_card()],
    )
    monkeypatch.setattr(
        "linkedin.discovery.collector.screen_cards",
        lambda cards, targets: {
            cards[0].public_identifier: DiscoveryScreenDecision(
                public_identifier=cards[0].public_identifier,
                should_visit=True,
                potential_icp="CSPs",
            ),
        },
    )
    monkeypatch.setattr("linkedin.discovery.collector.search_profile", lambda *a, **k: None)
    monkeypatch.setattr("linkedin.discovery.collector.PlaywrightLinkedinAPI", _API)
    task = Task.objects.create(
        task_type=Task.TaskType.DISCOVERY,
        status=Task.Status.RUNNING,
        scheduled_at=timezone.now(),
        payload=fresh_discovery_payload("testuser@example.com"),
    )
    crm_counts_before = (
        Lead.objects.count(),
        Deal.objects.count(),
        Message.objects.count(),
    )
    non_discovery_tasks_before = Task.objects.exclude(
        task_type=Task.TaskType.DISCOVERY,
    ).count()

    handle_discovery(task, fake_session)

    row = LinkedInDiscoveryLead.objects.get()
    task.refresh_from_db()
    assert row.stored_by_operator == "testuser@example.com"
    assert row.potential_icp == "CSPs"
    assert task.payload["cards_scanned"] == 1
    assert task.payload["profile_visits"] == 1
    assert Task.objects.filter(
        task_type=Task.TaskType.DISCOVERY,
        status=Task.Status.PENDING,
    ).count() == 1
    assert (
        Lead.objects.count(),
        Deal.objects.count(),
        Message.objects.count(),
    ) == crm_counts_before
    assert Task.objects.exclude(
        task_type=Task.TaskType.DISCOVERY,
    ).count() == non_discovery_tasks_before


@pytest.mark.django_db
def test_daily_limit_stops_and_schedules_next_day(
    fake_session,
    monkeypatch,
    tmp_path,
):
    _configure(monkeypatch, tmp_path)
    fake_session.linkedin_profile.discovery_daily_limit = 1
    fake_session.linkedin_profile.save(update_fields=["discovery_daily_limit"])
    monkeypatch.setattr(
        "linkedin.discovery.collector.collect_people_search_cards",
        lambda *args, **kwargs: [_card("daily-limit-profile")],
    )
    monkeypatch.setattr(
        "linkedin.discovery.collector.screen_cards",
        lambda cards, targets: {
            cards[0].public_identifier: DiscoveryScreenDecision(
                public_identifier=cards[0].public_identifier,
                should_visit=True,
                potential_icp="CSPs",
            ),
        },
    )
    monkeypatch.setattr("linkedin.discovery.collector.search_profile", lambda *a, **k: None)
    monkeypatch.setattr("linkedin.discovery.collector.PlaywrightLinkedinAPI", _API)
    next_day = timezone.now() + timedelta(days=1)
    monkeypatch.setattr(
        "linkedin.discovery.collector.next_discovery_window_start",
        lambda now=None, after_current_day=False: next_day,
    )
    task = Task.objects.create(
        task_type=Task.TaskType.DISCOVERY,
        status=Task.Status.RUNNING,
        scheduled_at=timezone.now(),
        payload=fresh_discovery_payload("testuser@example.com"),
    )

    handle_discovery(task, fake_session)

    task.refresh_from_db()
    assert task.payload["stop_reason"] == "daily_save_limit_reached"
    pending = Task.objects.get(
        task_type=Task.TaskType.DISCOVERY,
        status=Task.Status.PENDING,
    )
    assert pending.scheduled_at == next_day


@pytest.mark.django_db
def test_sparse_results_stop_at_no_match_cap(
    fake_session,
    monkeypatch,
    tmp_path,
):
    _configure(monkeypatch, tmp_path)
    monkeypatch.setattr(conf, "DISCOVERY_MAX_CONSECUTIVE_NO_MATCHES", 2)
    cards = [_card(f"reject-{index}") for index in range(3)]
    monkeypatch.setattr(
        "linkedin.discovery.collector.collect_people_search_cards",
        lambda *args, **kwargs: cards,
    )
    monkeypatch.setattr(
        "linkedin.discovery.collector.screen_cards",
        lambda cards, targets: {
            card.public_identifier: DiscoveryScreenDecision(
                public_identifier=card.public_identifier,
                should_visit=False,
                potential_icp="",
            )
            for card in cards
        },
    )
    monkeypatch.setattr(
        "linkedin.discovery.collector.next_discovery_window_start",
        lambda now=None, after_current_day=False: timezone.now() + timedelta(days=1),
    )
    task = Task.objects.create(
        task_type=Task.TaskType.DISCOVERY,
        status=Task.Status.RUNNING,
        scheduled_at=timezone.now(),
        payload=fresh_discovery_payload("testuser@example.com"),
    )

    handle_discovery(task, fake_session)

    task.refresh_from_db()
    assert task.payload["stop_reason"] == "consecutive_no_match_limit_reached"
    assert task.payload["profile_visits"] == 0
    assert LinkedInDiscoveryLead.objects.count() == 0


@pytest.mark.django_db
def test_discovery_task_requires_operator_payload():
    with pytest.raises(Exception, match="discovery tasks require"):
        Task.objects.create(
            task_type=Task.TaskType.DISCOVERY,
            scheduled_at=timezone.now(),
            payload={"query_index": 0, "page": 1},
        )


@pytest.mark.django_db
def test_startup_reconciliation_keeps_one_sender_task(
    fake_session,
    monkeypatch,
    tmp_path,
):
    _configure(monkeypatch, tmp_path)
    scheduled = timezone.now() + timedelta(hours=1)
    monkeypatch.setattr(
        "linkedin.discovery.collector.next_discovery_window_start",
        lambda now=None, after_current_day=False: scheduled,
    )

    assert reconcile_discovery_tasks(
        fake_session.linkedin_profile,
        "testuser@example.com",
    )
    assert reconcile_discovery_tasks(
        fake_session.linkedin_profile,
        "testuser@example.com",
    )

    assert Task.objects.filter(
        task_type=Task.TaskType.DISCOVERY,
        status=Task.Status.PENDING,
        payload__operator="testuser@example.com",
    ).count() == 1


@pytest.mark.django_db
def test_discovery_tasks_are_claimed_only_by_matching_operator():
    now = timezone.now()
    arian = Task.objects.create(
        task_type=Task.TaskType.DISCOVERY,
        scheduled_at=now,
        payload=fresh_discovery_payload("Arian"),
    )
    Task.objects.create(
        task_type=Task.TaskType.DISCOVERY,
        scheduled_at=now - timedelta(seconds=1),
        payload=fresh_discovery_payload("Chuka"),
    )

    claimed = Task.objects.claim_next(
        operator="Arian",
        campaign_ids=[],
        task_types={Task.TaskType.DISCOVERY},
    )

    assert claimed.pk == arian.pk


@pytest.mark.django_db
def test_enqueue_at_daily_limit_defers_until_next_day(
    fake_session,
    monkeypatch,
    tmp_path,
):
    _configure(monkeypatch, tmp_path)
    fake_session.linkedin_profile.discovery_daily_limit = 1
    fake_session.linkedin_profile.save(update_fields=["discovery_daily_limit"])
    save_discovery_profile(
        linkedin_profile=fake_session.linkedin_profile,
        operator="testuser@example.com",
        potential_icp="CSPs",
        profile=_api_profile("already-saved-today"),
    )
    next_day = timezone.now() + timedelta(days=1)
    monkeypatch.setattr(
        "linkedin.discovery.collector.next_discovery_window_start",
        lambda now=None, after_current_day=False: next_day,
    )

    assert enqueue_discovery(
        fake_session.linkedin_profile,
        "testuser@example.com",
    )
    task = Task.objects.get(task_type=Task.TaskType.DISCOVERY)
    assert task.scheduled_at == next_day


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("cards_scanned", 200, "card_limit_reached"),
        ("pages_scanned", 10, "page_limit_reached"),
        (
            "consecutive_no_matches",
            75,
            "consecutive_no_match_limit_reached",
        ),
    ],
)
def test_scan_caps_have_explicit_stop_reasons(
    fake_session,
    monkeypatch,
    tmp_path,
    field,
    value,
    reason,
):
    _configure(monkeypatch, tmp_path)
    payload = fresh_discovery_payload("testuser@example.com")
    payload[field] = value

    assert _scan_stop_reason(payload) == reason


@pytest.mark.django_db
def test_profile_visit_cap_stops_before_opening_another_profile(
    fake_session,
    monkeypatch,
    tmp_path,
):
    _configure(monkeypatch, tmp_path)
    payload = fresh_discovery_payload("testuser@example.com")
    payload["profile_visits"] = 40

    assert _hard_stop_before_visit(
        session=fake_session,
        operator="testuser@example.com",
        payload=payload,
        now=timezone.now(),
    ) == "profile_visit_limit_reached"
