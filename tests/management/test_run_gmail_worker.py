from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.db import OperationalError


@pytest.fixture(autouse=True)
def private_worker_logs(tmp_path, monkeypatch):
    monkeypatch.setattr("gmail.worker_logging.LOG_DIR", tmp_path)


def test_run_gmail_worker_is_account_scoped_and_guarded():
    with (
        patch(
            "linkedin.management.commands.run_gmail_worker.GmailWorker"
        ) as worker_class,
        patch(
            "linkedin.management.commands.run_gmail_worker.SingleInstanceGuard"
        ) as guard_class,
    ):
        call_command("run_gmail_worker", account="arian_boundera")

    worker_class.assert_called_once_with(account_key="arian_boundera")
    worker_class.return_value.run_forever.assert_called_once_with()
    worker_class.return_value.stop.assert_called_once_with()
    guard_class.assert_called_once()
    guard_kwargs = guard_class.call_args.kwargs
    assert guard_kwargs["pidfile"].name == (
        "run-gmail-worker-arian_boundera.pid"
    )
    assert guard_kwargs["marker"] == (
        "manage.py run_gmail_worker --account arian_boundera"
    )
    guard_class.return_value.acquire.assert_called_once_with()
    guard_class.return_value.release.assert_called_once_with()


def test_queue_connection_crash_is_saved_and_still_raises(tmp_path):
    with (
        patch("linkedin.management.commands.run_gmail_worker.SingleInstanceGuard") as guard,
        patch("gmail.worker.GmailWorker._reclaim_stale"),
        patch("gmail.worker.GmailWorker._claim_next", side_effect=OperationalError(
            "failed to resolve host password=private-secret recipient@example.test"
        )),
    ):
        with pytest.raises(OperationalError):
            call_command("run_gmail_worker", account="eddy_boundera")
    output = (tmp_path / "gmail-worker-eddy_boundera.log").read_text()
    assert '"event": "worker_crashed"' in output
    assert '"stage": "queue_claim"' in output
    assert "OperationalError" in output
    assert "host_resolution_failed" in output
    assert '"function": "_claim_next"' not in output  # mocked call, not a fake frame
    assert '"function": "_run_once"' in output
    assert "private-secret" not in output
    assert "recipient@example.test" not in output
    guard.return_value.release.assert_called_once()


def test_instance_guard_failure_is_logged_without_releasing_unowned_guard(tmp_path):
    with patch("linkedin.management.commands.run_gmail_worker.SingleInstanceGuard") as guard:
        guard.return_value.acquire.side_effect = RuntimeError("already running")
        with pytest.raises(RuntimeError, match="already running"):
            call_command("run_gmail_worker", account="eddy_boundera")
    output = (tmp_path / "gmail-worker-eddy_boundera.log").read_text()
    assert '"stage": "instance_guard"' in output
    assert '"event": "worker_crashed"' in output
    guard.return_value.release.assert_not_called()


def test_keyboard_interrupt_is_not_reported_as_crash(tmp_path):
    with (
        patch("linkedin.management.commands.run_gmail_worker.SingleInstanceGuard"),
        patch("linkedin.management.commands.run_gmail_worker.GmailWorker") as worker,
    ):
        worker.return_value.run_forever.side_effect = KeyboardInterrupt()
        call_command("run_gmail_worker", account="eddy_boundera")
    output = (tmp_path / "gmail-worker-eddy_boundera.log").read_text()
    assert "worker_interrupted" in output
    assert "worker_crashed" not in output
    worker.return_value.stop.assert_called_once()
