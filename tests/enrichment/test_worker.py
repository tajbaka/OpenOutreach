"""Tests for the EnrichmentWorker.

The threaded loop is not exercised under the `db` fixture (a worker thread
would not see the test transaction). Logic is tested via `_run_once`, which
is pure DB + HTTP. The start/stop lifecycle is smoke-tested with `_run_once`
patched out so the thread never touches the DB.
"""
from unittest.mock import patch

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


def _email_task(status=Task.Status.PENDING, scheduled_offset_s=-1):
    from datetime import timedelta

    return Task.objects.create(
        task_type=Task.TaskType.ENRICH_EMAIL,
        status=status,
        scheduled_at=timezone.now() + timedelta(seconds=scheduled_offset_s),
        payload={"lead_id": 1, "operator": "Arian", "bettercontact_email_request_id": ""},
    )


@pytest.mark.django_db
def test_run_once_no_task_returns_false():
    assert EnrichmentWorker()._run_once() is False


@pytest.mark.django_db
def test_run_once_found_marks_task_completed():
    task = _enrich_task()
    found = EnrichmentResult(
        status=EnrichmentStatus.FOUND, provider="leadmagic", phone="+1",
    )
    with patch("linkedin.enrichment.worker.handle_enrich_phone", return_value=found):
        handled = EnrichmentWorker()._run_once()
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
        EnrichmentWorker()._run_once()
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
        EnrichmentWorker()._run_once()
    task.refresh_from_db()
    assert task.status == Task.Status.FAILED
    mock_degraded.assert_not_called()


@pytest.mark.django_db
def test_run_once_skip_result_none_marks_completed():
    task = _enrich_task()
    with patch("linkedin.enrichment.worker.handle_enrich_phone", return_value=None):
        EnrichmentWorker()._run_once()
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
        handled = EnrichmentWorker()._run_once()
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
        EnrichmentWorker()._run_once()
    task.refresh_from_db()
    assert task.status == Task.Status.FAILED
    mock_err.assert_called_once()


@pytest.mark.django_db
def test_reclaim_stale_resets_running_enrich_tasks():
    task = _enrich_task(status=Task.Status.RUNNING)
    email = _email_task(status=Task.Status.RUNNING)
    EnrichmentWorker()._reclaim_stale()
    task.refresh_from_db()
    email.refresh_from_db()
    assert task.status == Task.Status.PENDING
    assert email.status == Task.Status.PENDING


@pytest.mark.django_db
def test_start_stop_lifecycle_does_not_hang():
    worker = EnrichmentWorker(poll_interval=0.01)
    with patch.object(EnrichmentWorker, "_run_once", return_value=False):
        worker.start()
        assert worker._thread is not None
        worker.stop()
        assert worker._thread is None
