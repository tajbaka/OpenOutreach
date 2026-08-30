from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from drip.models import (
    NONTERMINAL_ENROLLMENT_STATUSES,
    NONTERMINAL_LANE_STATUSES,
    DripDelivery,
    DripEnrollment,
    DripLane,
)


@transaction.atomic
def stop_enrollment_for_reason(
    enrollment_id: int,
    *,
    reason: str,
    now=None,
) -> bool:
    """Stop one nonterminal enrollment from a shared persisted stop reason."""
    from linkedin.models import Task

    from drip.services.ownership import lock_enrollment_graph

    enrollment = lock_enrollment_graph(enrollment_id)
    if enrollment.status not in NONTERMINAL_ENROLLMENT_STATUSES:
        return False
    stopped_at = now or timezone.now()
    enrollment.status = DripEnrollment.Status.STOPPED
    enrollment.stopped_at = stopped_at
    enrollment.stop_reason = "shared_stop"
    enrollment.stop_detail = reason
    enrollment.save(
        update_fields={
            "status",
            "stopped_at",
            "stop_reason",
            "stop_detail",
            "updated_at",
        },
    )
    lane_ids = list(
        DripLane.objects.select_for_update().filter(
            enrollment=enrollment,
            status__in=NONTERMINAL_LANE_STATUSES,
        ).values_list("pk", flat=True),
    )
    DripLane.objects.filter(pk__in=lane_ids).update(
        status=DripLane.Status.STOPPED,
        updated_at=stopped_at,
    )
    deliveries = DripDelivery.objects.select_for_update().filter(
        lane_id__in=lane_ids,
        status__in=(DripDelivery.Status.PLANNED, DripDelivery.Status.QUEUED),
    )
    task_ids = list(
        deliveries.exclude(current_task_id=None).values_list("current_task_id", flat=True),
    )
    deliveries.update(status=DripDelivery.Status.STOPPED, updated_at=stopped_at)
    Task.objects.filter(
        pk__in=task_ids,
        status=Task.Status.PENDING,
    ).update(
        status=Task.Status.COMPLETED,
        completed_at=stopped_at,
        error=reason,
    )
    return True


@transaction.atomic
def stop_for_inbound_message(message_id: int) -> int:
    """Stop drip state for one newly persisted human inbound Message.

    Ingestion owners call this after their Message transaction commits. The
    function is deliberately idempotent: a duplicate listener/backfill event
    finds no nonterminal enrollment on the second invocation.
    """
    from crm.models import Message
    from linkedin.models import Task

    message = Message.objects.filter(pk=message_id).only(
        "id",
        "lead_id",
        "source",
        "direction",
    ).first()
    if message is None:
        return 0
    if message.direction != Message.Direction.INBOUND:
        return 0
    if message.source not in {Message.Source.LINKEDIN, Message.Source.GMAIL}:
        return 0

    from drip.services.ownership import lock_lead_outbound_ownership

    lock_lead_outbound_ownership(message.lead_id)

    enrollments = list(
        DripEnrollment.objects.select_for_update().filter(
            lead_id=message.lead_id,
            status__in=NONTERMINAL_ENROLLMENT_STATUSES,
        ),
    )
    if not enrollments:
        return 0

    now = timezone.now()
    enrollment_ids = [enrollment.pk for enrollment in enrollments]
    detail = f"Inbound {message.source} Message {message.pk} persisted"
    DripEnrollment.objects.filter(pk__in=enrollment_ids).update(
        status=DripEnrollment.Status.STOPPED,
        stopped_at=now,
        stop_reason=f"inbound_{message.source}",
        stop_detail=detail,
        stop_trigger_message_id=message.pk,
        updated_at=now,
    )
    lane_ids = list(
        DripLane.objects.select_for_update().filter(
            enrollment_id__in=enrollment_ids,
            status__in=NONTERMINAL_LANE_STATUSES,
        ).values_list("pk", flat=True),
    )
    if lane_ids:
        DripLane.objects.filter(pk__in=lane_ids).update(
            status=DripLane.Status.STOPPED,
            updated_at=now,
        )
    delivery_qs = DripDelivery.objects.select_for_update().filter(
        lane_id__in=lane_ids,
        status__in=(DripDelivery.Status.PLANNED, DripDelivery.Status.QUEUED),
    )
    pending_task_ids = list(
        delivery_qs.exclude(current_task_id=None).values_list("current_task_id", flat=True),
    )
    delivery_qs.update(status=DripDelivery.Status.STOPPED, updated_at=now)
    if pending_task_ids:
        Task.objects.filter(
            pk__in=pending_task_ids,
            status=Task.Status.PENDING,
        ).update(
            status=Task.Status.COMPLETED,
            completed_at=now,
            error=detail,
        )
    return len(enrollments)
