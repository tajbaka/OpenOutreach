from datetime import timedelta

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone


@pytest.mark.django_db(transaction=True)
def test_retirement_migration_fails_only_live_withdrawal_tasks():
    executor = MigrationExecutor(connection)
    executor.migrate(
        [("linkedin", "0021_alter_actionlog_action_type_alter_task_task_type")]
    )
    old_apps = executor.loader.project_state(
        [("linkedin", "0021_alter_actionlog_action_type_alter_task_task_type")]
    ).apps
    OldTask = old_apps.get_model("linkedin", "Task")
    pending = OldTask.objects.create(
        task_type="withdraw_invites",
        status="pending",
        scheduled_at=timezone.now(),
        payload={"operator": "Athena"},
    )
    completed = OldTask.objects.create(
        task_type="withdraw_invites",
        status="completed",
        scheduled_at=timezone.now() - timedelta(days=1),
        completed_at=timezone.now(),
        payload={"operator": "Athena"},
    )

    executor = MigrationExecutor(connection)
    executor.migrate([("linkedin", "0022_retire_withdraw_invites_task")])
    new_apps = executor.loader.project_state(
        [("linkedin", "0022_retire_withdraw_invites_task")]
    ).apps
    NewTask = new_apps.get_model("linkedin", "Task")

    retired = NewTask.objects.get(pk=pending.pk)
    assert retired.status == "failed"
    assert retired.completed_at is not None
    assert "withdraw_invitations" in retired.error
    assert NewTask.objects.get(pk=completed.pk).status == "completed"
