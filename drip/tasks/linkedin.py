"""Fail-closed execution for one materialized LinkedIn drip delivery.

The reconciler decides what work is due and links one ``Task`` to a frozen
``DripDelivery``.  This module owns only the browser mutation boundary and the
resulting ledger transitions.  It deliberately does not refresh a LinkedIn
conversation, alter the current outreach ``Deal``, enqueue another channel, or
fall back to a second send route.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Any

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from drip.models import (
    NONTERMINAL_LANE_STATUSES,
    DripCampaign,
    DripDelivery,
    DripDeliveryAttempt,
    DripEnrollment,
    DripLane,
)
from linkedin.actions.message import (
    DirectMessageOutcome,
    MessageSubmissionAborted,
    send_direct_message_once,
)
from linkedin.enums import ProfileState
from linkedin.models import ActionLog, Campaign, Task
from linkedin.operators import resolve_operator


logger = logging.getLogger(__name__)


class StaleRecoveryResult(StrEnum):
    NOOP = "noop"
    PLANNED = "planned"
    REQUEUED = "requeued"
    UNCLEAR = "unclear"


@dataclass(frozen=True)
class _Reservation:
    delivery_id: int
    attempt_id: int
    public_identifier: str
    body: str
    action_campaign_id: int


@dataclass(frozen=True)
class _GuardFailure:
    detail: str
    retryable: bool = False
    waiting_connection: bool = False
    hold: bool = False


def _payload_delivery_id(task: Task) -> int:
    raw_delivery_id = (task.payload or {}).get("delivery_id")
    if isinstance(raw_delivery_id, bool):
        raise ValueError("drip_linkedin task delivery_id must be an integer")
    try:
        delivery_id = int(raw_delivery_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("drip_linkedin task delivery_id must be an integer") from exc
    if delivery_id <= 0:
        raise ValueError("drip_linkedin task delivery_id must be positive")
    return delivery_id


def _session_operator(session) -> str:
    return resolve_operator(session.linkedin_profile.linkedin_username)


def _current_linkedin_identity(lead) -> str:
    from linkedin.notifications.sheets import linkedin_identity_key

    identity = linkedin_identity_key(lead.linkedin_url or "")
    if identity:
        return identity
    public_identifier = (lead.public_identifier or "").strip().lower()
    return f"public:{public_identifier}" if public_identifier else ""


def _public_identifier(lead) -> str:
    from linkedin.db.urls import url_to_public_id

    return (lead.public_identifier or "").strip() or (
        url_to_public_id(lead.linkedin_url or "") or ""
    )


def _active_action_campaign(session, operator: str) -> Campaign | None:
    campaign = getattr(session, "campaign", None)
    profile = getattr(session, "linkedin_profile", None)
    if campaign is None or profile is None or not getattr(profile, "active", False):
        return None
    if resolve_operator(profile.linkedin_username) != operator:
        return None
    return Campaign.objects.filter(
        pk=campaign.pk,
        status=Campaign.Status.ACTIVE,
        user_id=profile.user_id,
    ).first()


def _deal_proves_connection(lane: DripLane) -> bool:
    """Require a connected Deal owned by this lane without mutating it."""
    from crm.models import Deal

    allowed_states = {ProfileState.CONNECTED}
    if lane.current_sequence_status == DripLane.CurrentSequenceStatus.COMPLETED:
        allowed_states.add(ProfileState.COMPLETED)

    deals = Deal.objects.filter(
        lead_id=lane.enrollment.lead_id,
        state__in=allowed_states,
    ).select_related("campaign__user")
    for deal in deals:
        evidence = {
            resolve_operator(value)
            for value in (deal.invitation_sender, deal.campaign.user.username)
            if value
        }
        if lane.operator in evidence:
            return True
    return False


def _manifest_themes(enrollment: DripEnrollment) -> list[dict[str, Any]]:
    try:
        return enrollment.campaign_version.manifest["audiences"][
            enrollment.frozen_icp
        ]["themes"]
    except (KeyError, TypeError) as exc:
        raise ValueError(
            f"Drip enrollment {enrollment.pk} has invalid frozen manifest routing",
        ) from exc


def _linkedin_steps(
    enrollment: DripEnrollment,
    *,
    theme_index: int,
    operator: str,
) -> list[dict[str, Any]] | None:
    themes = _manifest_themes(enrollment)
    if theme_index < 0 or theme_index >= len(themes):
        return None
    rendition = themes[theme_index].get("senders", {}).get(operator, {})
    steps = rendition.get(DripLane.Channel.LINKEDIN)
    return steps if isinstance(steps, list) and steps else None


def _predecessors_and_timing_ready(
    delivery: DripDelivery,
    lane: DripLane,
    enrollment: DripEnrollment,
    *,
    now,
) -> str:
    """Return a reason when the exact manifest predecessor/timing is not ready."""
    themes = _manifest_themes(enrollment)
    if delivery.theme_index >= len(themes):
        return "delivery theme index is outside the frozen manifest"
    theme = themes[delivery.theme_index]
    if theme.get("key") != delivery.theme_key:
        return "delivery theme key does not match the frozen manifest"
    if (
        lane.current_theme_index != delivery.theme_index
        or lane.current_theme_key != delivery.theme_key
    ):
        return "delivery is not the lane's exact current theme"

    steps = _linkedin_steps(
        enrollment,
        theme_index=delivery.theme_index,
        operator=lane.operator,
    )
    if steps is None or delivery.step_index >= len(steps):
        return "delivery step is not in the frozen LinkedIn rendition"

    # A later theme cannot start merely because its calendar date has passed.
    # Every configured step in every earlier LinkedIn theme must be durably sent.
    existing = {
        (candidate.theme_index, candidate.step_index): candidate
        for candidate in DripDelivery.objects.filter(
            lane=lane,
            theme_index__lte=delivery.theme_index,
        )
    }
    for earlier_theme_index in range(delivery.theme_index):
        earlier_steps = _linkedin_steps(
            enrollment,
            theme_index=earlier_theme_index,
            operator=lane.operator,
        )
        if earlier_steps is None:
            continue
        for earlier_step_index in range(len(earlier_steps)):
            predecessor = existing.get((earlier_theme_index, earlier_step_index))
            if (
                predecessor is None
                or predecessor.status != DripDelivery.Status.SENT
                or predecessor.sent_at is None
            ):
                return "an earlier LinkedIn theme is not complete"

    for earlier_step_index in range(delivery.step_index):
        predecessor = existing.get((delivery.theme_index, earlier_step_index))
        if (
            predecessor is None
            or predecessor.status != DripDelivery.Status.SENT
            or predecessor.sent_at is None
        ):
            return "the exact prior LinkedIn step is not sent"

    delay = timedelta(days=float(steps[delivery.step_index]["delay_days"]))
    if delivery.step_index == 0:
        if lane.theme_started_at is None:
            return "lane theme start time is missing"
        manifest_due_at = lane.theme_started_at + delay
    else:
        predecessor = existing[(delivery.theme_index, delivery.step_index - 1)]
        manifest_due_at = predecessor.sent_at + delay

    if delivery.scheduled_at > now or manifest_due_at > now:
        return "delivery is not due from its previous successful send"
    return ""


def _execution_guard(
    *,
    task: Task,
    delivery: DripDelivery,
    lane: DripLane,
    enrollment: DripEnrollment,
    session,
    now,
    expected_delivery_status: str,
) -> _GuardFailure | None:
    if task.task_type != Task.TaskType.DRIP_LINKEDIN:
        raise ValueError(f"Task {task.pk} is not a drip_linkedin task")
    if task.status != Task.Status.RUNNING:
        raise ValueError(f"drip_linkedin Task {task.pk} was not claimed")
    if delivery.current_task_id != task.pk:
        raise ValueError(
            f"Drip delivery {delivery.pk} is not linked to claimed Task {task.pk}",
        )
    if delivery.status != expected_delivery_status:
        return _GuardFailure(
            f"delivery state changed to {delivery.status}",
        )

    payload_operator = (task.payload or {}).get("operator")
    session_operator = _session_operator(session)
    if payload_operator != lane.operator or session_operator != lane.operator:
        return _GuardFailure(
            "frozen LinkedIn operator does not match the claimed task and session",
        )
    if (
        delivery.provider_account != lane.provider_account
        or lane.provider_account != lane.operator.casefold()
        or lane.sender_identity != lane.operator.casefold()
    ):
        return _GuardFailure("frozen LinkedIn sender ownership drifted")
    if lane.recipient_identity != _current_linkedin_identity(enrollment.lead):
        return _GuardFailure("frozen LinkedIn recipient identity drifted")
    if not _public_identifier(enrollment.lead):
        return _GuardFailure("lead has no LinkedIn public identifier")

    if enrollment.campaign.status != DripCampaign.Status.ACTIVE:
        return _GuardFailure("drip campaign is not active", hold=True)
    if enrollment.status != DripEnrollment.Status.ACTIVE:
        return _GuardFailure(f"drip enrollment is {enrollment.status}", hold=True)
    if lane.status != DripLane.Status.ACTIVE:
        return _GuardFailure(f"LinkedIn lane is {lane.status}", hold=True)
    if lane.channel != DripLane.Channel.LINKEDIN:
        raise ValueError(f"Drip delivery {delivery.pk} does not belong to LinkedIn")
    if lane.handed_off_at is None:
        return _GuardFailure("LinkedIn lane has not been handed off")
    if lane.current_sequence_status not in {
        DripLane.CurrentSequenceStatus.COMPLETED,
        DripLane.CurrentSequenceStatus.NOT_APPLICABLE,
    }:
        return _GuardFailure("current LinkedIn sequence is not complete")
    if not delivery.frozen_body.strip():
        raise ValueError(f"Drip delivery {delivery.pk} has an empty frozen body")

    if _active_action_campaign(session, lane.operator) is None:
        return _GuardFailure(
            "no active sender-owned outreach Campaign is available",
            hold=True,
        )
    if not _deal_proves_connection(lane):
        return _GuardFailure(
            "no sender-owned connected Deal proves LinkedIn messaging eligibility",
            waiting_connection=True,
        )

    timing_reason = _predecessors_and_timing_ready(
        delivery,
        lane,
        enrollment,
        now=now,
    )
    if timing_reason:
        return _GuardFailure(timing_reason, retryable=True)
    if not session.linkedin_profile.can_execute(ActionLog.ActionType.FOLLOW_UP):
        return _GuardFailure("LinkedIn follow-up quota is exhausted", retryable=True)
    return None


def _stop_code(reason: str) -> str:
    if reason.startswith("Lead disqualified"):
        return "lead_disqualified"
    if reason.startswith("Suppression:"):
        return "outreach_suppressed"
    if reason.startswith("Meeting exists"):
        return "persisted_meeting"
    if reason.startswith("Lead replied"):
        return "inbound_reply"
    return "automation_stop"


def _stop_enrollment_locked(
    enrollment: DripEnrollment,
    *,
    reason: str,
    now,
) -> None:
    """Apply a DB-local global stop, including sibling-channel queued work."""
    enrollment.status = DripEnrollment.Status.STOPPED
    enrollment.stopped_at = now
    enrollment.stop_reason = _stop_code(reason)
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

    # Lock every lane, including one already stopped by the inbound on-commit
    # hook.  That hook can race a SENDING delivery and intentionally does not
    # retire it; the pre-click guard below must still reach that delivery.
    lanes = list(
        DripLane.objects.select_for_update().filter(enrollment=enrollment),
    )
    lane_ids = [candidate.pk for candidate in lanes]
    if lane_ids:
        DripLane.objects.filter(
            pk__in=lane_ids,
            status__in=NONTERMINAL_LANE_STATUSES,
        ).update(
            status=DripLane.Status.STOPPED,
            updated_at=now,
        )
    deliveries = DripDelivery.objects.select_for_update().filter(
        lane_id__in=lane_ids,
        status__in={
            DripDelivery.Status.PLANNED,
            DripDelivery.Status.QUEUED,
            DripDelivery.Status.SENDING,
        },
    )
    pending_task_ids = list(
        deliveries.exclude(current_task_id=None).values_list("current_task_id", flat=True),
    )
    deliveries.update(status=DripDelivery.Status.STOPPED, updated_at=now)
    if pending_task_ids:
        Task.objects.filter(
            pk__in=pending_task_ids,
            status=Task.Status.PENDING,
        ).update(
            status=Task.Status.COMPLETED,
            completed_at=now,
            error=reason,
        )


def _release_for_guard_locked(
    delivery: DripDelivery,
    lane: DripLane,
    failure: _GuardFailure,
    *,
    now,
) -> None:
    if (
        lane.status in {DripLane.Status.STOPPED, DripLane.Status.COMPLETED}
        or lane.enrollment.status
        in {DripEnrollment.Status.STOPPED, DripEnrollment.Status.COMPLETED}
    ):
        delivery.status = DripDelivery.Status.STOPPED
    elif failure.waiting_connection:
        lane.status = DripLane.Status.WAITING_CONNECTION
        lane.save(update_fields={"status", "updated_at"})
        delivery.status = DripDelivery.Status.PLANNED
    elif failure.retryable:
        delivery.status = DripDelivery.Status.PLANNED
    elif failure.hold:
        delivery.status = DripDelivery.Status.PLANNED
    else:
        lane.status = DripLane.Status.PAUSED
        lane.save(update_fields={"status", "updated_at"})
        delivery.status = DripDelivery.Status.PLANNED
    delivery.current_task = None
    delivery.save(update_fields={"status", "current_task", "updated_at"})
    logger.warning("drip_linkedin delivery %s blocked: %s", delivery.pk, failure.detail)


@transaction.atomic
def _reserve_delivery(task: Task, session) -> _Reservation | None:
    from drip.services.ownership import lock_delivery_graph

    now = timezone.now()
    delivery_id = _payload_delivery_id(task)
    graph = lock_delivery_graph(delivery_id, task_id=task.pk)
    locked_task = graph.task
    delivery = graph.delivery
    lane = graph.lane
    enrollment = graph.enrollment

    from linkedin.tasks.stop_checks import lead_automation_stop_reason

    stop_reason = lead_automation_stop_reason(enrollment.lead)
    if stop_reason:
        _stop_enrollment_locked(enrollment, reason=stop_reason, now=now)
        return None

    failure = _execution_guard(
        task=locked_task,
        delivery=delivery,
        lane=lane,
        enrollment=enrollment,
        session=session,
        now=now,
        expected_delivery_status=DripDelivery.Status.QUEUED,
    )
    if failure:
        _release_for_guard_locked(delivery, lane, failure, now=now)
        return None

    open_attempt = delivery.attempts.filter(
        outcome=DripDeliveryAttempt.Outcome.RESERVED,
        finished_at=None,
    ).first()
    if open_attempt is not None:
        raise ValueError(
            f"Drip delivery {delivery.pk} already has unfinished attempt {open_attempt.pk}",
        )
    highest_attempt = delivery.attempts.aggregate(number=Max("attempt_number"))["number"] or 0
    attempt = DripDeliveryAttempt.objects.create(
        delivery=delivery,
        attempt_number=highest_attempt + 1,
        outcome=DripDeliveryAttempt.Outcome.RESERVED,
    )
    delivery.status = DripDelivery.Status.SENDING
    delivery.save(update_fields={"status", "updated_at"})
    action_campaign = _active_action_campaign(session, lane.operator)
    if action_campaign is None:  # Guarded above; fail if DB state changed inside this transaction.
        raise ValueError("active sender-owned outreach Campaign disappeared during reservation")
    return _Reservation(
        delivery_id=delivery.pk,
        attempt_id=attempt.pk,
        public_identifier=_public_identifier(enrollment.lead),
        body=delivery.frozen_body,
        action_campaign_id=action_campaign.pk,
    )


def _mark_attempt_not_submitted(
    attempt: DripDeliveryAttempt,
    *,
    detail: str,
    now,
) -> None:
    attempt.outcome = DripDeliveryAttempt.Outcome.NOT_SUBMITTED
    attempt.finished_at = now
    attempt.diagnostic_detail = detail
    attempt.save(update_fields={"outcome", "finished_at", "diagnostic_detail"})


def _submission_callback(
    *,
    task_id: int,
    reservation: _Reservation,
    session,
) -> None:
    abort_reason = ""
    with transaction.atomic():
        from drip.services.ownership import lock_delivery_graph

        now = timezone.now()
        graph = lock_delivery_graph(
            reservation.delivery_id,
            attempt_id=reservation.attempt_id,
            task_id=task_id,
        )
        task = graph.task
        delivery = graph.delivery
        lane = graph.lane
        enrollment = graph.enrollment
        attempt = graph.attempt
        if (
            attempt.outcome != DripDeliveryAttempt.Outcome.RESERVED
            or attempt.finished_at is not None
            or attempt.submission_attempted_at is not None
        ):
            abort_reason = "delivery attempt is no longer reserved at submit boundary"
        else:
            from linkedin.tasks.stop_checks import lead_automation_stop_reason

            stop_reason = lead_automation_stop_reason(enrollment.lead)
            if stop_reason:
                _mark_attempt_not_submitted(attempt, detail=stop_reason, now=now)
                _stop_enrollment_locked(enrollment, reason=stop_reason, now=now)
                abort_reason = stop_reason
            else:
                failure = _execution_guard(
                    task=task,
                    delivery=delivery,
                    lane=lane,
                    enrollment=enrollment,
                    session=session,
                    now=now,
                    expected_delivery_status=DripDelivery.Status.SENDING,
                )
                if failure:
                    _mark_attempt_not_submitted(attempt, detail=failure.detail, now=now)
                    _release_for_guard_locked(delivery, lane, failure, now=now)
                    abort_reason = failure.detail
                else:
                    attempt.submission_attempted_at = now
                    attempt.save(update_fields={"submission_attempted_at"})

    if abort_reason:
        raise MessageSubmissionAborted(abort_reason)


def _advance_lane_after_success(
    lane: DripLane,
    enrollment: DripEnrollment,
    delivery: DripDelivery,
    *,
    sent_at,
) -> None:
    steps = _linkedin_steps(
        enrollment,
        theme_index=delivery.theme_index,
        operator=lane.operator,
    )
    if steps is None or delivery.step_index >= len(steps):
        raise ValueError("sent delivery is outside the frozen LinkedIn rendition")
    if delivery.step_index < len(steps) - 1:
        return

    themes = _manifest_themes(enrollment)
    for next_theme_index in range(delivery.theme_index + 1, len(themes)):
        if _linkedin_steps(
            enrollment,
            theme_index=next_theme_index,
            operator=lane.operator,
        ) is None:
            continue
        lane.status = DripLane.Status.ACTIVE
        lane.current_theme_index = next_theme_index
        lane.current_theme_key = themes[next_theme_index]["key"]
        lane.theme_started_at = sent_at
        lane.save(
            update_fields={
                "status",
                "current_theme_index",
                "current_theme_key",
                "theme_started_at",
                "updated_at",
            },
        )
        return

    lane.status = DripLane.Status.COMPLETED
    lane.current_theme_index = len(themes)
    lane.current_theme_key = ""
    lane.theme_started_at = sent_at
    lane.save(
        update_fields={
            "status",
            "current_theme_index",
            "current_theme_key",
            "theme_started_at",
            "updated_at",
        },
    )


@transaction.atomic
def _finish_sent(reservation: _Reservation, task_id: int, session) -> None:
    from crm.models import Message, SalesOwner
    from drip.services.ownership import lock_delivery_graph

    now = timezone.now()
    graph = lock_delivery_graph(
        reservation.delivery_id,
        attempt_id=reservation.attempt_id,
        task_id=task_id,
    )
    delivery = graph.delivery
    lane = graph.lane
    enrollment = graph.enrollment
    attempt = graph.attempt
    if delivery.status == DripDelivery.Status.SENT:
        return
    if (
        delivery.status != DripDelivery.Status.SENDING
        or delivery.current_task_id != task_id
        or attempt.outcome != DripDeliveryAttempt.Outcome.RESERVED
        or attempt.finished_at is not None
    ):
        raise ValueError("drip LinkedIn send confirmation no longer owns the delivery")
    if attempt.submission_attempted_at is None:
        attempt.outcome = DripDeliveryAttempt.Outcome.UNCLEAR
        attempt.finished_at = now
        attempt.diagnostic_detail = "send reported success without a committed submit boundary"
        attempt.save(update_fields={"outcome", "finished_at", "diagnostic_detail"})
        delivery.status = DripDelivery.Status.UNCLEAR
        delivery.save(update_fields={"status", "updated_at"})
        lane.status = DripLane.Status.PAUSED
        lane.save(update_fields={"status", "updated_at"})
        return

    owner = SalesOwner.objects.filter(
        normalized_handle=lane.operator.casefold(),
        active=True,
    ).first()
    external_id = f"drip-linkedin:{delivery.pk}"
    message, created = Message.objects.get_or_create(
        source=Message.Source.LINKEDIN,
        external_id=external_id,
        defaults={
            "lead": enrollment.lead,
            "operator": owner,
            "direction": Message.Direction.OUTBOUND,
            "sender": lane.operator,
            "body": delivery.frozen_body,
            "sent_at": now,
            "raw": {
                "kind": "drip_linkedin",
                "delivery_id": delivery.pk,
                "attempt_id": attempt.pk,
                "task_id": task_id,
                "operator": lane.operator,
                "action_campaign_id": reservation.action_campaign_id,
            },
        },
    )
    if not created and (
        message.lead_id != enrollment.lead_id
        or message.direction != Message.Direction.OUTBOUND
        or message.body != delivery.frozen_body
    ):
        raise ValueError(f"drip Message identity collision for delivery {delivery.pk}")

    action_campaign = Campaign.objects.get(pk=reservation.action_campaign_id)
    session.linkedin_profile.record_action(
        ActionLog.ActionType.FOLLOW_UP,
        action_campaign,
    )
    attempt.outcome = DripDeliveryAttempt.Outcome.SENT
    attempt.finished_at = now
    attempt.diagnostic_detail = ""
    attempt.save(update_fields={"outcome", "finished_at", "diagnostic_detail"})
    delivery.status = DripDelivery.Status.SENT
    delivery.sent_at = now
    delivery.outbound_message = message
    delivery.save(
        update_fields={"status", "sent_at", "outbound_message", "updated_at"},
    )
    # A listener/backfill stop can commit after the pre-click callback.  The
    # confirmed outbound is still recorded, but a stopped lane must never be
    # reactivated by its successful finalization.
    from linkedin.tasks.stop_checks import lead_automation_stop_reason

    stop_reason = lead_automation_stop_reason(enrollment.lead)
    if stop_reason:
        _stop_enrollment_locked(enrollment, reason=stop_reason, now=now)
    elif (
        enrollment.status == DripEnrollment.Status.ACTIVE
        and lane.status == DripLane.Status.ACTIVE
    ):
        _advance_lane_after_success(lane, enrollment, delivery, sent_at=now)


@transaction.atomic
def _finish_pre_submit_failure(reservation: _Reservation, detail: str) -> None:
    from drip.services.ownership import lock_delivery_graph

    now = timezone.now()
    graph = lock_delivery_graph(
        reservation.delivery_id,
        attempt_id=reservation.attempt_id,
    )
    delivery = graph.delivery
    lane = graph.lane
    attempt = graph.attempt
    if attempt.outcome != DripDeliveryAttempt.Outcome.RESERVED or attempt.finished_at is not None:
        return
    if attempt.submission_attempted_at is not None:
        _finish_unclear_locked(
            delivery,
            lane,
            attempt,
            detail="pre-submit failure reported after submit boundary: " + detail,
            now=now,
        )
        return
    _mark_attempt_not_submitted(attempt, detail=detail, now=now)
    delivery.status = DripDelivery.Status.PLANNED
    delivery.current_task = None
    delivery.save(update_fields={"status", "current_task", "updated_at"})


def _finish_unclear_locked(
    delivery: DripDelivery,
    lane: DripLane,
    attempt: DripDeliveryAttempt,
    *,
    detail: str,
    now,
) -> None:
    attempt.outcome = DripDeliveryAttempt.Outcome.UNCLEAR
    attempt.finished_at = now
    attempt.diagnostic_detail = detail
    attempt.save(update_fields={"outcome", "finished_at", "diagnostic_detail"})
    delivery.status = DripDelivery.Status.UNCLEAR
    delivery.save(update_fields={"status", "updated_at"})
    if lane.status in NONTERMINAL_LANE_STATUSES:
        lane.status = DripLane.Status.PAUSED
        lane.save(update_fields={"status", "updated_at"})


@transaction.atomic
def _finish_unclear(reservation: _Reservation, detail: str) -> None:
    from drip.services.ownership import lock_delivery_graph

    now = timezone.now()
    graph = lock_delivery_graph(
        reservation.delivery_id,
        attempt_id=reservation.attempt_id,
    )
    delivery = graph.delivery
    attempt = graph.attempt
    if attempt.outcome != DripDeliveryAttempt.Outcome.RESERVED or attempt.finished_at is not None:
        return
    _finish_unclear_locked(
        delivery,
        graph.lane,
        attempt,
        detail=detail,
        now=now,
    )


def handle_drip_linkedin(task: Task, session, qualifiers=None) -> None:
    """Execute one already-claimed LinkedIn delivery through one UI route."""
    del qualifiers
    reservation = _reserve_delivery(task, session)
    if reservation is None:
        return

    try:
        result = send_direct_message_once(
            session,
            {"public_identifier": reservation.public_identifier},
            reservation.body,
            on_submit_attempt=lambda: _submission_callback(
                task_id=task.pk,
                reservation=reservation,
                session=session,
            ),
        )
    except Exception as exc:
        # The action primitive classifies expected UI failures. Preserve the
        # same duplicate-prevention guarantee if an unexpected exception
        # escapes it: the committed submit timestamp is the authority.
        attempt = DripDeliveryAttempt.objects.only("submission_attempted_at").get(
            pk=reservation.attempt_id,
        )
        detail = f"{type(exc).__name__}: {str(exc)[:900]}"
        if attempt.submission_attempted_at is None:
            _finish_pre_submit_failure(reservation, detail)
        else:
            _finish_unclear(reservation, detail)
        raise
    if result.outcome == DirectMessageOutcome.SENT:
        _finish_sent(reservation, task.pk, session)
    elif result.outcome == DirectMessageOutcome.PRE_SUBMIT_FAILED:
        _finish_pre_submit_failure(reservation, result.detail)
    elif result.outcome == DirectMessageOutcome.UNCLEAR:
        _finish_unclear(reservation, result.detail)
    else:  # pragma: no cover - a new action outcome must be handled explicitly.
        raise ValueError(f"Unknown direct-message result: {result.outcome!r}")


@transaction.atomic
def recover_stale_linkedin_delivery(delivery_id: int) -> StaleRecoveryResult:
    """Reconcile one stale SENDING delivery without risking a duplicate click.

    A committed submit-boundary timestamp is irreversible evidence that a click
    may have happened, so that case becomes UNCLEAR and pauses the lane.  An
    attempt that never crossed the boundary is definitely safe: its existing
    Task is returned to PENDING/QUEUED, or the delivery to PLANNED when no Task
    remains linked.
    """
    from drip.services.ownership import lock_delivery_graph

    now = timezone.now()
    # Lock the shared ownership graph before the attempt ledger or queue Task.
    # This is the same order used by reply stops and both channel executors.
    graph = lock_delivery_graph(delivery_id)
    delivery = graph.delivery
    lane = graph.lane
    if lane.channel != DripLane.Channel.LINKEDIN:
        raise ValueError(f"Drip delivery {delivery.pk} is not LinkedIn")
    if delivery.status != DripDelivery.Status.SENDING:
        return StaleRecoveryResult.NOOP

    attempts = list(
        DripDeliveryAttempt.objects.select_for_update().filter(
            delivery=delivery,
            outcome=DripDeliveryAttempt.Outcome.RESERVED,
            finished_at=None,
        ),
    )
    if len(attempts) != 1:
        # Missing or multiple reservations violate the atomic reservation
        # invariant.  With no trustworthy pre-click evidence, pause rather than
        # manufacture permission to resend.
        delivery.status = DripDelivery.Status.UNCLEAR
        delivery.save(update_fields={"status", "updated_at"})
        if lane.status in NONTERMINAL_LANE_STATUSES:
            lane.status = DripLane.Status.PAUSED
            lane.save(update_fields={"status", "updated_at"})
        _retire_recovered_task(delivery, now=now, detail="invalid stale attempt ledger")
        return StaleRecoveryResult.UNCLEAR

    attempt = attempts[0]
    if attempt.submission_attempted_at is not None:
        _finish_unclear_locked(
            delivery,
            lane,
            attempt,
            detail="stale worker after committed LinkedIn submit boundary",
            now=now,
        )
        _retire_recovered_task(
            delivery,
            now=now,
            detail="LinkedIn delivery paused: submission outcome unclear",
        )
        return StaleRecoveryResult.UNCLEAR

    _mark_attempt_not_submitted(
        attempt,
        detail="stale worker recovered before LinkedIn submit boundary",
        now=now,
    )
    controls_active = (
        graph.enrollment.campaign.status == DripCampaign.Status.ACTIVE
        and graph.enrollment.status == DripEnrollment.Status.ACTIVE
        and lane.status == DripLane.Status.ACTIVE
    )
    if not controls_active:
        task_id = delivery.current_task_id
        delivery.current_task = None
        delivery.status = DripDelivery.Status.PLANNED
        delivery.save(update_fields={"current_task", "status", "updated_at"})
        if task_id is not None:
            Task.objects.select_for_update().filter(
                pk=task_id,
                status__in={Task.Status.PENDING, Task.Status.RUNNING, Task.Status.FAILED},
            ).update(
                status=Task.Status.COMPLETED,
                completed_at=now,
                error="Drip controls inactive during stale recovery",
            )
        return StaleRecoveryResult.PLANNED
    if delivery.current_task_id is None:
        delivery.status = DripDelivery.Status.PLANNED
        delivery.save(update_fields={"status", "updated_at"})
        return StaleRecoveryResult.PLANNED

    task = Task.objects.select_for_update().get(pk=delivery.current_task_id)
    if task.status in {Task.Status.RUNNING, Task.Status.FAILED}:
        task.status = Task.Status.PENDING
        task.started_at = None
        task.completed_at = None
        task.error = ""
        task.save(update_fields={"status", "started_at", "completed_at", "error"})
    elif task.status != Task.Status.PENDING:
        delivery.current_task = None
        delivery.status = DripDelivery.Status.PLANNED
        delivery.save(update_fields={"current_task", "status", "updated_at"})
        return StaleRecoveryResult.PLANNED
    delivery.status = DripDelivery.Status.QUEUED
    delivery.save(update_fields={"status", "updated_at"})
    return StaleRecoveryResult.REQUEUED


def _retire_recovered_task(delivery: DripDelivery, *, now, detail: str) -> None:
    if delivery.current_task_id is None:
        return
    Task.objects.select_for_update().filter(
        pk=delivery.current_task_id,
        status__in={Task.Status.PENDING, Task.Status.RUNNING},
    ).update(
        status=Task.Status.COMPLETED,
        completed_at=now,
        error=detail,
    )


def recover_stale_linkedin_task(task_id: int) -> StaleRecoveryResult:
    """Task-oriented wrapper for daemon startup recovery."""
    task = Task.objects.get(pk=task_id, task_type=Task.TaskType.DRIP_LINKEDIN)
    return recover_stale_linkedin_delivery(_payload_delivery_id(task))
