import logging
from unittest.mock import Mock, patch

from gmail import worker_logging
from gmail.worker import GmailWorker
from linkedin.models import Task


def test_exception_chain_is_saved_without_messages_or_source_lines(tmp_path, monkeypatch):
    monkeypatch.setattr(worker_logging, "LOG_DIR", tmp_path)
    with worker_logging.worker_log("eddy_boundera"):
        try:
            try:
                raise ValueError("invalid_grant refresh_token=TOP_SECRET email body here")
            except ValueError as cause:
                raise RuntimeError("Authorization: Bearer TOP_SECRET") from cause
        except RuntimeError as exc:
            worker_logging.log_worker_event("eddy_boundera", "task_failed", exception=exc, task_id=42)
    text = (tmp_path / "gmail-worker-eddy_boundera.log").read_text()
    assert "RuntimeError" in text and "ValueError" in text
    assert "oauth_invalid_grant" in text
    assert '"task_id": 42' in text
    assert "TOP_SECRET" not in text
    assert "email body here" not in text
    assert "Authorization" not in text


def test_rotation_account_isolation_and_handler_cleanup(tmp_path, monkeypatch):
    monkeypatch.setattr(worker_logging, "LOG_DIR", tmp_path)
    before = list(worker_logging.logger.handlers)
    with worker_logging.worker_log("eddy_boundera"):
        handler = worker_logging.logger.handlers[-1]
        assert handler.maxBytes == 5_000_000
        assert handler.backupCount == 3
        handler.maxBytes = 250
        for _ in range(20):
            worker_logging.log_worker_event("eddy_boundera", "worker_starting")
            worker_logging.log_worker_event("arian_boundera", "other_account_event")
        worker_logging.logger.error("unstructured TOP_SECRET")
        logging.getLogger("gmail.worker").info("private email body")
    assert list(worker_logging.logger.handlers) == before
    files = list(tmp_path.glob("gmail-worker-eddy_boundera.log*"))
    assert len(files) == 4
    for path in files:
        text = path.read_text()
        assert "other_account_event" not in text
        assert "TOP_SECRET" not in text
        assert "private email body" not in text


def test_recoverable_task_failure_is_logged_without_task_payload(tmp_path, monkeypatch):
    monkeypatch.setattr(worker_logging, "LOG_DIR", tmp_path)
    task = Mock(id=42, task_type=Task.TaskType.GMAIL_FOLLOW_UP,
                payload={"body": "PRIVATE_BODY"})
    with (
        worker_logging.worker_log("eddy_boundera"),
        patch.object(GmailWorker, "_claim_next", return_value=task),
        patch("gmail.worker.handle_gmail_follow_up", side_effect=RuntimeError("PRIVATE_BODY")),
        patch("gmail.worker.reschedule_persisted_current_gmail_task", return_value=False),
        patch("gmail.worker.notify_error"),
    ):
        assert GmailWorker(account_key="eddy_boundera")._run_once() is True
    output = (tmp_path / "gmail-worker-eddy_boundera.log").read_text()
    assert '"event": "task_failed"' in output
    assert '"task_id": 42' in output
    assert '"stage": "task_handler"' in output
    assert "PRIVATE_BODY" not in output
    task.mark_failed.assert_called_once()
