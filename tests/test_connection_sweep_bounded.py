from datetime import date, datetime, timedelta, timezone as dt_timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from linkedin.actions.connections import (
    ConnectionEntry,
    ConnectionScrapeResult,
    _extract_visible_cards,
    _scan_connections_page,
)
from linkedin.db.deals import set_profile_state
from linkedin.db.leads import create_enriched_lead, promote_lead_to_deal
from linkedin.enums import ProfileState
from linkedin.models import Task, WorkflowRun
from linkedin.operators import resolve_operator
from linkedin.tasks.sweep_connections import (
    SweepReconciliationResult,
    _incremental_cutoff,
    _load_pending_candidates,
    _matched_deal_queryset,
    _pending_candidate_queryset,
    _record_sweep_run,
    handle_sweep_connections,
    reconcile_pending_connections,
)


SAMPLE_PROFILE = {
    "first_name": "Alice",
    "last_name": "Smith",
    "headline": "Engineer",
    "positions": [{"company_name": "Acme"}],
}


def _session_with_page():
    return SimpleNamespace(page=MagicMock())


def _entry(public_id: str, connected_on: date) -> ConnectionEntry:
    return ConnectionEntry(
        public_id=public_id,
        name=public_id.title(),
        connected_on=connected_on,
    )


def _scrape(
    entries,
    *,
    stop_reason="cutoff",
    elapsed_seconds=1.0,
) -> ConnectionScrapeResult:
    dates = [entry.connected_on for entry in entries if entry.connected_on]
    return ConnectionScrapeResult(
        entries=list(entries),
        rounds=max(len(entries), 1),
        cards_inspected=len(entries),
        elapsed_seconds=elapsed_seconds,
        stop_reason=stop_reason,
        oldest_connected_on=min(dates) if dates else None,
    )


def _make_pending(fake_session, public_id="alice"):
    create_enriched_lead(
        fake_session,
        f"https://www.linkedin.com/in/{public_id}/",
        SAMPLE_PROFILE,
    )
    promote_lead_to_deal(fake_session, public_id)
    set_profile_state(fake_session, public_id, ProfileState.PENDING.value)


def test_extract_visible_cards_uses_one_browser_side_batch():
    session = _session_with_page()
    locator = session.page.locator.return_value
    locator.evaluate_all.return_value = [
        {
            "connected_text": "Connected on July 29, 2026",
            "href": "https://www.linkedin.com/in/alice/",
            "name": "Alice Smith",
        },
        {
            "connected_text": "Connected on Jul 28, 2026",
            "href": "/in/bob/",
            "name": "Bob Jones",
        },
    ]

    entries = _extract_visible_cards(session)

    assert [entry.public_id for entry in entries] == ["alice", "bob"]
    assert [entry.connected_on for entry in entries] == [
        date(2026, 7, 29),
        date(2026, 7, 28),
    ]
    locator.evaluate_all.assert_called_once()


def test_virtualized_batches_progress_by_new_profile_ids_until_cutoff():
    session = _session_with_page()
    batches = [
        [_entry("alice", date(2026, 7, 29))],
        [_entry("bob", date(2026, 7, 28))],
        [_entry("carol", date(2026, 7, 26))],
    ]

    with (
        patch(
            "linkedin.actions.connections._extract_visible_cards",
            side_effect=batches,
        ),
        patch("linkedin.actions.connections._scroll_one_step") as scroll,
    ):
        result = _scan_connections_page(
            session,
            stop_before=date(2026, 7, 27),
            max_seconds=30,
            max_rounds=10,
            pause_ms=0,
        )

    assert result.stop_reason == "cutoff"
    assert result.complete
    assert result.rounds == 3
    assert {entry.public_id for entry in result.entries} == {
        "alice",
        "bob",
        "carol",
    }
    assert scroll.call_count == 2


def test_scan_stops_at_round_budget_even_when_list_keeps_growing():
    session = _session_with_page()
    counter = iter(range(100))

    def next_batch(_session):
        index = next(counter)
        return [_entry(f"lead-{index}", date(2026, 7, 29))]

    with (
        patch(
            "linkedin.actions.connections._extract_visible_cards",
            side_effect=next_batch,
        ),
        patch("linkedin.actions.connections._scroll_one_step"),
    ):
        result = _scan_connections_page(
            session,
            stop_before=date(2026, 7, 1),
            max_seconds=30,
            max_rounds=4,
            pause_ms=0,
        )

    assert result.stop_reason == "max_rounds"
    assert not result.complete
    assert result.rounds == 4
    assert result.cards_inspected == 4


def test_scan_stops_before_browser_work_when_runtime_budget_is_zero():
    session = _session_with_page()

    with patch("linkedin.actions.connections._extract_visible_cards") as extract:
        result = _scan_connections_page(
            session,
            stop_before=date(2026, 7, 1),
            max_seconds=0,
            max_rounds=100,
        )

    assert result.stop_reason == "max_seconds"
    assert result.rounds == 0
    extract.assert_not_called()


def test_empty_connections_surface_is_incomplete_not_a_watermark():
    session = _session_with_page()

    with (
        patch(
            "linkedin.actions.connections._extract_visible_cards",
            return_value=[],
        ),
        patch("linkedin.actions.connections._scroll_one_step"),
    ):
        result = _scan_connections_page(
            session,
            stop_before=date(2026, 7, 1),
            max_seconds=30,
            max_rounds=10,
            pause_ms=0,
        )

    assert result.stop_reason == "empty"
    assert not result.complete


@pytest.mark.django_db
def test_pending_candidate_query_excludes_large_related_fields(fake_session):
    _make_pending(fake_session)

    sql = str(_pending_candidate_queryset(fake_session).query)

    assert '"linkedin_campaign"."model_blob"' not in sql
    assert '"linkedin_campaign"."product_docs"' not in sql
    assert '"linkedin_campaign"."campaign_objective"' not in sql
    assert '"crm_lead"."embedding"' not in sql
    assert '"crm_lead"."description"' not in sql


@pytest.mark.django_db
def test_pending_candidate_loader_keeps_only_reconciliation_ledger(fake_session):
    from crm.models import Deal

    _make_pending(fake_session)
    deal = Deal.objects.get(campaign=fake_session.campaign)

    candidates = _load_pending_candidates(fake_session)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.deal_id == deal.pk
    assert candidate.campaign_id == fake_session.campaign.pk
    assert candidate.linkedin_url == "https://www.linkedin.com/in/alice/"
    assert candidate.invitation_sent_at == deal.invitation_sent_at
    assert candidate.update_date == deal.update_date
    assert candidate.public_id == "alice"


@pytest.mark.django_db
def test_matched_deal_query_defers_large_related_fields(fake_session):
    from crm.models import Deal

    _make_pending(fake_session)
    deal = Deal.objects.get(campaign=fake_session.campaign)

    sql = str(_matched_deal_queryset([deal.pk]).query)

    assert '"linkedin_campaign"."model_blob"' not in sql
    assert '"linkedin_campaign"."product_docs"' not in sql
    assert '"linkedin_campaign"."campaign_objective"' not in sql
    assert '"linkedin_campaign"."seed_public_ids"' not in sql
    assert '"crm_lead"."embedding"' not in sql


@pytest.mark.django_db
def test_reconciliation_hydrates_only_linkedin_matches(fake_session):
    from crm.models import Deal

    _make_pending(fake_session, "alice")
    _make_pending(fake_session, "bob")
    alice = Deal.objects.get(
        campaign=fake_session.campaign,
        lead__public_identifier="alice",
    )

    with (
        patch(
            "linkedin.tasks.sweep_connections.scrape_connections_with_stats",
            return_value=_scrape([_entry("alice", date(2026, 7, 29))]),
        ),
        patch("linkedin.tasks.sweep_connections._recycle_database_connection"),
        patch(
            "linkedin.tasks.sweep_connections._matched_deal_queryset",
            wraps=_matched_deal_queryset,
        ) as hydrate,
        patch(
            "linkedin.tasks.sweep_connections.process_accepted_deal",
        ) as accept,
    ):
        result = reconcile_pending_connections(fake_session)

    hydrate.assert_called_once_with([alice.pk])
    accept.assert_called_once()
    assert accept.call_args.args[1].pk == alice.pk
    assert result.pending_count == 2
    assert result.matched_count == 1


@pytest.mark.django_db
def test_old_invitation_accepted_today_uses_incremental_watermark(
    fake_session,
):
    from crm.models import Deal

    _make_pending(fake_session)
    Deal.objects.filter(campaign=fake_session.campaign).update(
        update_date=timezone.now() - timedelta(days=90),
    )
    operator = resolve_operator(fake_session.linkedin_profile.linkedin_username)
    anchor = datetime(2026, 7, 29, 15, 0, tzinfo=dt_timezone.utc)
    run = WorkflowRun.objects.create(
        name="connection-sweep",
        operator=operator,
        summary="",
        counts={},
    )
    WorkflowRun.objects.filter(pk=run.pk).update(completed_at=anchor)
    accepted_today = _entry("alice", date(2026, 7, 29))

    events = []
    with (
        patch(
            "linkedin.tasks.sweep_connections.scrape_connections_with_stats",
            return_value=_scrape([accepted_today]),
        ) as scrape,
        patch(
            "linkedin.tasks.sweep_connections._recycle_database_connection",
            side_effect=lambda: events.append("recycle"),
        ),
        patch(
            "linkedin.tasks.sweep_connections.process_accepted_deal",
            side_effect=lambda *args, **kwargs: events.append("accept"),
        ) as accept,
    ):
        result = reconcile_pending_connections(fake_session)

    assert result.cutoff_date == date(2026, 7, 28)
    assert result.matched_count == 1
    assert events == ["recycle", "accept"]
    scrape.assert_called_once_with(
        fake_session,
        stop_before=date(2026, 7, 28),
        max_seconds=180,
        max_rounds=120,
    )
    accept.assert_called_once()


@pytest.mark.django_db
def test_acceptance_processing_yields_after_total_sweep_budget(fake_session):
    _make_pending(fake_session, "alice")
    _make_pending(fake_session, "bob")
    entries = [
        _entry("alice", date(2026, 7, 29)),
        _entry("bob", date(2026, 7, 29)),
    ]

    with (
        patch(
            "linkedin.tasks.sweep_connections.scrape_connections_with_stats",
            return_value=_scrape(entries),
        ),
        patch("linkedin.tasks.sweep_connections._recycle_database_connection"),
        patch(
            "linkedin.tasks.sweep_connections.process_accepted_deal",
        ) as accept,
        patch(
            "linkedin.tasks.sweep_connections.time.monotonic",
            side_effect=[0, 181, 182],
        ),
    ):
        result = reconcile_pending_connections(fake_session)

    assert result.matched_count == 1
    assert result.scrape.stop_reason == "max_seconds_processing"
    assert not result.complete
    accept.assert_called_once()


@pytest.mark.django_db
def test_incomplete_sweep_preserves_cutoff_for_retry(fake_session):
    operator = resolve_operator(fake_session.linkedin_profile.linkedin_username)
    cutoff = date(2026, 7, 20)
    task = Task.objects.create(
        task_type=Task.TaskType.SWEEP_CONNECTIONS,
        status=Task.Status.RUNNING,
        scheduled_at=timezone.now(),
        started_at=timezone.now(),
        payload={"operator": operator},
    )
    result = SweepReconciliationResult(
        pending_count=10,
        matched_count=0,
        cutoff_date=cutoff,
        scrape=_scrape([], stop_reason="max_seconds", elapsed_seconds=180),
    )

    _record_sweep_run(task=task, operator=operator, result=result)

    run = WorkflowRun.objects.get(name="connection-sweep-incomplete")
    assert run.counts["cutoff_date"] == cutoff.isoformat()
    assert _incremental_cutoff(operator) == cutoff


@pytest.mark.django_db
def test_first_instrumented_sweep_bootstraps_from_legacy_completion(
    fake_session,
):
    operator = resolve_operator(fake_session.linkedin_profile.linkedin_username)
    anchor = datetime(2026, 7, 29, 15, 0, tzinfo=dt_timezone.utc)
    Task.objects.create(
        task_type=Task.TaskType.SWEEP_CONNECTIONS,
        status=Task.Status.COMPLETED,
        scheduled_at=anchor - timedelta(minutes=5),
        started_at=anchor - timedelta(minutes=2),
        completed_at=anchor,
        payload={"operator": operator},
    )

    assert _incremental_cutoff(operator) == date(2026, 7, 28)


@pytest.mark.django_db
def test_first_sweep_without_history_uses_exact_initial_lookback(fake_session):
    operator = resolve_operator(fake_session.linkedin_profile.linkedin_username)
    now = datetime(2026, 7, 29, 15, 0, tzinfo=dt_timezone.utc)

    with patch("linkedin.tasks.sweep_connections.timezone.now", return_value=now):
        cutoff = _incremental_cutoff(operator)

    assert cutoff == date(2026, 7, 22)


@pytest.mark.django_db
def test_later_success_supersedes_older_incomplete_cutoff(fake_session):
    operator = resolve_operator(fake_session.linkedin_profile.linkedin_username)
    incomplete = WorkflowRun.objects.create(
        name="connection-sweep-incomplete",
        operator=operator,
        summary="",
        counts={"cutoff_date": "2026-06-01"},
    )
    WorkflowRun.objects.filter(pk=incomplete.pk).update(
        completed_at=datetime(2026, 7, 20, tzinfo=dt_timezone.utc),
    )
    success = WorkflowRun.objects.create(
        name="connection-sweep",
        operator=operator,
        summary="",
        counts={},
    )
    WorkflowRun.objects.filter(pk=success.pk).update(
        completed_at=datetime(2026, 7, 29, 15, 0, tzinfo=dt_timezone.utc),
    )

    assert _incremental_cutoff(operator) == date(2026, 7, 28)


@pytest.mark.django_db
def test_incomplete_sweep_retries_quickly(fake_session):
    operator = resolve_operator(fake_session.linkedin_profile.linkedin_username)
    task = Task.objects.create(
        task_type=Task.TaskType.SWEEP_CONNECTIONS,
        status=Task.Status.RUNNING,
        scheduled_at=timezone.now(),
        started_at=timezone.now(),
        payload={"operator": operator},
    )
    result = SweepReconciliationResult(
        pending_count=10,
        matched_count=0,
        cutoff_date=date(2026, 7, 28),
        scrape=_scrape([], stop_reason="max_rounds"),
    )

    with (
        patch(
            "linkedin.tasks.sweep_connections.reconcile_pending_connections",
            return_value=result,
        ),
        patch("linkedin.tasks.sweep_connections._record_sweep_run"),
        patch("linkedin.tasks.sweep_connections.enqueue_sweep_connections") as enqueue,
    ):
        handle_sweep_connections(task, fake_session, {})

    enqueue.assert_called_once_with(operator=operator, delay_seconds=10 * 60)
