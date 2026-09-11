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
    delivery_id = payload.get("delivery_id")
    message_version_id = payload.get("message_version_id")
    if delivery_id is not None or message_version_id is not None:
        if (
            isinstance(delivery_id, bool)
            or not isinstance(delivery_id, int)
            or delivery_id <= 0
            or isinstance(message_version_id, bool)
            or not isinstance(message_version_id, int)
            or message_version_id <= 0
        ):
            return ""
        return (
            f"gmail_follow_up:delivery-{delivery_id}:"
            f"version-{message_version_id}"
        )
    return (
        f"gmail_follow_up:{operator}:{lead_id}:"
        f"{sequence_name}:step-{step_index}"
    )


def persisted_submission_evidence(payload) -> bool:
    """Return whether the exact current Task already has an outbound Message."""
    return _persisted_submission_message(payload) is not None


def _persisted_submission_message(payload):
    from crm.models import Message

    automation_key = current_gmail_automation_key(payload)
    if not automation_key:
        return None
    filters = {
        "lead_id": payload["lead_id"],
        "source": Message.Source.GMAIL,
        "direction": Message.Direction.OUTBOUND,
        "raw__automation_key": automation_key,
    }
    if payload.get("delivery_id") is not None:
        filters.update({
            "raw__delivery_id": payload["delivery_id"],
            "raw__message_version_id": payload["message_version_id"],
        })
    return Message.objects.filter(
        **filters,
    ).order_by("pk").first()


def _versioned_delivery_for_task(task):
    """Return an exact bound delivery or legacy ``None`` for one Task."""
    payload = task.payload if isinstance(task.payload, dict) else {}
    from gmail.handoff import _bound_gmail_delivery

    return _bound_gmail_delivery(
        deal_id=payload.get("deal_id"),
        lead_id=payload.get("lead_id"),
        operator=payload.get("operator") or "",
        step_index=payload.get("step_index"),
        delivery_id=payload.get("delivery_id"),
        message_version_id=payload.get("message_version_id"),
        task_id=task.pk,
    )


def _set_delivery_status(delivery, status: str, *, sent_at=None) -> None:
    if delivery is None:
        return
    from linkedin.message_delivery_runtime import mark_delivery_status

    mark_delivery_status(delivery, status)
    if sent_at is not None and delivery.sent_at != sent_at:
        delivery.sent_at = sent_at
        delivery.save(update_fields={"sent_at", "updated_at"})


def _lock_current_delivery(task):
    """Keep delivery timing stable through the final Task/receipt transaction."""
    from linkedin.models import OutboundDelivery

    delivery_id = (task.payload or {}).get("delivery_id")
    if isinstance(delivery_id, int) and not isinstance(delivery_id, bool):
        # Identity validation below remains authoritative. This read only
        # establishes the lock before inspecting mutable schedule/status.
        OutboundDelivery.objects.select_for_update(of=("self",)).filter(
            pk=delivery_id,
        ).first()
    return _versioned_delivery_for_task(task)


def _lock_current_task(task_id: int, *, expected_payload=None):
    """Use the shared Lead-first lock prefix before Task and delivery locks."""
    from crm.models import Lead
    from linkedin.models import Task

    snapshot = Task.objects.filter(pk=task_id).values("payload").first()
    if snapshot is None:
        return None
    snapshot_payload = snapshot["payload"] or {}
    lead_id = snapshot_payload.get("lead_id")
    if isinstance(lead_id, int) and not isinstance(lead_id, bool):
        Lead.objects.select_for_update(of=("self",)).filter(pk=lead_id).first()
    locked = Task.objects.select_for_update(of=("self",)).filter(pk=task_id).first()
    if locked is None:
        return None
    if (locked.payload or {}).get("lead_id") != lead_id:
        raise ValueError("Gmail Task Lead changed while acquiring its ownership lock")
    if expected_payload is not None and (
        current_gmail_automation_key(locked.payload) != current_gmail_automation_key(expected_payload)
        or (locked.payload or {}).get("deal_id") != expected_payload.get("deal_id")
    ):
        raise ValueError("Gmail Task identity changed after claim")
    return locked


def _requeue(task, *, scheduled_at=None) -> None:
    task.status = task.Status.PENDING
    task.started_at = None
    task.scheduled_at = scheduled_at or task.scheduled_at
    task.error = ""
    task.save(update_fields={"status", "started_at", "scheduled_at", "error"})


def _defer_locked_task_if_early(locked, *, delivery, task) -> bool:
    """Keep the same frozen identity while honoring both persisted due dates."""
    from crm.models import Deal
    from linkedin.models import Campaign
    from linkedin.tasks.follow_up import _normalize_linkedin_due_at

    now = timezone.now()
    due_at = locked.scheduled_at
    if delivery is not None:
        due_at = max(due_at, delivery.scheduled_at)
    # Existing campaigns retain their current cadence. Only the new opt-in
    # lane uses the sender's active/rest-day window at the Gmail boundary.
    invitation_mode = Deal.objects.filter(
        pk=(locked.payload or {}).get("deal_id"),
        campaign__gmail_start_mode=Campaign.GmailStartMode.INVITATION_SENT,
    ).exists()
    if invitation_mode:
        due_at = _normalize_linkedin_due_at(due_at, current_time=now)
    if due_at <= now:
        return False
    _requeue(locked, scheduled_at=due_at)
    locked.error = "Gmail deferred until its persisted due time/sender window"
    locked.save(update_fields={"error"})
    task.status = locked.status
    task.started_at = locked.started_at
    task.scheduled_at = locked.scheduled_at
    task.error = locked.error
    return True


@transaction.atomic
def defer_early_current_gmail_task(task) -> bool:
    """Defer a mistakenly early claim without making a replacement Task."""
    from linkedin.models import Task

    locked = _lock_current_task(task.pk, expected_payload=task.payload)
    if locked is None:
        raise ValueError("Gmail timing Task no longer exists")
    if locked.task_type != Task.TaskType.GMAIL_FOLLOW_UP:
        raise ValueError("Gmail timing guard received another Task type")
    if locked.status != Task.Status.RUNNING:
        raise ValueError("Gmail timing guard requires a claimed running Task")
    delivery = _lock_current_delivery(locked)
    if submission_attempted(locked.payload) or (
        delivery is not None and delivery.status != delivery.Status.QUEUED
    ):
        raise ValueError("Gmail timing guard requires an unsubmitted queued delivery")
    return _defer_locked_task_if_early(locked, delivery=delivery, task=task)


@transaction.atomic
def stamp_submission_attempt(task) -> bool:
    """Stamp an on-time submission, or commit a safe same-Task deferral."""
    from linkedin.models import Task

    locked = _lock_current_task(task.pk, expected_payload=task.payload)
    if locked is None:
        raise ValueError("Gmail submission Task no longer exists")
    if locked.task_type != Task.TaskType.GMAIL_FOLLOW_UP:
        raise ValueError("Gmail submission marker received another Task type")
    if locked.status != Task.Status.RUNNING:
        raise ValueError("Gmail submission marker requires a claimed running Task")
    if submission_attempted(locked.payload):
        raise ValueError("Gmail submission was already attempted for this Task")
    delivery = _lock_current_delivery(locked)
    if delivery is not None and delivery.status != delivery.Status.QUEUED:
        raise ValueError(
            f"versioned Gmail delivery {delivery.pk} status "
            f"{delivery.status!r} cannot cross submission"
        )
    if _defer_locked_task_if_early(locked, delivery=delivery, task=task):
        return False
    payload = dict(locked.payload or {})
    payload[SUBMISSION_ATTEMPTED_AT_KEY] = timezone.now().isoformat()
    locked.payload = payload
    locked.save(update_fields={"payload"})
    if delivery is not None:
        _set_delivery_status(delivery, delivery.Status.SENDING)
    task.payload = payload
    return True


@transaction.atomic
def recover_stale_current_gmail_task(task_id: int) -> bool:
    """Recover pre-submit work or post-submit work with exact sent evidence."""
    from linkedin.models import Task

    task = _lock_current_task(task_id)
    if task is None or task.task_type != Task.TaskType.GMAIL_FOLLOW_UP:
        return False
    if task.status != Task.Status.RUNNING:
        return False
    try:
        delivery = _lock_current_delivery(task)
    except ValueError as exc:
        task.status = Task.Status.FAILED
        task.error = f"Invalid versioned Gmail delivery: {exc}"
        task.save(update_fields={"status", "error"})
        return True
    if delivery is not None and delivery.status in {
        delivery.Status.STOPPED,
        delivery.Status.UNCLEAR,
    }:
        task.status = Task.Status.FAILED
        task.error = (
            f"Versioned Gmail delivery is terminal: {delivery.status}"
        )
        task.save(update_fields={"status", "error"})
        return True
    if delivery is not None and delivery.status in {
        delivery.Status.SENT,
        delivery.Status.SENDING,
    }:
        sent_message = _persisted_submission_message(task.payload)
        if sent_message is not None:
            _set_delivery_status(
                delivery,
                delivery.Status.SENT,
                sent_at=sent_message.sent_at,
            )
            _requeue(task, scheduled_at=timezone.now())
            return True
        if delivery.status == delivery.Status.SENT:
            task.status = Task.Status.FAILED
            task.error = "Gmail delivery is marked sent without persisted Message evidence"
            task.save(update_fields={"status", "error"})
            return True
        _set_delivery_status(delivery, delivery.Status.UNCLEAR)
        task.status = Task.Status.FAILED
        task.error = (
            "Gmail delivery was sending without persisted Message evidence; "
            "automatic retry is blocked"
        )
        task.save(update_fields={"status", "error"})
        return True
    if submission_attempted(task.payload):
        sent_message = _persisted_submission_message(task.payload)
        if sent_message is not None:
            if delivery is not None:
                _set_delivery_status(
                    delivery,
                    delivery.Status.SENT,
                    sent_at=sent_message.sent_at,
                )
            _requeue(task, scheduled_at=timezone.now())
            return True
        if delivery is not None:
            _set_delivery_status(delivery, delivery.Status.UNCLEAR)
        task.status = Task.Status.FAILED
        task.error = (
            "Gmail submission outcome is unclear after worker restart; "
            "automatic retry is blocked"
        )
        task.save(update_fields={"status", "error"})
        return True
    if delivery is not None:
        _set_delivery_status(delivery, delivery.Status.QUEUED)
    _requeue(task)
    return True


@transaction.atomic
def reschedule_persisted_current_gmail_task(task_id: int) -> bool:
    """Heal the post-persist/pre-successor failure window without re-sending."""
    from linkedin.models import Task

    task = _lock_current_task(task_id)
    if task is None or task.task_type != Task.TaskType.GMAIL_FOLLOW_UP:
        return False
    if task.status != Task.Status.RUNNING:
        return False
    if not submission_attempted(task.payload):
        return False
    try:
        delivery = _lock_current_delivery(task)
    except ValueError:
        return False
    if delivery is not None and delivery.status in {
        delivery.Status.STOPPED,
        delivery.Status.UNCLEAR,
    }:
        return False
    sent_message = _persisted_submission_message(task.payload)
    if sent_message is None:
        return False
    if delivery is not None:
        _set_delivery_status(
            delivery,
            delivery.Status.SENT,
            sent_at=sent_message.sent_at,
        )
    _requeue(
        task,
        scheduled_at=timezone.now() + POST_SEND_RECOVERY_DELAY,
    )
    return True
