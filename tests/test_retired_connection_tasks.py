"""Historical tasks remain receipts after their execution paths are removed."""
from datetime import timedelta

import pytest
from django.utils import timezone

from linkedin.models import Task


@pytest.mark.django_db
@pytest.mark.parametrize("task_type", ["rb2b_connect", "rb2b_replies"])
@pytest.mark.parametrize("scope", [{}, {"operator": "Arian", "campaign_ids": [1]}])
def test_retired_tasks_are_neither_claimed_nor_scheduler_wakeups(task_type, scope):
    retired = Task.objects.create(
        task_type=task_type,
        scheduled_at=timezone.now() - timedelta(hours=1),
        payload={"operator": "Arian", "visitor_id": 123},
    )
    before = Task.objects.filter(pk=retired.pk).values().get()
    assert Task.objects.claim_next(**scope) is None
    assert Task.objects.seconds_to_next(**scope) is None
    current = Task.objects.create(
        task_type=Task.TaskType.CONNECT,
        scheduled_at=timezone.now() - timedelta(minutes=1),
        payload={"campaign_id": 1},
    )
    assert Task.objects.claim_next(**scope).pk == current.pk
    assert Task.objects.filter(pk=retired.pk).values().get() == before
