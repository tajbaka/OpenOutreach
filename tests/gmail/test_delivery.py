import ast
from pathlib import Path

import pytest
from django.utils import timezone

from gmail.delivery import (
    consume_gmail_delivery_permit,
    issue_gmail_delivery_permit,
)
from gmail.exceptions import GmailDeliveryAuthorizationError
from linkedin.models import Task


pytestmark = pytest.mark.django_db


MESSAGE = {
    "operator": "Arian",
    "account_key": "arian_boundera",
    "to": "ada@example.com",
    "subject": "A subject",
    "body": "A body",
    "rfc_message_id": "<automation@getboundera.com>",
}


def _task(*, task_type=Task.TaskType.GMAIL_FOLLOW_UP, status=Task.Status.RUNNING):
    payload = {"operator": "Arian"}
    if task_type == Task.TaskType.GMAIL_FOLLOW_UP:
        payload.update({"lead_id": 1, "step_index": 0})
    elif task_type == Task.TaskType.MANUAL_REPLY:
        payload.update({"lead_id": 1, "message": "A reply"})
    return Task.objects.create(
        task_type=task_type,
        status=status,
        started_at=timezone.now() if status == Task.Status.RUNNING else None,
        scheduled_at=timezone.now(),
        payload=payload,
    )


def test_claimed_automation_task_issues_single_use_exact_message_permit():
    permit = issue_gmail_delivery_permit(task=_task(), **MESSAGE)

    consume_gmail_delivery_permit(permit, **MESSAGE)

    with pytest.raises(GmailDeliveryAuthorizationError, match="already used"):
        consume_gmail_delivery_permit(permit, **MESSAGE)


def test_pending_task_cannot_authorize_gmail_delivery():
    with pytest.raises(GmailDeliveryAuthorizationError, match="claimed running Task"):
        issue_gmail_delivery_permit(
            task=_task(status=Task.Status.PENDING),
            **MESSAGE,
        )


def test_non_gmail_task_cannot_authorize_gmail_delivery():
    with pytest.raises(GmailDeliveryAuthorizationError, match="automated Gmail lane"):
        issue_gmail_delivery_permit(
            task=_task(task_type=Task.TaskType.MANUAL_REPLY),
            **MESSAGE,
        )


def test_permit_rejects_changed_recipient_or_content():
    permit = issue_gmail_delivery_permit(task=_task(), **MESSAGE)

    with pytest.raises(GmailDeliveryAuthorizationError, match="content does not match"):
        consume_gmail_delivery_permit(
            permit,
            **{**MESSAGE, "to": "someone-else@example.com"},
        )


def test_production_gmail_send_callers_stay_worker_only():
    root = Path(__file__).resolve().parents[2]
    python_files = list(root.glob("*.py"))
    for package in ("api", "crm", "drip", "gmail", "linkedin", "notifications"):
        python_files.extend((root / package).rglob("*.py"))

    callers = set()
    for path in python_files:
        if "migrations" in path.parts:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        if any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "send_message"
            for node in ast.walk(tree)
        ):
            callers.add(path.relative_to(root).as_posix())

    assert callers == {
        "drip/tasks/gmail.py",
        "gmail/tasks/follow_up.py",
    }
    assert not (
        root / "linkedin/management/commands/gmail_send_test.py"
    ).exists()
