from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.db import connection

from drip.exceptions import ReconciliationBusy


# Stable signed bigint used only by PostgreSQL transaction advisory locks.
DRIP_RECONCILE_LOCK_ID = 7_341_170_887_122_601


@dataclass(frozen=True)
class LockedDeliveryGraph:
    lead: Any
    enrollment: Any
    lane: Any
    delivery: Any
    attempt: Any = None
    task: Any = None


def acquire_reconciliation_lock() -> None:
    """Acquire one transaction-scoped global lock for a reconciliation pass."""
    if connection.vendor != "postgresql":
        return
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_try_advisory_xact_lock(%s)",
            [DRIP_RECONCILE_LOCK_ID],
        )
        acquired = cursor.fetchone()[0]
    if not acquired:
        raise ReconciliationBusy("Another drip reconciliation pass is already running.")


def lock_lead_outbound_ownership(lead_id: int):
    """Lock the narrow row shared by channel ownership decisions.

    Callers must already be inside ``transaction.atomic()``. Current-flow
    enqueue guards can adopt this same helper when their shared seam lands.
    """
    from crm.models import Lead

    return Lead.objects.select_for_update().get(pk=lead_id)


def drip_owns_channel(*, lead_id: int, channel: str) -> bool:
    """Whether a committed drip handoff permanently owns one Lead channel.

    Completion, pause, or a terminal stop never hands the channel back to the
    current sequence. This is the narrow rollback contract that prevents old
    healing/enqueue paths from restarting after drip has taken ownership.
    Callers that are about to create current work must first lock the Lead with
    :func:`lock_lead_outbound_ownership` in the same transaction.
    """
    from drip.models import DripLane

    if channel not in DripLane.Channel.values:
        raise ValueError(f"Unknown outbound ownership channel: {channel!r}")
    return DripLane.objects.filter(
        enrollment__lead_id=lead_id,
        channel=channel,
        handed_off_at__isnull=False,
    ).exists()


def lock_enrollment_graph(enrollment_id: int):
    """Lock Lead then Enrollment, the global drip lock order prefix."""
    from drip.models import DripEnrollment

    lead_id = DripEnrollment.objects.values_list("lead_id", flat=True).get(
        pk=enrollment_id,
    )
    lock_lead_outbound_ownership(lead_id)
    return (
        DripEnrollment.objects.select_for_update(of=("self",))
        .select_related("lead", "campaign", "campaign_version")
        .get(pk=enrollment_id)
    )


def lock_lane_graph(lane_id: int):
    """Lock Lead, Enrollment, then Lane and return the latter two objects."""
    from drip.models import DripLane

    enrollment_id = DripLane.objects.values_list("enrollment_id", flat=True).get(
        pk=lane_id,
    )
    enrollment = lock_enrollment_graph(enrollment_id)
    lane = DripLane.objects.select_for_update(of=("self",)).get(pk=lane_id)
    if lane.enrollment_id != enrollment.pk:
        raise ValueError("Drip lane ownership graph changed while locking")
    return enrollment, lane


def lock_delivery_graph(
    delivery_id: int,
    *,
    attempt_id: int | None = None,
    task_id: int | None = None,
) -> LockedDeliveryGraph:
    """Lock one runtime graph in Lead->Enrollment->Lane->Delivery->Attempt->Task order."""
    from drip.models import DripDelivery, DripDeliveryAttempt, DripLane

    metadata = DripDelivery.objects.values(
        "lane_id",
        "lane__enrollment_id",
    ).get(pk=delivery_id)
    enrollment = lock_enrollment_graph(metadata["lane__enrollment_id"])
    lane = DripLane.objects.select_for_update(of=("self",)).get(
        pk=metadata["lane_id"],
    )
    delivery = DripDelivery.objects.select_for_update(of=("self",)).get(
        pk=delivery_id,
    )
    if lane.enrollment_id != enrollment.pk or delivery.lane_id != lane.pk:
        raise ValueError("Drip delivery ownership graph changed while locking")

    attempt = None
    if attempt_id is not None:
        attempt = DripDeliveryAttempt.objects.select_for_update(of=("self",)).get(
            pk=attempt_id,
            delivery_id=delivery.pk,
        )

    task = None
    if task_id is not None:
        from linkedin.models import Task

        task = Task.objects.select_for_update(of=("self",)).get(pk=task_id)
    return LockedDeliveryGraph(
        lead=enrollment.lead,
        enrollment=enrollment,
        lane=lane,
        delivery=delivery,
        attempt=attempt,
        task=task,
    )
