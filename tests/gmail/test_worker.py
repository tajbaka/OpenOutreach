from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from gmail.worker import GmailWorker
from linkedin.models import Task


def _gmail_task(status=Task.Status.PENDING):
    return Task.objects.create(
        task_type=Task.TaskType.GMAIL_FOLLOW_UP,
        status=status,
        scheduled_at=timezone.now() - timedelta(seconds=1),
        payload={"lead_id": 1, "operator": "Arian", "step_index": 0},
    )


@pytest.mark.django_db
def test_gmail_worker_run_once_marks_completed():
    task = _gmail_task()
    with patch("gmail.worker.handle_gmail_follow_up") as mock_handler:
        assert GmailWorker()._run_once() is True
    task.refresh_from_db()
    assert task.status == Task.Status.COMPLETED
    mock_handler.assert_called_once_with(task)


@pytest.mark.django_db
def test_gmail_worker_reclaims_stale_running_tasks():
    task = _gmail_task(status=Task.Status.RUNNING)
    GmailWorker()._reclaim_stale()
    task.refresh_from_db()
    assert task.status == Task.Status.PENDING


@pytest.mark.django_db
def test_gmail_worker_run_once_no_task_returns_false():
    assert GmailWorker()._run_once() is False


@pytest.mark.django_db
def test_gmail_worker_only_claims_own_operator_tasks():
    _gmail_task()
    athena_task = Task.objects.create(
        task_type=Task.TaskType.GMAIL_FOLLOW_UP,
        status=Task.Status.PENDING,
        scheduled_at=timezone.now() - timedelta(seconds=1),
        payload={"lead_id": 2, "operator": "Athena", "step_index": 0},
    )

    with patch("gmail.worker.handle_gmail_follow_up") as mock_handler:
        assert GmailWorker(operator="Athena")._run_once() is True

    athena_task.refresh_from_db()
    arian_task = Task.objects.get(payload__operator="Arian")
    assert athena_task.status == Task.Status.COMPLETED
    assert arian_task.status == Task.Status.PENDING
    mock_handler.assert_called_once_with(athena_task)
