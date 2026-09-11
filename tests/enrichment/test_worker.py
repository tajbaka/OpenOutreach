"""Tests for the EnrichmentWorker.

The threaded loop is not exercised under the `db` fixture (a worker thread
would not see the test transaction). Logic is tested via `_run_once`, which
is pure DB + HTTP. The start/stop lifecycle is smoke-tested with `_run_once`
patched out so the thread never touches the DB.
"""
from unittest.mock import patch
from datetime import timedelta

import pytest
from django.utils import timezone

from linkedin.enrichment.base import EnrichmentResult, EnrichmentStatus
from linkedin.enrichment.worker import EnrichmentWorker
from linkedin.models import Task


def _enrich_task(status=Task.Status.PENDING, scheduled_offset_s=-1):
    from datetime import timedelta

    return Task.objects.create(
        task_type=Task.TaskType.ENRICH_PHONE,
        status=status,
        scheduled_at=timezone.now() + timedelta(seconds=scheduled_offset_s),
        payload={"lead_id": 1, "bettercontact_request_id": ""},
    )


def _email_task(status=Task.Status.PENDING, scheduled_offset_s=-1, *, operator="Arian"):
    from datetime import timedelta

    return Task.objects.create(
        task_type=Task.TaskType.ENRICH_EMAIL,
        status=status,
        scheduled_at=timezone.now() + timedelta(seconds=scheduled_offset_s),
        payload={"lead_id": 1, "operator": operator, "bettercontact_email_request_id": ""},
    )


@pytest.mark.django_db
def test_run_once_no_task_returns_false():
    assert EnrichmentWorker(operator="Arian")._run_once() is False


@pytest.mark.django_db
def test_run_once_found_marks_task_completed():
    task = _enrich_task()
    found = EnrichmentResult(
        status=EnrichmentStatus.FOUND, provider="leadmagic", phone="+1",
    )
    with patch("linkedin.enrichment.worker.handle_enrich_phone", return_value=found):
        handled = EnrichmentWorker(operator="Arian")._run_once()
    task.refresh_from_db()
    assert handled is True
    assert task.status == Task.Status.COMPLETED


@pytest.mark.django_db
def test_run_once_api_failure_marks_task_failed():
    task = _enrich_task()
    fail = EnrichmentResult(
        status=EnrichmentStatus.API_FAILURE,
        provider="prospeo",
        raw={"reason": "http_error", "status": 402},
    )
    with patch("linkedin.enrichment.worker.handle_enrich_phone", return_value=fail), \
         patch("linkedin.enrichment.worker._should_alert", return_value=True), \
         patch("linkedin.enrichment.worker.notify_degraded") as mock_degraded:
        EnrichmentWorker(operator="Arian")._run_once()
    task.refresh_from_db()
    assert task.status == Task.Status.FAILED
    assert "prospeo" in task.error
    assert '"status": 402' in task.error
    mock_degraded.assert_called_once()


@pytest.mark.django_db
def test_run_once_api_failure_respects_alert_cooldown():
    task = _enrich_task()
    fail = EnrichmentResult(
        status=EnrichmentStatus.API_FAILURE,
        provider="prospeo",
        raw={"reason": "http_error", "status": 429},
    )
    with patch("linkedin.enrichment.worker.handle_enrich_phone", return_value=fail), \
         patch("linkedin.enrichment.worker._should_alert", return_value=False), \
         patch("linkedin.enrichment.worker.notify_degraded") as mock_degraded:
        EnrichmentWorker(operator="Arian")._run_once()
    task.refresh_from_db()
    assert task.status == Task.Status.FAILED
    mock_degraded.assert_not_called()


@pytest.mark.django_db
def test_run_once_skip_result_none_marks_completed():
    task = _enrich_task()
    with patch("linkedin.enrichment.worker.handle_enrich_phone", return_value=None):
        EnrichmentWorker(operator="Arian")._run_once()
    task.refresh_from_db()
    assert task.status == Task.Status.COMPLETED


@pytest.mark.django_db
def test_run_once_dispatches_email_enrichment():
    task = _email_task()
    found = EnrichmentResult(
        status=EnrichmentStatus.FOUND,
        provider="bettercontact",
        email="ada@example.com",
    )
    with patch("linkedin.enrichment.worker.handle_enrich_email", return_value=found) as mock_email:
        handled = EnrichmentWorker(operator="Arian")._run_once()
    task.refresh_from_db()
    assert handled is True
    assert task.status == Task.Status.COMPLETED
    mock_email.assert_called_once_with(task)


@pytest.mark.django_db
def test_run_once_handler_exception_marks_failed_and_notifies():
    task = _enrich_task()
    with patch("linkedin.enrichment.worker.handle_enrich_phone",
               side_effect=RuntimeError("boom")), \
         patch("linkedin.enrichment.worker.notify_error") as mock_err:
        EnrichmentWorker(operator="Arian")._run_once()
    task.refresh_from_db()
    assert task.status == Task.Status.FAILED
    mock_err.assert_called_once()


@pytest.mark.django_db
def test_reclaim_stale_resets_running_enrich_tasks():
    task = _enrich_task(status=Task.Status.RUNNING)
    email = _email_task(status=Task.Status.RUNNING)
    Task.objects.filter(pk__in=[task.pk, email.pk]).update(
        started_at=timezone.now() - timedelta(hours=2),
    )
    EnrichmentWorker(operator="Arian")._reclaim_stale()
    task.refresh_from_db()
    email.refresh_from_db()
    assert task.status == Task.Status.PENDING
    assert email.status == Task.Status.PENDING


@pytest.mark.django_db
def test_start_stop_lifecycle_does_not_hang():
    worker = EnrichmentWorker(operator="Arian", poll_interval=0.01)
    with patch.object(EnrichmentWorker, "_run_once", return_value=False):
        worker.start()
        assert worker._thread is not None
        worker.stop()
        assert worker._thread is None


@pytest.mark.parametrize("operator", ["", None, "Eddy", "arian", "unknown"])
def test_worker_requires_exact_canonical_operator(operator):
    with pytest.raises(ValueError, match="canonical operator"):
        EnrichmentWorker(operator=operator)


@pytest.mark.django_db
@pytest.mark.parametrize("operator,other", [("Arian", "Chuka"), ("Chuka", "Arian")])
def test_email_claims_are_sender_scoped_and_stamp_started_at(operator, other):
    foreign = _email_task(operator=other, scheduled_offset_s=-10)
    own = _email_task(operator=operator)
    before = timezone.now()

    claimed = EnrichmentWorker(operator=operator)._claim_next()

    assert claimed.pk == own.pk
    own.refresh_from_db()
    foreign.refresh_from_db()
    assert own.status == Task.Status.RUNNING
    assert before <= own.started_at <= timezone.now()
    assert foreign.status == Task.Status.PENDING and foreign.started_at is None
    assert EnrichmentWorker(operator=operator)._claim_next() is None


@pytest.mark.django_db
@pytest.mark.parametrize("operator", ["Arian", "Chuka"])
def test_legacy_phone_queue_remains_shared_including_missing_operator(operator):
    phone = _enrich_task()
    assert "operator" not in phone.payload
    assert EnrichmentWorker(operator=operator)._claim_next().pk == phone.pk


@pytest.mark.django_db
def test_claim_never_pulls_future_or_running_work_forward():
    future = _email_task(scheduled_offset_s=600)
    running = _email_task(status=Task.Status.RUNNING)
    assert EnrichmentWorker(operator="Arian")._claim_next() is None
    future.refresh_from_db()
    running.refresh_from_db()
    assert future.status == Task.Status.PENDING
    assert running.status == Task.Status.RUNNING


@pytest.mark.django_db
@pytest.mark.parametrize("operator,other", [("Arian", "Chuka"), ("Chuka", "Arian")])
def test_startup_recovery_preserves_fresh_and_foreign_email_work(operator, other):
    now = timezone.now()
    stale = _email_task(status=Task.Status.RUNNING, operator=operator)
    stale.payload["bettercontact_email_request_id"] = "persisted-provider-request"
    stale.started_at = now - timedelta(hours=2)
    stale.save(update_fields=["payload", "started_at"])
    original_payload, original_due = stale.payload.copy(), stale.scheduled_at
    fresh = _email_task(status=Task.Status.RUNNING, operator=operator)
    fresh.started_at = now - timedelta(minutes=1)
    fresh.save(update_fields=["started_at"])
    foreign = _email_task(status=Task.Status.RUNNING, operator=other)
    foreign.started_at = now - timedelta(hours=2)
    foreign.save(update_fields=["started_at"])
    unknown_fresh = _email_task(status=Task.Status.RUNNING, operator=operator)

    EnrichmentWorker(operator=operator)._reclaim_stale()

    stale.refresh_from_db()
    assert stale.status == Task.Status.PENDING and stale.started_at is None
    assert stale.payload == original_payload and stale.scheduled_at == original_due
    for untouched in (fresh, foreign, unknown_fresh):
        untouched.refresh_from_db()
        assert untouched.status == Task.Status.RUNNING


@pytest.mark.django_db
def test_unknown_start_recovery_requires_old_task_age_and_keeps_identity():
    old = _email_task(status=Task.Status.RUNNING)
    Task.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(hours=2))
    fresh = _email_task(status=Task.Status.RUNNING)

    EnrichmentWorker(operator="Arian")._reclaim_stale()

    old.refresh_from_db()
    fresh.refresh_from_db()
    assert old.status == Task.Status.PENDING
    assert old.pk != fresh.pk and old.payload == fresh.payload
    assert fresh.status == Task.Status.RUNNING


@pytest.mark.django_db
def test_shared_phone_recovery_is_age_gated_too():
    old = _enrich_task(status=Task.Status.RUNNING)
    old.started_at = timezone.now() - timedelta(hours=2)
    old.save(update_fields=["started_at"])
    fresh = _enrich_task(status=Task.Status.RUNNING)
    fresh.started_at = timezone.now()
    fresh.save(update_fields=["started_at"])

    EnrichmentWorker(operator="Chuka")._reclaim_stale()

    old.refresh_from_db()
    fresh.refresh_from_db()
    assert old.status == Task.Status.PENDING and old.started_at is None
    assert fresh.status == Task.Status.RUNNING


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("task_kind,operators", [
    ("phone", ("Arian", "Chuka")),
    ("email", ("Arian", "Arian")),
    ("email", ("Chuka", "Chuka")),
])
def test_concurrent_workers_make_only_one_provider_call(monkeypatch, task_kind, operators):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from django.db import connections

    task = _enrich_task() if task_kind == "phone" else _email_task(operator=operators[0])
    gate = Barrier(2)
    calls = []

    def handler(claimed):
        calls.append(claimed.pk)
        assert claimed.status == Task.Status.RUNNING and claimed.started_at is not None
        return EnrichmentResult(status=EnrichmentStatus.NOT_FOUND, provider="fixture")

    monkeypatch.setattr("linkedin.enrichment.worker.handle_enrich_phone", handler)
    monkeypatch.setattr("linkedin.enrichment.worker.handle_enrich_email", handler)

    def run(operator):
        try:
            gate.wait(timeout=10)
            return EnrichmentWorker(operator=operator)._run_once()
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(run, operator) for operator in operators]
        outcomes = [future.result(timeout=20) for future in futures]

    task.refresh_from_db()
    assert sum(outcomes) == 1
    assert calls == [task.pk]
    assert task.status == Task.Status.COMPLETED


@pytest.mark.django_db(transaction=True)
def test_claim_skips_locked_earlier_row_without_blocking_another_due_task():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from django.db import connections, transaction

    first = _email_task(scheduled_offset_s=-10)
    second = _email_task(scheduled_offset_s=-1)
    locked, release = Event(), Event()

    def lock_first():
        try:
            with transaction.atomic():
                Task.objects.select_for_update().get(pk=first.pk)
                locked.set()
                assert release.wait(timeout=10)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as executor:
        holder = executor.submit(lock_first)
        try:
            assert locked.wait(timeout=10)
            claimed = EnrichmentWorker(operator="Arian")._claim_next()
            assert claimed.pk == second.pk
        finally:
            release.set()
        holder.result(timeout=10)

    first.refresh_from_db()
    assert first.status == Task.Status.PENDING
