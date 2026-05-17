"""Tests for daemon resilience to Neon mid-task SSL drops.

Covers the three self-healing fixes added 2026-05-13 after the live crash:

1. `_safe_mark_failed` survives a dead connection during the daemon's task
   error path, by closing all DB conns + retrying once.
2. `handle_follow_up` post-send block (record_action + set_profile_state)
   retries on _DB_DEAD_ERRORS so a connection drop after the DM was sent
   doesn't leave the Deal in a re-send-able state.
3. The pre-flight `ensure_connection()` is exercised by the daemon loop
   itself, not unit-tested directly here — verified to be a single
   one-liner that opens a new connection when the current one is closed.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from django.db.utils import OperationalError

from linkedin import daemon
from linkedin.models import Task


def _make_pending_task(fake_session, task_type=Task.TaskType.FOLLOW_UP):
    """Minimal task row that mark_failed can act on."""
    return Task.objects.create(
        task_type=task_type,
        status=Task.Status.PENDING,
        scheduled_at=__import__("django").utils.timezone.now(),
        payload={"campaign_id": fake_session.campaign.pk, "public_id": "x"},
    )


# ------------------------------------------------------------------
# _safe_mark_failed
# ------------------------------------------------------------------


def test_safe_mark_failed_succeeds_on_first_try(db, fake_session):
    """No dead-connection error → returns True, task transitions to FAILED."""
    task = _make_pending_task(fake_session)
    task.mark_running()
    assert daemon._safe_mark_failed(task, "boom") is True
    task.refresh_from_db()
    assert task.status == Task.Status.FAILED
    assert "boom" in task.error


def test_safe_mark_failed_recycles_and_retries_on_dead_conn(db, fake_session):
    """First attempt raises OperationalError → close_all + retry succeeds."""
    task = _make_pending_task(fake_session)
    task.mark_running()

    call_count = {"n": 0}
    real_mark_failed = task.mark_failed

    def flaky_mark_failed(error_text):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise OperationalError("simulated SSL drop")
        return real_mark_failed(error_text)

    with patch.object(task, "mark_failed", side_effect=flaky_mark_failed):
        with patch("linkedin.daemon.connections.close_all") as mock_close:
            result = daemon._safe_mark_failed(task, "boom")

    assert result is True
    assert call_count["n"] == 2
    mock_close.assert_called_once()
    task.refresh_from_db()
    assert task.status == Task.Status.FAILED


def test_safe_mark_failed_returns_false_when_both_attempts_fail(db, fake_session, caplog):
    """Both attempts raise → log warning, return False, task stays RUNNING.

    Operationally this means `heal_tasks` will pick it up on next daemon
    startup and reset it to PENDING — we don't lose the task, just defer
    its recovery to the next process restart.
    """
    task = _make_pending_task(fake_session)
    task.mark_running()

    with patch.object(task, "mark_failed", side_effect=OperationalError("dead")):
        with patch("linkedin.daemon.connections.close_all"):
            result = daemon._safe_mark_failed(task, "boom")

    assert result is False
    task.refresh_from_db()
    assert task.status == Task.Status.RUNNING
    assert any("mark_failed retry also failed" in r.message for r in caplog.records)


# ------------------------------------------------------------------
# handle_follow_up post-send retry
# ------------------------------------------------------------------


def test_follow_up_post_send_retries_on_dead_conn(db, fake_session, monkeypatch):
    """A dead conn between `send_message` and the post-send DB writes should
    not leave the Deal at CONNECTED. The retry-with-fresh-conn block must
    fire and complete the state transition."""
    from datetime import datetime, timezone
    from crm.models import Lead, Deal, Message
    from linkedin.enums import ProfileState
    from linkedin.models import ActionLog
    from linkedin.tasks import follow_up as fu_mod

    # Seed a Lead + CONNECTED Deal so the handler has something to work on.
    lead = Lead.objects.create(
        first_name="Test", last_name="User",
        linkedin_url="https://www.linkedin.com/in/testuser/",
    )
    deal = Deal.objects.create(
        lead=lead,
        campaign=fake_session.campaign,
        state=ProfileState.CONNECTED.value,
    )
    # Seed an outbound LinkedIn message so the "no outbound thread" early
    # return doesn't fire — the handler's post-send path requires an
    # existing thread to follow up on.
    # Sender must match the daemon's operator handle so the
    # operator-ownership check passes — fake_session resolves to
    # "testuser@example.com" via resolve_operator's fallback path.
    Message.objects.create(
        lead=lead,
        source=Message.Source.LINKEDIN,
        direction=Message.Direction.OUTBOUND,
        sender=fake_session.linkedin_profile.linkedin_username,
        external_id="seed-1",
        body="initial",
        sent_at=datetime.now(timezone.utc),
    )

    # Patch out the LinkedIn-side calls so we only exercise the DB-write
    # half. Treat the send as "succeeded" (the bug-scenario trigger).
    monkeypatch.setattr(fu_mod, "get_profile_dict_for_public_id", lambda *a, **k: {"profile": {}})
    monkeypatch.setattr(fu_mod, "classify_role", lambda lead: "CSP")
    monkeypatch.setattr(
        fu_mod, "fill_for_lead",
        lambda lead, role, channel, **kw: type("F", (), {"body": "hi", "attachments": []})(),
    )

    # Inline send_raw_message + send_media_message that pretend to succeed.
    import linkedin.tasks.follow_up as fu_pkg
    monkeypatch.setattr(
        "linkedin.actions.message.send_raw_message", lambda *a, **k: True, raising=False,
    )
    monkeypatch.setattr(
        "linkedin.actions.message.send_media_message", lambda *a, **k: True, raising=False,
    )

    # Flaky record_action: first call raises OperationalError, second succeeds.
    real_record = fake_session.linkedin_profile.record_action
    call_count = {"n": 0}

    def flaky_record(action_type, campaign):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise OperationalError("simulated SSL drop")
        return real_record(action_type, campaign)

    # Build a minimal task object the handler can consume.
    task = MagicMock()
    task.payload = {"campaign_id": fake_session.campaign.pk, "public_id": "testuser"}

    # Make can_execute always allow, and stub resolve_operator.
    fake_session.linkedin_profile.can_execute = MagicMock(return_value=True)

    with patch.object(fake_session.linkedin_profile, "record_action", side_effect=flaky_record):
        with patch("linkedin.tasks.follow_up.connections.close_all") as mock_close:
            fu_mod.handle_follow_up(task, fake_session, qualifiers={})

    # Both attempts ran (flaky raised once, retried).
    assert call_count["n"] == 2
    mock_close.assert_called_once()
    # Deal advanced to COMPLETED — proves set_profile_state ran on the
    # retry (not skipped due to the first OperationalError escaping).
    deal.refresh_from_db()
    assert deal.state == ProfileState.COMPLETED.value
    # ActionLog row was written by the successful retry.
    assert ActionLog.objects.filter(
        linkedin_profile=fake_session.linkedin_profile,
        action_type=ActionLog.ActionType.FOLLOW_UP,
    ).count() == 1


def test_heal_tasks_does_not_reset_running_enrich_phone(fake_session):
    """heal_tasks resets all stale RUNNING tasks to PENDING — but it must
    leave ENRICH_PHONE tasks alone, because the EnrichmentWorker (which
    spawns after heal_tasks) owns reclaiming those itself. Resetting them
    here would yank an in-flight enrichment away from the worker."""
    from django.utils import timezone

    from linkedin.daemon import heal_tasks
    from linkedin.models import Task

    enrich = Task.objects.create(
        task_type=Task.TaskType.ENRICH_PHONE,
        status=Task.Status.RUNNING,
        scheduled_at=timezone.now(),
        payload={"lead_id": 1},
    )
    other = Task.objects.create(
        task_type=Task.TaskType.CONNECT,
        status=Task.Status.RUNNING,
        scheduled_at=timezone.now(),
        payload={"campaign_id": fake_session.campaign.pk},
    )

    heal_tasks(fake_session)

    enrich.refresh_from_db()
    other.refresh_from_db()
    assert enrich.status == Task.Status.RUNNING   # left for the worker
    assert other.status == Task.Status.PENDING    # reset as before
