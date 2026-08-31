"""Durable submission boundary for current LinkedIn media follow-up Tasks."""
from __future__ import annotations

from collections.abc import Callable

from django.db import transaction
from django.utils import timezone


SUBMISSION_ATTEMPTED_AT_KEY = "linkedin_media_submission_attempted_at"
SUBMISSION_LEAD_ID_KEY = "linkedin_media_submission_lead_id"
SUBMISSION_MESSAGE_PREFIX_KEY = "linkedin_media_submission_message_prefix"
SUBMISSION_OPERATOR_KEY = "linkedin_media_submission_operator"


def submission_attempted(payload) -> bool:
    """Return whether a current media Task crossed its no-retry boundary."""
    if not isinstance(payload, dict):
        return False
    return bool(str(payload.get(SUBMISSION_ATTEMPTED_AT_KEY) or "").strip())


def persisted_submission_evidence(payload) -> bool:
    """Return whether the exact marked media Task has its outbound Message."""
    from crm.models import Message

    if not submission_attempted(payload):
        return False
    lead_id = payload.get(SUBMISSION_LEAD_ID_KEY)
    prefix = str(payload.get(SUBMISSION_MESSAGE_PREFIX_KEY) or "").strip()
    from linkedin.operators import resolve_operator

    operator = resolve_operator(
        payload.get(SUBMISSION_OPERATOR_KEY) or payload.get("operator")
    )
    if (
        isinstance(lead_id, bool)
        or not isinstance(lead_id, int)
        or lead_id <= 0
        or not prefix.startswith("daemon-send:")
        or not prefix.endswith(":")
        or not operator
        or not prefix.startswith(f"daemon-send:{operator}:")
    ):
        return False
    candidates = Message.objects.filter(
        lead_id=lead_id,
        source=Message.Source.LINKEDIN,
        direction=Message.Direction.OUTBOUND,
        external_id__startswith=prefix,
    ).only("raw")
    return any(
        isinstance(message.raw, dict)
        and isinstance(message.raw.get("media"), dict)
        for message in candidates
    )


def unresolved_submission_keys() -> set[tuple[int, str]]:
    """Return Lead/operator pairs whose marked media send lacks evidence.

    The marker remains authoritative after the original Task becomes FAILED,
    so startup catch-up cannot create a fresh unmarked Task for the same Lead.
    """
    from linkedin.models import Task
    from linkedin.operators import resolve_operator

    blocked: set[tuple[int, str]] = set()
    marked_tasks = Task.objects.filter(
        task_type=Task.TaskType.FOLLOW_UP,
        payload__has_key=SUBMISSION_ATTEMPTED_AT_KEY,
    ).only("payload")
    for task in marked_tasks:
        payload = task.payload
        lead_id = payload.get(SUBMISSION_LEAD_ID_KEY) if isinstance(payload, dict) else None
        operator = resolve_operator(
            payload.get(SUBMISSION_OPERATOR_KEY) or payload.get("operator")
        ) if isinstance(payload, dict) else ""
        if (
            submission_attempted(payload)
            and isinstance(lead_id, int)
            and not isinstance(lead_id, bool)
            and lead_id > 0
            and operator
            and not persisted_submission_evidence(payload)
        ):
            blocked.add((lead_id, operator))
    return blocked


def has_unresolved_submission(*, lead_id: int, operator: str) -> bool:
    """Return whether this sender must not create or execute another send."""
    from linkedin.operators import resolve_operator

    return (lead_id, resolve_operator(operator)) in unresolved_submission_keys()


@transaction.atomic
def stamp_submission_attempt(
    task,
    *,
    lead_id: int,
    message_prefix: str,
    operator: str,
    final_guard: Callable[[], None],
) -> None:
    """Serialize on the Lead, recheck guards, then persist the send boundary."""
    from crm.models import Lead
    from linkedin.models import Task
    from linkedin.operators import resolve_operator

    canonical_operator = resolve_operator(operator)

    # Lead-first locking serializes sibling campaign Tasks for the same
    # recipient. A second daemon cannot pass its final guard until the first
    # Task's durable marker is visible.
    Lead.objects.select_for_update(of=("self",)).get(pk=lead_id)
    locked = Task.objects.select_for_update(of=("self",)).get(pk=task.pk)
    if locked.task_type != Task.TaskType.FOLLOW_UP:
        raise ValueError("LinkedIn media submission marker received another Task type")
    if locked.status != Task.Status.RUNNING:
        raise ValueError("LinkedIn media submission marker requires a running Task")
    if submission_attempted(locked.payload):
        raise ValueError("LinkedIn media submission was already attempted for this Task")
    if (
        isinstance(lead_id, bool)
        or not isinstance(lead_id, int)
        or lead_id <= 0
        or not isinstance(message_prefix, str)
        or not message_prefix.startswith("daemon-send:")
        or not message_prefix.endswith(":")
        or not canonical_operator
        or not message_prefix.startswith(f"daemon-send:{canonical_operator}:")
    ):
        raise ValueError("LinkedIn media submission marker received invalid evidence")

    final_guard()

    payload = dict(locked.payload or {})
    payload[SUBMISSION_ATTEMPTED_AT_KEY] = timezone.now().isoformat()
    payload[SUBMISSION_LEAD_ID_KEY] = lead_id
    payload[SUBMISSION_MESSAGE_PREFIX_KEY] = message_prefix
    payload[SUBMISSION_OPERATOR_KEY] = canonical_operator
    locked.payload = payload
    locked.save(update_fields={"payload"})
    task.payload = payload


def _requeue(task) -> None:
    task.status = task.Status.PENDING
    task.started_at = None
    task.scheduled_at = timezone.now()
    task.error = ""
    task.save(update_fields={"status", "started_at", "scheduled_at", "error"})


@transaction.atomic
def recover_stale_media_follow_up_task(task_id: int) -> bool:
    """Recover marked Tasks without ever retrying an ambiguous provider send."""
    from linkedin.models import Task

    task = Task.objects.select_for_update(of=("self",)).filter(pk=task_id).first()
    if task is None or task.task_type != Task.TaskType.FOLLOW_UP:
        return False
    if not submission_attempted(task.payload):
        return False
    has_evidence = persisted_submission_evidence(task.payload)
    if task.status == Task.Status.FAILED and has_evidence:
        _requeue(task)
        return True
    if task.status != Task.Status.RUNNING:
        return False
    if has_evidence:
        _requeue(task)
        return True

    task.status = Task.Status.FAILED
    task.error = (
        "LinkedIn media submission outcome is unclear after daemon restart; "
        "automatic retry is blocked"
    )
    task.save(update_fields={"status", "error"})
    return True
