"""Runtime guards for executing already-frozen outbound deliveries."""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from linkedin.message_delivery import MessageDeliveryError
from linkedin.models import OutboundDelivery, Task
from linkedin.operators import resolve_operator


def delivery_audit_metadata(delivery: OutboundDelivery) -> dict[str, object]:
    """Return stable identifiers persisted beside the provider message."""
    enrollment = delivery.enrollment
    return {
        "outbound_delivery_id": delivery.pk,
        "message_enrollment_id": enrollment.pk,
        "message_version_id": enrollment.message_version_id,
        "message_program_key": enrollment.message_version.program.key,
        "message_content_hash": enrollment.message_version.content_hash,
        "audience_key": enrollment.audience_key,
        "message_channel": delivery.channel,
        "message_step_key": delivery.step_key,
        "message_step_index": delivery.step_index,
        "message_variant_key": delivery.variant_key,
        "message_render_hash": delivery.render_hash,
    }


@transaction.atomic
def bind_delivery_task(
    *,
    delivery: OutboundDelivery,
    task: Task,
) -> OutboundDelivery:
    """Bind the latest durable Task and mark a nonterminal delivery queued."""
    locked = (
        OutboundDelivery.objects.select_for_update()
        .select_related("enrollment")
        .get(pk=delivery.pk)
    )
    if locked.status in {
        OutboundDelivery.Status.UNCLEAR,
        OutboundDelivery.Status.STOPPED,
    }:
        raise MessageDeliveryError(
            f"delivery {locked.pk} in terminal status {locked.status!r} cannot be queued"
        )
    locked.task = task
    update_fields = {"task", "updated_at"}
    if locked.status != OutboundDelivery.Status.SENT:
        # An existing pending Task may predate the frozen delivery's due time.
        # Binding it must repair that Task, not erase the delivery's delay.
        if task.scheduled_at < locked.scheduled_at:
            task.scheduled_at = locked.scheduled_at
            task.save(update_fields=["scheduled_at"])
        locked.status = OutboundDelivery.Status.QUEUED
        locked.scheduled_at = task.scheduled_at
        update_fields.update({"status", "scheduled_at"})
    locked.save(update_fields=update_fields)
    delivery.task_id = task.pk
    delivery.status = locked.status
    delivery.scheduled_at = locked.scheduled_at
    return delivery


@transaction.atomic
def mark_delivery_status(
    delivery: OutboundDelivery,
    status: str,
) -> OutboundDelivery:
    """Move mutable execution state without changing frozen identity or copy."""
    allowed = {choice for choice, _label in OutboundDelivery.Status.choices}
    if status not in allowed:
        raise MessageDeliveryError(f"unknown outbound delivery status {status!r}")
    locked = OutboundDelivery.objects.select_for_update().get(pk=delivery.pk)
    if locked.status == OutboundDelivery.Status.SENT and status != locked.status:
        raise MessageDeliveryError(f"sent delivery {locked.pk} cannot change status")
    if locked.status == OutboundDelivery.Status.UNCLEAR and status != locked.status:
        raise MessageDeliveryError(
            f"unclear delivery {locked.pk} requires manual reconciliation"
        )
    if locked.status == OutboundDelivery.Status.STOPPED and status != locked.status:
        raise MessageDeliveryError(f"stopped delivery {locked.pk} cannot be resumed")
    if (
        status == OutboundDelivery.Status.SENDING
        and locked.channel == OutboundDelivery.Channel.LINKEDIN_FOLLOWUP
        and locked.scheduled_at > timezone.now()
    ):
        raise MessageDeliveryError(
            f"delivery {locked.pk} cannot send before its scheduled time"
        )
    locked.status = status
    update_fields = {"status", "updated_at"}
    if status == OutboundDelivery.Status.SENT:
        locked.sent_at = locked.sent_at or timezone.now()
        update_fields.add("sent_at")
    locked.save(update_fields=update_fields)
    delivery.status = locked.status
    delivery.sent_at = locked.sent_at
    return delivery


def delivery_for_task(
    *,
    task: Task,
    deal,
    channel: str,
    operator: str,
) -> OutboundDelivery:
    """Load and validate the exact frozen delivery named by a Task payload."""
    payload = task.payload if isinstance(task.payload, dict) else {}
    delivery_id = payload.get("delivery_id")
    if isinstance(delivery_id, bool) or not isinstance(delivery_id, int) or delivery_id <= 0:
        raise MessageDeliveryError("bound outbound Task has no valid delivery_id")
    delivery = (
        OutboundDelivery.objects.select_related(
            "enrollment__deal__campaign",
            "enrollment__deal__lead",
            "enrollment__message_version__program",
        )
        .filter(pk=delivery_id)
        .first()
    )
    if delivery is None:
        raise MessageDeliveryError(f"outbound delivery {delivery_id} does not exist")
    enrollment = delivery.enrollment
    canonical_operator = resolve_operator(operator)
    if payload.get("campaign_id") != deal.campaign_id:
        raise MessageDeliveryError(
            f"Task {task.pk} campaign does not match Deal {deal.pk}"
        )
    if resolve_operator(payload.get("operator")) != canonical_operator:
        raise MessageDeliveryError(
            f"Task {task.pk} operator does not match its executing sender"
        )
    if payload.get("channel") != channel:
        raise MessageDeliveryError(
            f"Task {task.pk} channel does not match its frozen delivery"
        )
    expected = {
        "deal_id": deal.pk,
        "channel": channel,
        "operator": canonical_operator,
        "message_version_id": enrollment.message_version_id,
        "message_enrollment_id": enrollment.pk,
        "audience_key": enrollment.audience_key,
        "step_key": delivery.step_key,
        "step_index": delivery.step_index,
        "variant_key": delivery.variant_key,
    }
    actual = {
        "deal_id": enrollment.deal_id,
        "channel": delivery.channel,
        "operator": delivery.operator,
        "message_version_id": payload.get("message_version_id"),
        "message_enrollment_id": payload.get("message_enrollment_id"),
        "audience_key": payload.get("audience_key"),
        "step_key": payload.get("step_key"),
        "step_index": payload.get("step_index"),
        "variant_key": payload.get("variant_key"),
    }
    if actual != expected:
        raise MessageDeliveryError(
            f"Task {task.pk} delivery binding mismatch: expected {expected!r}, got {actual!r}"
        )
    if delivery.task_id not in {None, task.pk}:
        raise MessageDeliveryError(
            f"delivery {delivery.pk} is bound to Task {delivery.task_id}, not {task.pk}"
        )
    if delivery.status in {
        OutboundDelivery.Status.UNCLEAR,
        OutboundDelivery.Status.STOPPED,
    }:
        raise MessageDeliveryError(
            f"delivery {delivery.pk} status {delivery.status!r} is not executable"
        )
    return delivery


def task_payload_for_delivery(delivery: OutboundDelivery) -> dict[str, object]:
    """Return the immutable routing identity every bound Task must carry."""
    enrollment = delivery.enrollment
    return {
        "delivery_id": delivery.pk,
        "message_enrollment_id": enrollment.pk,
        "message_version_id": enrollment.message_version_id,
        "audience_key": enrollment.audience_key,
        "step_key": delivery.step_key,
        "step_index": delivery.step_index,
        "variant_key": delivery.variant_key,
    }
