from unittest.mock import Mock
from datetime import timedelta

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from linkedin import conf
from linkedin.discovery.collector import fresh_discovery_payload
from linkedin.models import Task


@pytest.mark.django_db
def test_run_once_claims_only_discovery_task(fake_session, monkeypatch):
    monkeypatch.setattr(conf, "ENABLE_PROFILE_DISCOVERY", True)
    monkeypatch.setattr(
        "linkedin.management.commands.run_discovery_once."
        "validate_discovery_settings",
        lambda: None,
    )
    monkeypatch.setattr(
        "linkedin.management.commands.run_discovery_once.discovery_window_open",
        lambda: True,
    )
    monkeypatch.setattr(
        "linkedin.management.commands.run_discovery_once."
        "discovery_enabled_for_sender",
        lambda profile, operator: True,
    )
    monkeypatch.setattr(
        "linkedin.management.commands.run_discovery_once."
        "reconcile_discovery_tasks",
        lambda profile, operator: True,
    )
    monkeypatch.setattr(
        "linkedin.management.commands.run_discovery_once.get_or_create_session",
        lambda handle: fake_session,
    )
    fake_session.close = Mock()
    fake_session.ensure_browser = Mock()
    handler = Mock()
    monkeypatch.setattr(
        "linkedin.management.commands.run_discovery_once.handle_discovery",
        handler,
    )

    discovery = Task.objects.create(
        task_type=Task.TaskType.DISCOVERY,
        scheduled_at=timezone.now(),
        payload=fresh_discovery_payload("testuser@example.com"),
    )
    status = Task.objects.create(
        task_type=Task.TaskType.STATUS_SUMMARY,
        scheduled_at=timezone.now(),
        payload={},
    )

    call_command("run_discovery_once", handle="testuser")

    discovery.refresh_from_db()
    status.refresh_from_db()
    assert discovery.status == Task.Status.COMPLETED
    assert status.status == Task.Status.PENDING
    handler.assert_called_once_with(discovery, fake_session)
    fake_session.close.assert_called_once_with()
    fake_session.ensure_browser.assert_called_once_with()


@pytest.mark.django_db
def test_run_once_keeps_one_browser_for_bounded_batch(fake_session, monkeypatch):
    monkeypatch.setattr(conf, "ENABLE_PROFILE_DISCOVERY", True)
    command = "linkedin.management.commands.run_discovery_once"
    monkeypatch.setattr(f"{command}.validate_discovery_settings", lambda: None)
    monkeypatch.setattr(f"{command}.discovery_window_open", lambda: True)
    monkeypatch.setattr(f"{command}.discovery_window_end", lambda: timezone.now() + timedelta(hours=1))
    monkeypatch.setattr(f"{command}.discovery_enabled_for_sender", lambda profile, operator: True)
    monkeypatch.setattr(f"{command}.reconcile_discovery_tasks", lambda profile, operator: True)
    monkeypatch.setattr(f"{command}.get_or_create_session", lambda handle: fake_session)
    fake_session.close = Mock()
    fake_session.ensure_browser = Mock()
    handler = Mock()
    monkeypatch.setattr(f"{command}.handle_discovery", handler)

    tasks = [
        Task.objects.create(
            task_type=Task.TaskType.DISCOVERY,
            scheduled_at=timezone.now(),
            payload=fresh_discovery_payload("testuser@example.com"),
        )
        for _ in range(2)
    ]

    call_command("run_discovery_once", handle="testuser", max_tasks=2)

    assert handler.call_count == 2
    assert {call.args[0].pk for call in handler.call_args_list} == {task.pk for task in tasks}
    fake_session.ensure_browser.assert_called_once_with()
    fake_session.close.assert_called_once_with()


@pytest.mark.django_db
def test_run_once_refuses_when_discovery_disabled(monkeypatch):
    monkeypatch.setattr(conf, "ENABLE_PROFILE_DISCOVERY", False)
    monkeypatch.setattr(
        "linkedin.management.commands.run_discovery_once."
        "validate_discovery_settings",
        lambda: None,
    )

    with pytest.raises(CommandError, match="ENABLE_PROFILE_DISCOVERY is false"):
        call_command("run_discovery_once", handle="testuser")
