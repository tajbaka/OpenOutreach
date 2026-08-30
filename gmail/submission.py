"""Durable submission-boundary helpers for current Gmail follow-up Tasks."""
from __future__ import annotations

from datetime import timedelta

from django.db import transaction
from django.utils import timezone


SUBMISSION_ATTEMPTED_AT_KEY = "gmail_submission_attempted_at"
POST_SEND_RECOVERY_DELAY = timedelta(minutes=5)


def submission_attempted(payload) -> bool:
    """Return whether a Task crossed the no-automatic-retry boundary."""
    if not isinstance(payload, dict):
        return False
    return bool(str(payload.get(SUBMISSION_ATTEMPTED_AT_KEY) or "").strip())


def current_gmail_automation_key(payload) -> str:
    """Return the exact persisted Message key for a valid current Gmail Task."""
    if not isinstance(payload, dict):
        return ""
    lead_id = payload.get("lead_id")
    operator = str(payload.get("operator") or "").strip()
    sequence_name = str(payload.get("sequence_name") or "gmail_fallback").strip()
    step_index = payload.get("step_index")
    if (
        isinstance(lead_id, bool)
        or not isinstance(lead_id, int)
        or lead_id <= 0
        or not operator
        or not sequence_name
        or isinstance(step_index, bool)
        or not isinstance(step_index, int)
        or step_index < 0
    ):
        return ""
    return (
        f"gmail_follow_up:{operator}:{lead_id}:"
        f"{sequence_name}:step-{step_index}"
    )


def persisted_submission_evidence(payload) -> bool:
    """Return whether the exact current Task already has an outbound Message."""
    from crm.models import Message

    automation_key = current_gmail_automation_key(payload)
    if not automation_key:
        return False
    return Message.objects.filter(
        lead_id=payload["lead_id"],
        source=Message.Source.GMAIL,
        direction=Message.Direction.OUTBOUND,
        raw__automation_key=automation_key,
    ).exists()


def _requeue(task, *, scheduled_at=None) -> None:
    task.status = task.Status.PENDING
    task.started_at = None
    task.scheduled_at = scheduled_at or task.scheduled_at
    task.error = ""
    task.save(update_fields={"status", "started_at", "scheduled_at", "error"})


@transaction.atomic
def stamp_submission_attempt(task) -> None:
    """Persist the boundary immediately before Gmail provider submission."""
    from linkedin.models import Task

    locked = Task.objects.select_for_update(of=("self",)).get(pk=task.pk)
    if locked.task_type != Task.TaskType.GMAIL_FOLLOW_UP:
        raise ValueError("Gmail submission marker received another Task type")
    if locked.status not in {Task.Status.PENDING, Task.Status.RUNNING}:
        raise ValueError("Gmail submission marker requires a live Task")
    if submission_attempted(locked.payload):
        raise ValueError("Gmail submission was already attempted for this Task")
    payload = dict(locked.payload or {})
    payload[SUBMISSION_ATTEMPTED_AT_KEY] = timezone.now().isoformat()
    locked.payload = payload
    locked.save(update_fields={"payload"})
    task.payload = payload


@transaction.atomic
def recover_stale_current_gmail_task(task_id: int) -> bool:
    """Recover pre-submit work or post-submit work with exact sent evidence."""
    from linkedin.models import Task

    task = Task.objects.select_for_update(of=("self",)).filter(pk=task_id).first()
    if task is None or task.task_type != Task.TaskType.GMAIL_FOLLOW_UP:
        return False
    if task.status != Task.Status.RUNNING:
        return False
    if submission_attempted(task.payload):
        if persisted_submission_evidence(task.payload):
            _requeue(task, scheduled_at=timezone.now())
            return True
        task.status = Task.Status.FAILED
        task.error = (
            "Gmail submission outcome is unclear after worker restart; "
            "automatic retry is blocked"
        )
        task.save(update_fields={"status", "error"})
        return True
    _requeue(task)
    return True


@transaction.atomic
def reschedule_persisted_current_gmail_task(task_id: int) -> bool:
    """Heal the post-persist/pre-successor failure window without re-sending."""
    from linkedin.models import Task

    task = Task.objects.select_for_update(of=("self",)).filter(pk=task_id).first()
    if task is None or task.task_type != Task.TaskType.GMAIL_FOLLOW_UP:
        return False
    if task.status != Task.Status.RUNNING:
        return False
    if not submission_attempted(task.payload):
        return False
    if not persisted_submission_evidence(task.payload):
        return False
    _requeue(
        task,
        scheduled_at=timezone.now() + POST_SEND_RECOVERY_DELAY,
    )
    return True
