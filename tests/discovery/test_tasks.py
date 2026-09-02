import json
from datetime import timedelta

import pytest
from django.utils import timezone

from linkedin import conf, icp_outbound
from linkedin.exceptions import DiscoveryScreeningError
from linkedin.discovery.collector import (
    _hard_stop_before_visit,
    _mynetwork_card_budget,
    _scan_stop_reason,
    _screen_new_cards,
    enqueue_discovery,
    reconcile_discovery_tasks,
    fresh_discovery_payload,
    handle_discovery,
    save_discovery_profile,
)
from linkedin.discovery.screening import DiscoveryScreenDecision
from linkedin.discovery.sources.base import DiscoveryCard
from linkedin.discovery.sources.recommendation_common import (
    RecommendationSourceResult,
)
from linkedin.models import LinkedInDiscoveryLead, Task


def _configure(monkeypatch, tmp_path):
    monkeypatch.setattr(conf, "ENABLE_PROFILE_DISCOVERY", True)
    monkeypatch.setattr(conf, "ENABLE_ACTIVE_HOURS", True)
    monkeypatch.setattr(conf, "LLM_API_KEY", "test-key")
    monkeypatch.setattr(conf, "AI_MODEL", "test-model")
    monkeypatch.setattr(conf, "ACTIVE_TIMEZONE", "UTC")
    monkeypatch.setattr(conf, "ACTIVE_END_HOUR", 17)
    monkeypatch.setattr(conf, "DISCOVERY_DAILY_LIMIT", 25)
    monkeypatch.setattr(conf, "DISCOVERY_MAX_CARDS_PER_RUN", 200)
    monkeypatch.setattr(conf, "DISCOVERY_MAX_SECTIONS_PER_RUN", 12)
    monkeypatch.setattr(conf, "DISCOVERY_MAX_SCROLL_ROUNDS_PER_RUN", 12)
    monkeypatch.setattr(conf, "DISCOVERY_MAX_CONSECUTIVE_EMPTY_SCROLLS", 3)
    monkeypatch.setattr(
        conf,
        "DISCOVERY_MAX_PROFILE_RECOMMENDATIONS_PER_VISIT",
        20,
    )
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
                        },
                    },
                },
            },
        ),
    )
    monkeypatch.setattr(icp_outbound, "_MESSAGES_PATH", path)
    monkeypatch.setattr(
        "linkedin.discovery.collector.discovery_gate_open",
        lambda profile, now=None: True,
    )
    monkeypatch.setattr(
        "linkedin.discovery.collector.discovery_day_end",
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


def _network_result(*cards):
    return RecommendationSourceResult(
        cards=tuple(cards),
        sections_scanned=1,
        scroll_rounds=1,
        consecutive_empty_scrolls=1,
        stop_reason="source_exhausted",
        section_headings=("Suggestions for you",),
    )


def _patch_empty_profile_recommendations(monkeypatch):
    monkeypatch.setattr(
        "linkedin.discovery.collector.collect_profile_recommendations",
        lambda *args, **kwargs: RecommendationSourceResult(cards=()),
    )


@pytest.mark.django_db
def test_discovery_task_scans_visits_and_saves(
    fake_session,
    monkeypatch,
    tmp_path,
):
    from crm.models import Deal, Lead, Message

    _configure(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "linkedin.discovery.collector.collect_mynetwork_recommendations",
        lambda *args, **kwargs: _network_result(
            _card(),
            _card("queued-discovery"),
        ),
    )
    monkeypatch.setattr(
        "linkedin.discovery.collector.screen_cards",
        lambda cards, targets: {
            card.public_identifier: DiscoveryScreenDecision(
                public_identifier=card.public_identifier,
                should_visit=True,
                potential_icp="CSPs",
            )
            for card in cards
        },
    )
    monkeypatch.setattr("linkedin.discovery.collector.search_profile", lambda *a, **k: None)
    monkeypatch.setattr("linkedin.discovery.collector.PlaywrightLinkedinAPI", _API)
    _patch_empty_profile_recommendations(monkeypatch)
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
    continuation = Task.objects.get(
        task_type=Task.TaskType.DISCOVERY,
        status=Task.Status.PENDING,
    )
    assert row.stored_by_operator == "testuser@example.com"
    assert row.potential_icp == "CSPs"
    assert task.payload["cards_scanned"] == 2
    assert task.payload["sections_scanned"] == 1
    assert task.payload["scroll_rounds"] == 1
    assert task.payload["profile_visits"] == 1
    assert continuation.payload["cards_scanned"] == 2
    assert continuation.payload["sections_scanned"] == 1
    assert continuation.payload["scroll_rounds"] == 1
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
    monkeypatch.setattr(conf, "DISCOVERY_DAILY_LIMIT", 1)
    monkeypatch.setattr(
        "linkedin.discovery.collector.collect_mynetwork_recommendations",
        lambda *args, **kwargs: _network_result(_card("daily-limit-profile")),
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
    _patch_empty_profile_recommendations(monkeypatch)
    next_day = timezone.now() + timedelta(days=1)
    monkeypatch.setattr(
        "linkedin.discovery.collector.next_discovery_day_start",
        lambda now=None: next_day,
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
        "linkedin.discovery.collector.collect_mynetwork_recommendations",
        lambda *args, **kwargs: _network_result(*cards),
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
        "linkedin.discovery.collector.next_discovery_day_start",
        lambda now=None: timezone.now() + timedelta(days=1),
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
            payload={"source": "mynetwork_recommendations", "section_cursor": 0},
        )


@pytest.mark.django_db
def test_startup_reconciliation_keeps_one_sender_task(
    fake_session,
    monkeypatch,
    tmp_path,
):
    _configure(monkeypatch, tmp_path)
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
def test_startup_reconciliation_resets_search_era_payload(
    fake_session,
    monkeypatch,
    tmp_path,
):
    _configure(monkeypatch, tmp_path)
    task = Task.objects.create(
        task_type=Task.TaskType.DISCOVERY,
        scheduled_at=timezone.now(),
        payload=fresh_discovery_payload("testuser@example.com"),
    )
    Task.objects.filter(pk=task.pk).update(
        payload={
            "operator": "testuser@example.com",
            "source": "people_search",
            "query_index": 2,
            "page": 4,
        },
    )

    assert reconcile_discovery_tasks(
        fake_session.linkedin_profile,
        "testuser@example.com",
    )

    task.refresh_from_db()
    assert task.payload["source"] == "mynetwork_recommendations"
    assert task.payload["section_cursor"] == 0
    assert "query_index" not in task.payload
    assert "page" not in task.payload


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
def test_disabled_sender_does_not_cancel_another_senders_discovery(
    fake_session,
    monkeypatch,
):
    monkeypatch.setattr(conf, "ENABLE_PROFILE_DISCOVERY", False)
    own = Task.objects.create(
        task_type=Task.TaskType.DISCOVERY,
        scheduled_at=timezone.now(),
        payload=fresh_discovery_payload("testuser@example.com"),
    )
    other = Task.objects.create(
        task_type=Task.TaskType.DISCOVERY,
        scheduled_at=timezone.now(),
        payload=fresh_discovery_payload("Athena"),
    )

    assert not reconcile_discovery_tasks(
        fake_session.linkedin_profile,
        "testuser@example.com",
    )

    own.refresh_from_db()
    other.refresh_from_db()
    assert own.status == Task.Status.COMPLETED
    assert other.status == Task.Status.PENDING


@pytest.mark.django_db
def test_enqueue_at_daily_limit_defers_until_next_day(
    fake_session,
    monkeypatch,
    tmp_path,
):
    _configure(monkeypatch, tmp_path)
    monkeypatch.setattr(conf, "DISCOVERY_DAILY_LIMIT", 1)
    save_discovery_profile(
        linkedin_profile=fake_session.linkedin_profile,
        operator="testuser@example.com",
        potential_icp="CSPs",
        profile=_api_profile("already-saved-today"),
    )
    next_day = timezone.now() + timedelta(days=1)
    monkeypatch.setattr(
        "linkedin.discovery.collector.next_discovery_day_start",
        lambda now=None: next_day,
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
        ("sections_scanned", 12, "section_limit_reached"),
        ("scroll_rounds", 12, "scroll_limit_reached"),
        (
            "consecutive_scrolls_without_new_cards",
            3,
            "empty_scroll_limit_reached",
        ),
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


@pytest.mark.django_db
def test_run_time_cap_stops_before_opening_another_profile(
    fake_session,
    monkeypatch,
    tmp_path,
):
    _configure(monkeypatch, tmp_path)
    payload = fresh_discovery_payload("testuser@example.com")
    payload["run_started_at"] = (
        timezone.now() - timedelta(minutes=121)
    ).isoformat()

    assert _hard_stop_before_visit(
        session=fake_session,
        operator="testuser@example.com",
        payload=payload,
        now=timezone.now(),
    ) == "run_time_limit_reached"


@pytest.mark.django_db
def test_duplicate_cards_are_screened_once_per_run(
    fake_session,
    monkeypatch,
    tmp_path,
):
    _configure(monkeypatch, tmp_path)
    duplicate = _card("same-recommendation")
    monkeypatch.setattr(
        "linkedin.discovery.collector.collect_mynetwork_recommendations",
        lambda *args, **kwargs: _network_result(duplicate, duplicate),
    )
    screened = []

    def _screen(cards, targets):
        screened.extend(card.public_identifier for card in cards)
        return {
            card.public_identifier: DiscoveryScreenDecision(
                public_identifier=card.public_identifier,
                should_visit=False,
                potential_icp="",
            )
            for card in cards
        }

    monkeypatch.setattr("linkedin.discovery.collector.screen_cards", _screen)
    monkeypatch.setattr(
        "linkedin.discovery.collector.next_discovery_day_start",
        lambda now=None: timezone.now() + timedelta(days=1),
    )
    task = Task.objects.create(
        task_type=Task.TaskType.DISCOVERY,
        status=Task.Status.RUNNING,
        scheduled_at=timezone.now(),
        payload=fresh_discovery_payload("testuser@example.com"),
    )

    handle_discovery(task, fake_session)

    assert screened == ["same-recommendation"]
    task.refresh_from_db()
    assert task.payload["cards_scanned"] == 1


@pytest.mark.django_db
def test_recommendation_screening_is_batched(
    fake_session,
    monkeypatch,
    tmp_path,
):
    _configure(monkeypatch, tmp_path)
    batch_sizes = []

    def _screen(cards, targets):
        batch_sizes.append(len(cards))
        return {
            card.public_identifier: DiscoveryScreenDecision(
                public_identifier=card.public_identifier,
                should_visit=False,
                potential_icp="",
            )
            for card in cards
        }

    monkeypatch.setattr("linkedin.discovery.collector.screen_cards", _screen)
    payload = fresh_discovery_payload("testuser@example.com")

    _screen_new_cards(
        cards=[_card(f"batch-{index}") for index in range(21)],
        payload=payload,
        targets=icp_outbound.load_discovery_targets("testuser@example.com"),
    )

    assert batch_sizes == [5, 5, 5, 5, 1]


@pytest.mark.django_db
def test_malformed_screening_batch_is_skipped_fail_closed(
    fake_session,
    monkeypatch,
    tmp_path,
):
    _configure(monkeypatch, tmp_path)

    def _screen(cards, targets):
        raise DiscoveryScreeningError(
            "Screening returned unknown profile 'not-in-this-batch'",
        )

    monkeypatch.setattr("linkedin.discovery.collector.screen_cards", _screen)
    payload = fresh_discovery_payload("testuser@example.com")

    matches = _screen_new_cards(
        cards=[_card(f"bad-batch-{index}") for index in range(5)],
        payload=payload,
        targets=icp_outbound.load_discovery_targets("testuser@example.com"),
    )

    assert matches == []
    assert payload["cards_scanned"] == 5
    assert payload["consecutive_no_matches"] == 5


def test_mynetwork_budget_reserves_capacity_for_profile_recommendations(
    monkeypatch,
    tmp_path,
):
    _configure(monkeypatch, tmp_path)
    monkeypatch.setattr(conf, "DISCOVERY_MAX_CARDS_PER_RUN", 40)
    monkeypatch.setattr(
        conf,
        "DISCOVERY_MAX_PROFILE_RECOMMENDATIONS_PER_VISIT",
        20,
    )

    assert _mynetwork_card_budget(
        fresh_discovery_payload("testuser@example.com"),
    ) == 30


@pytest.mark.django_db
def test_depth_zero_queue_stays_ahead_of_one_hop_recommendations(
    fake_session,
    monkeypatch,
    tmp_path,
):
    _configure(monkeypatch, tmp_path)
    first = _card("depth-zero-first")
    second = _card("depth-zero-second")
    related = DiscoveryCard(
        **{
            **_card("depth-one-related").to_payload(),
            "source_kind": "profile_recommendation",
            "source_profile_public_identifier": first.public_identifier,
            "recommendation_depth": 1,
        },
    )
    monkeypatch.setattr(
        "linkedin.discovery.collector.collect_mynetwork_recommendations",
        lambda *args, **kwargs: _network_result(first, second),
    )
    monkeypatch.setattr(
        "linkedin.discovery.collector.screen_cards",
        lambda cards, targets: {
            card.public_identifier: DiscoveryScreenDecision(
                public_identifier=card.public_identifier,
                should_visit=True,
                potential_icp="CSPs",
            )
            for card in cards
        },
    )
    monkeypatch.setattr(
        "linkedin.discovery.collector.search_profile",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr("linkedin.discovery.collector.PlaywrightLinkedinAPI", _API)
    monkeypatch.setattr(
        "linkedin.discovery.collector.collect_profile_recommendations",
        lambda *args, **kwargs: RecommendationSourceResult(
            cards=(related,),
            sections_scanned=1,
        ),
    )
    task = Task.objects.create(
        task_type=Task.TaskType.DISCOVERY,
        status=Task.Status.RUNNING,
        scheduled_at=timezone.now(),
        payload=fresh_discovery_payload("testuser@example.com"),
    )

    handle_discovery(task, fake_session)

    continuation = Task.objects.get(
        task_type=Task.TaskType.DISCOVERY,
        status=Task.Status.PENDING,
    )
    queued = continuation.payload["pending_cards"]
    assert [card["public_identifier"] for card in queued] == [
        "depth-zero-second",
        "depth-one-related",
    ]
    assert [card["recommendation_depth"] for card in queued] == [0, 1]


@pytest.mark.django_db
def test_depth_one_profile_does_not_expand_recommendations(
    fake_session,
    monkeypatch,
    tmp_path,
):
    _configure(monkeypatch, tmp_path)
    card = DiscoveryCard(
        **{
            **_card("depth-one").to_payload(),
            "potential_icp": "CSPs",
            "source_kind": "profile_recommendation",
            "source_profile_public_identifier": "seed",
            "recommendation_depth": 1,
        },
    )
    payload = fresh_discovery_payload("testuser@example.com")
    payload["source_complete"] = True
    payload["pending_cards"] = [card.to_payload()]
    payload["stop_after_pending"] = "recommendation_source_exhausted"
    task = Task.objects.create(
        task_type=Task.TaskType.DISCOVERY,
        status=Task.Status.RUNNING,
        scheduled_at=timezone.now(),
        payload=payload,
    )
    monkeypatch.setattr("linkedin.discovery.collector.search_profile", lambda *a, **k: None)
    monkeypatch.setattr("linkedin.discovery.collector.PlaywrightLinkedinAPI", _API)
    profile_source = pytest.fail
    monkeypatch.setattr(
        "linkedin.discovery.collector.collect_profile_recommendations",
        profile_source,
    )
    monkeypatch.setattr(
        "linkedin.discovery.collector.next_discovery_day_start",
        lambda now=None: timezone.now() + timedelta(days=1),
    )

    handle_discovery(task, fake_session)

    row = LinkedInDiscoveryLead.objects.get(public_identifier="depth-one")
    assert row.potential_icp == "CSPs"
