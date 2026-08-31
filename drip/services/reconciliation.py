from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from django.db import transaction
from django.utils import timezone

from drip.exceptions import LinkAttributionError
from drip.link_attribution import (
    MAX_REFERENCE_GENERATION_ATTEMPTS,
    build_attributed_url,
    generate_reference,
)
from drip.manifest import render_template
from drip.models import (
    NONTERMINAL_ENROLLMENT_STATUSES,
    DripCampaign,
    DripDelivery,
    DripEnrollment,
    DripLane,
    DripTrackedLink,
)
from drip.services.handoff import evaluate_handoff
from drip.services.ownership import acquire_reconciliation_lock, lock_enrollment_graph
from drip.services.stops import stop_enrollment_for_reason


@dataclass(frozen=True)
class ReconcileDecision:
    enrollment_id: int
    lane_id: int | None
    channel: str
    action: str
    detail: str
    due_at: Any = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "enrollment_id": self.enrollment_id,
            "lane_id": self.lane_id,
            "channel": self.channel,
            "action": self.action,
            "detail": self.detail,
            "due_at": self.due_at.isoformat() if self.due_at else None,
        }


@dataclass(frozen=True)
class ReconcileResult:
    applied: bool
    decisions: tuple[ReconcileDecision, ...]
    counts: dict[str, int]
    workflow_run_id: int | None = None


@dataclass(frozen=True)
class _LaneOutcome:
    status: str
    decisions: tuple[ReconcileDecision, ...]
    stop_reason: str = ""


def _decision(lane: DripLane, action: str, detail: str, *, due_at=None) -> ReconcileDecision:
    return ReconcileDecision(
        enrollment_id=lane.enrollment_id,
        lane_id=lane.pk,
        channel=lane.channel,
        action=action,
        detail=detail,
        due_at=due_at,
    )


def _render_context(*, lane: DripLane) -> dict[str, str]:
    from gmail.auth import GMAIL_OPERATOR_MAPPING
    from linkedin.conf import OUR_COMPANY_NAME, OUR_WEBSITE_URL
    from linkedin.icp_outbound import safe_company_name
    from linkedin.name_utils import greeting_first_name

    lead = lane.enrollment.lead
    mapping = GMAIL_OPERATOR_MAPPING.get(lane.operator) or {}
    company_name = safe_company_name(lead.company_name or "") or "your team"
    return {
        "first_name": greeting_first_name(lead.first_name or ""),
        "last_name": (lead.last_name or "").strip(),
        "company_name": company_name,
        "my_name": mapping.get("display_name") or lane.operator,
        "our_company_name": OUR_COMPANY_NAME,
        "our_website_url": OUR_WEBSITE_URL,
    }


def _due_at(*, lane: DripLane, anchor, delay_days: float, now):
    if lane.channel == DripLane.Channel.GMAIL:
        return anchor + timedelta(days=float(delay_days))
    from linkedin.tasks.follow_up import _normalize_linkedin_due_at

    minimum_due_at = anchor + timedelta(days=float(delay_days))
    return _normalize_linkedin_due_at(
        minimum_due_at,
        current_time=now,
    )


def _render_step(
    *,
    lane: DripLane,
    rendition: list[dict[str, Any]],
    step_index: int,
    thread_subject: str,
    extra_context: dict[str, str] | None = None,
) -> tuple[str, str]:
    context = _render_context(lane=lane)
    if extra_context:
        context.update(extra_context)
    body = render_template(rendition[step_index]["body"], context)
    if lane.channel == DripLane.Channel.LINKEDIN:
        return "", body
    first_subject = render_template(rendition[0]["subject"], context)
    return thread_subject or first_subject, body


def _available_reference() -> str:
    for _attempt in range(MAX_REFERENCE_GENERATION_ATTEMPTS):
        reference = generate_reference()
        if not DripTrackedLink.objects.filter(reference=reference).exists():
            return reference
    raise LinkAttributionError("Could not generate a unique drip tracked-link reference.")


def _save_lane_progress(
    lane: DripLane,
    *,
    status: str,
    theme_index: int,
    theme_key: str,
    theme_started_at,
) -> None:
    lane.status = status
    lane.current_theme_index = theme_index
    lane.current_theme_key = theme_key
    lane.theme_started_at = theme_started_at
    lane.save(
        update_fields={
            "status",
            "current_theme_index",
            "current_theme_key",
            "theme_started_at",
            "updated_at",
        },
    )


def _apply_handoff(lane: DripLane, evaluation, *, now) -> None:
    anchor_candidates = [value for value in (lane.enrollment.activated_at, evaluation.completed_at) if value]
    anchor = max(anchor_candidates) if anchor_candidates else now
    existing_evidence = lane.handoff_evidence if isinstance(lane.handoff_evidence, dict) else {}
    lane.current_sequence_status = (
        lane.current_sequence_status
        if lane.current_sequence_status == DripLane.CurrentSequenceStatus.NOT_APPLICABLE
        else DripLane.CurrentSequenceStatus.COMPLETED
    )
    lane.handoff_evidence = {
        **existing_evidence,
        **evaluation.evidence,
        "handed_off_at": now.isoformat(),
    }
    lane.handed_off_at = now
    lane.status = DripLane.Status.ACTIVE
    lane.theme_started_at = anchor
    if evaluation.gmail_thread_id:
        lane.gmail_thread_id = evaluation.gmail_thread_id
        lane.gmail_thread_subject = evaluation.gmail_thread_subject
    lane.save(
        update_fields={
            "current_sequence_status",
            "handoff_evidence",
            "handed_off_at",
            "status",
            "theme_started_at",
            "gmail_thread_id",
            "gmail_thread_subject",
            "updated_at",
        },
    )


def _materialize_delivery(
    *,
    lane: DripLane,
    theme: dict[str, Any],
    theme_index: int,
    step_index: int,
    rendition: list[dict[str, Any]],
    due_at,
    thread_subject: str,
) -> DripDelivery:
    step = rendition[step_index]
    link = step.get("link")
    reference = ""
    attributed_url = ""
    extra_context = None
    if link is not None:
        if lane.channel != DripLane.Channel.GMAIL:
            raise ValueError("Tracked links are permitted only on Gmail deliveries")
        reference = _available_reference()
        attributed_url = build_attributed_url(link["url"], reference)
        extra_context = {"tracked_link": attributed_url}
    subject, body = _render_step(
        lane=lane,
        rendition=rendition,
        step_index=step_index,
        thread_subject=thread_subject,
        extra_context=extra_context,
    )
    media = rendition[step_index].get("media") or {}
    delivery = DripDelivery(
        lane=lane,
        theme_key=theme["key"],
        theme_index=theme_index,
        step_index=step_index,
        frozen_subject=subject,
        frozen_body=body,
        frozen_media_kind=media.get("type", ""),
        frozen_media_reference=media.get("file", ""),
        frozen_media_mime_type=media.get("mime_type", ""),
        frozen_media_size_bytes=media.get("size_bytes"),
        frozen_media_sha256=media.get("sha256", ""),
        scheduled_at=due_at,
        status=DripDelivery.Status.PLANNED,
        provider_account=lane.provider_account,
    )
    delivery.full_clean()
    delivery.save()
    if link is not None:
        tracked_link = DripTrackedLink(
            delivery=delivery,
            reference=reference,
            link_key=link["key"],
            destination_url=link["url"],
            attributed_url=attributed_url,
        )
        tracked_link.full_clean()
        tracked_link.save()
    _materialize_task(delivery=delivery, lane=lane)
    if lane.channel == DripLane.Channel.GMAIL and not lane.gmail_thread_subject:
        lane.gmail_thread_subject = subject
        lane.save(update_fields={"gmail_thread_subject", "updated_at"})
    return delivery


def _materialize_task(*, delivery: DripDelivery, lane: DripLane) -> None:
    """Attach one new queue Task to an existing frozen Delivery."""
    from linkedin.models import Task

    if delivery.current_task_id is not None:
        raise ValueError(f"Drip delivery {delivery.pk} already has a current Task")
    if delivery.status != DripDelivery.Status.PLANNED:
        raise ValueError(f"Drip delivery {delivery.pk} is not planned")
    task_type = (
        Task.TaskType.DRIP_LINKEDIN
        if lane.channel == DripLane.Channel.LINKEDIN
        else Task.TaskType.DRIP_GMAIL
    )
    task = Task.objects.create(
        task_type=task_type,
        status=Task.Status.PENDING,
        scheduled_at=delivery.scheduled_at,
        payload={"delivery_id": delivery.pk, "operator": lane.operator},
    )
    delivery.current_task = task
    delivery.status = DripDelivery.Status.QUEUED
    delivery.save(update_fields={"current_task", "status", "updated_at"})


def _reconcile_existing_outstanding(
    lane: DripLane,
    *,
    apply: bool,
    allow_task_creation: bool,
) -> _LaneOutcome | None:
    """Recover or hold the lane's one already-frozen outstanding Delivery."""
    from linkedin.models import Task
    from linkedin.tasks.stop_checks import lead_automation_stop_reason

    delivery_queryset = lane.deliveries.filter(
        status__in=(
            DripDelivery.Status.PLANNED,
            DripDelivery.Status.QUEUED,
            DripDelivery.Status.SENDING,
        ),
    ).order_by("theme_index", "step_index")
    if apply:
        delivery_queryset = delivery_queryset.select_for_update(of=("self",))
    deliveries = list(delivery_queryset)
    if not deliveries:
        return None
    if len(deliveries) > 1:
        return _LaneOutcome(
            lane.status,
            (
                _decision(
                    lane,
                    "invariant_block",
                    "lane has more than one outstanding delivery",
                ),
            ),
        )

    delivery = deliveries[0]
    decisions: list[ReconcileDecision] = []
    task = None
    if delivery.current_task_id is not None:
        task_queryset = Task.objects
        if apply:
            task_queryset = task_queryset.select_for_update(of=("self",))
        task = task_queryset.get(pk=delivery.current_task_id)
        if task.status in (Task.Status.PENDING, Task.Status.RUNNING):
            decisions.append(
                _decision(
                    lane,
                    "outstanding_delivery",
                    f"delivery {delivery.pk} is {delivery.status} with {task.status} task {task.pk}",
                    due_at=delivery.scheduled_at,
                ),
            )
            return _LaneOutcome(lane.status, tuple(decisions))
        if delivery.status not in (DripDelivery.Status.PLANNED, DripDelivery.Status.QUEUED):
            decisions.append(
                _decision(
                    lane,
                    "invariant_block",
                    f"delivery {delivery.pk} is {delivery.status} with terminal task {task.pk}",
                ),
            )
            return _LaneOutcome(lane.status, tuple(decisions))
        decisions.append(
            _decision(
                lane,
                "detached_terminal_task" if apply else "would_detach_terminal_task",
                f"delivery {delivery.pk} released from {task.status} task {task.pk}",
            ),
        )
        if apply:
            delivery.current_task = None
            delivery.status = DripDelivery.Status.PLANNED
            delivery.save(update_fields={"current_task", "status", "updated_at"})

    virtual_status = (
        DripDelivery.Status.PLANNED
        if task is not None and task.status in (Task.Status.COMPLETED, Task.Status.FAILED)
        else delivery.status
    )
    virtual_has_task = bool(
        delivery.current_task_id
        and not (task is not None and task.status in (Task.Status.COMPLETED, Task.Status.FAILED))
    )
    if virtual_status == DripDelivery.Status.SENDING or virtual_has_task:
        decisions.append(
            _decision(
                lane,
                "outstanding_delivery",
                f"delivery {delivery.pk} is {delivery.status}",
                due_at=delivery.scheduled_at,
            ),
        )
        return _LaneOutcome(lane.status, tuple(decisions))
    if virtual_status != DripDelivery.Status.PLANNED:
        decisions.append(
            _decision(
                lane,
                "invariant_block",
                f"queued delivery {delivery.pk} has no live current Task",
            ),
        )
        return _LaneOutcome(lane.status, tuple(decisions))
    if not allow_task_creation:
        decisions.append(
            _decision(
                lane,
                "planned_held",
                f"delivery {delivery.pk} remains frozen while controls are inactive",
                due_at=delivery.scheduled_at,
            ),
        )
        return _LaneOutcome(lane.status, tuple(decisions))

    stop_reason = lead_automation_stop_reason(lane.enrollment.lead)
    if stop_reason:
        decisions.append(_decision(lane, "stop", stop_reason))
        return _LaneOutcome(DripLane.Status.STOPPED, tuple(decisions), stop_reason)
    decisions.append(
        _decision(
            lane,
            "rematerialized_task" if apply else "would_rematerialize_task",
            f"delivery {delivery.pk} reuses its frozen content and schedule",
            due_at=delivery.scheduled_at,
        ),
    )
    if apply:
        _materialize_task(delivery=delivery, lane=lane)
    return _LaneOutcome(lane.status, tuple(decisions))


def _reconcile_lane(lane: DripLane, *, now, apply: bool) -> _LaneOutcome:
    from linkedin.tasks.stop_checks import lead_automation_stop_reason

    decisions: list[ReconcileDecision] = []
    if lane.status == DripLane.Status.STOPPED:
        return _LaneOutcome(lane.status, tuple(decisions))
    if lane.status == DripLane.Status.COMPLETED:
        return _LaneOutcome(lane.status, tuple(decisions))
    if lane.status == DripLane.Status.PAUSED:
        existing = _reconcile_existing_outstanding(
            lane,
            apply=apply,
            allow_task_creation=False,
        )
        if existing is not None:
            decisions.extend(existing.decisions)
        decisions.append(_decision(lane, "paused", "lane is paused"))
        return _LaneOutcome(lane.status, tuple(decisions))

    status = lane.status
    theme_index = lane.current_theme_index
    theme_started_at = lane.theme_started_at
    thread_subject = lane.gmail_thread_subject
    if lane.handed_off_at is None:
        evaluation = evaluate_handoff(lane)
        if not evaluation.eligible:
            status = evaluation.wait_status
            decisions.append(_decision(lane, "waiting_handoff", evaluation.reason))
            if apply and lane.status != status:
                lane.status = status
                lane.save(update_fields={"status", "updated_at"})
            return _LaneOutcome(status, tuple(decisions))
        anchor_candidates = [
            value
            for value in (lane.enrollment.activated_at, evaluation.completed_at)
            if value
        ]
        theme_started_at = max(anchor_candidates) if anchor_candidates else now
        status = DripLane.Status.ACTIVE
        thread_subject = evaluation.gmail_thread_subject or thread_subject
        decisions.append(_decision(lane, "handoff", evaluation.reason))
        if apply:
            _apply_handoff(lane, evaluation, now=now)
    elif status == DripLane.Status.WAITING_CONNECTION:
        if lane.channel != DripLane.Channel.LINKEDIN:
            decisions.append(
                _decision(
                    lane,
                    "invariant_block",
                    "only a LinkedIn lane may wait for connection after handoff",
                ),
            )
            return _LaneOutcome(status, tuple(decisions))
        evaluation = evaluate_handoff(lane)
        if not evaluation.eligible:
            decisions.append(
                _decision(lane, "waiting_connection", evaluation.reason),
            )
            return _LaneOutcome(status, tuple(decisions))
        status = DripLane.Status.ACTIVE
        decisions.append(
            _decision(lane, "connection_restored", evaluation.reason),
        )
        if apply:
            lane.status = DripLane.Status.ACTIVE
            lane.save(update_fields={"status", "updated_at"})
    elif status == DripLane.Status.WAITING_CURRENT:
        decisions.append(
            _decision(
                lane,
                "invariant_block",
                "handed-off lane cannot wait for the current sequence",
            ),
        )
        return _LaneOutcome(status, tuple(decisions))

    manifest = lane.enrollment.campaign_version.manifest
    themes = manifest["audiences"][lane.enrollment.frozen_icp]["themes"]
    if theme_started_at is None:
        decisions.append(_decision(lane, "invariant_block", "active lane has no theme anchor"))
        return _LaneOutcome(status, tuple(decisions))

    outstanding = _reconcile_existing_outstanding(
        lane,
        apply=apply,
        allow_task_creation=True,
    )
    if outstanding is not None:
        decisions.extend(outstanding.decisions)
        return _LaneOutcome(
            outstanding.status,
            tuple(decisions),
            outstanding.stop_reason,
        )

    while theme_index < len(themes):
        theme = themes[theme_index]
        sender_block = theme["senders"][lane.operator]
        rendition = sender_block.get(lane.channel)
        if rendition is None:
            decisions.append(
                _decision(
                    lane,
                    "advance_omitted_theme",
                    f"theme {theme['key']} has no {lane.channel} rendition",
                ),
            )
            theme_index += 1
            # An omitted rendition is explicitly completed at this lane's
            # current reconciliation transition. The next applicable theme
            # gets a fresh start instead of catching up from an old handoff or
            # an earlier theme's send date.
            theme_started_at = now
            next_key = themes[theme_index]["key"] if theme_index < len(themes) else ""
            if apply:
                _save_lane_progress(
                    lane,
                    status=(
                        DripLane.Status.ACTIVE
                        if theme_index < len(themes)
                        else DripLane.Status.COMPLETED
                    ),
                    theme_index=theme_index,
                    theme_key=next_key,
                    theme_started_at=theme_started_at,
                )
            continue

        deliveries = list(
            lane.deliveries.filter(theme_index=theme_index).order_by("step_index"),
        )
        by_step = {delivery.step_index: delivery for delivery in deliveries}
        unexpected = sorted(index for index in by_step if index >= len(rendition))
        if unexpected:
            decisions.append(
                _decision(
                    lane,
                    "invariant_block",
                    f"theme {theme['key']} has unexpected delivery step(s) {unexpected}",
                ),
            )
            return _LaneOutcome(status, tuple(decisions))

        next_step = 0
        while next_step < len(rendition):
            delivery = by_step.get(next_step)
            if delivery is None:
                break
            if delivery.status == DripDelivery.Status.SENT and delivery.sent_at:
                next_step += 1
                continue
            decisions.append(
                _decision(
                    lane,
                    "delivery_block",
                    f"delivery {delivery.pk} is {delivery.status}",
                ),
            )
            return _LaneOutcome(status, tuple(decisions))
        if any(index > next_step for index in by_step):
            decisions.append(
                _decision(lane, "invariant_block", "delivery sequence contains a gap"),
            )
            return _LaneOutcome(status, tuple(decisions))

        if next_step == len(rendition):
            completion_at = by_step[len(rendition) - 1].sent_at
            decisions.append(
                _decision(
                    lane,
                    "advance_theme",
                    f"theme {theme['key']} completed",
                ),
            )
            theme_index += 1
            theme_started_at = completion_at
            next_key = themes[theme_index]["key"] if theme_index < len(themes) else ""
            if apply:
                _save_lane_progress(
                    lane,
                    status=(
                        DripLane.Status.ACTIVE
                        if theme_index < len(themes)
                        else DripLane.Status.COMPLETED
                    ),
                    theme_index=theme_index,
                    theme_key=next_key,
                    theme_started_at=theme_started_at,
                )
            continue

        anchor = theme_started_at if next_step == 0 else by_step[next_step - 1].sent_at
        due_at = _due_at(
            lane=lane,
            anchor=anchor,
            delay_days=rendition[next_step]["delay_days"],
            now=now,
        )
        if due_at > now:
            decisions.append(
                _decision(
                    lane,
                    "waiting_due",
                    f"theme {theme['key']} step {next_step} is not due",
                    due_at=due_at,
                ),
            )
            return _LaneOutcome(status, tuple(decisions))

        stop_reason = lead_automation_stop_reason(lane.enrollment.lead)
        if stop_reason:
            decisions.append(_decision(lane, "stop", stop_reason))
            return _LaneOutcome(DripLane.Status.STOPPED, tuple(decisions), stop_reason)
        action = "materialized" if apply else "would_materialize"
        decisions.append(
            _decision(
                lane,
                action,
                f"theme {theme['key']} step {next_step}",
                due_at=due_at,
            ),
        )
        if apply:
            _materialize_delivery(
                lane=lane,
                theme=theme,
                theme_index=theme_index,
                step_index=next_step,
                rendition=rendition,
                due_at=due_at,
                thread_subject=thread_subject,
            )
        return _LaneOutcome(status, tuple(decisions))

    decisions.append(_decision(lane, "complete_lane", "all configured themes completed"))
    if apply and lane.status != DripLane.Status.COMPLETED:
        _save_lane_progress(
            lane,
            status=DripLane.Status.COMPLETED,
            theme_index=len(themes),
            theme_key="",
            theme_started_at=theme_started_at,
        )
    return _LaneOutcome(DripLane.Status.COMPLETED, tuple(decisions))


@transaction.atomic
def reconcile_drips(
    *,
    apply: bool,
    campaign_key: str = "",
    now=None,
) -> ReconcileResult:
    from linkedin.models import WorkflowRun
    from linkedin.tasks.stop_checks import lead_automation_stop_reason

    reconcile_now = now or timezone.now()
    acquire_reconciliation_lock()
    enrollments = DripEnrollment.objects.filter(
        status__in=NONTERMINAL_ENROLLMENT_STATUSES,
    )
    if campaign_key:
        enrollments = enrollments.filter(campaign__key=campaign_key)

    decisions: list[ReconcileDecision] = []
    enrollment_ids = list(enrollments.order_by("pk").values_list("pk", flat=True))
    for enrollment_id in enrollment_ids:
        # Canonical runtime lock order starts with Lead, then Enrollment.  The
        # initial ID scan is deliberately non-locking.
        enrollment = (
            lock_enrollment_graph(enrollment_id)
            if apply
            else DripEnrollment.objects.select_related(
                "lead",
                "campaign",
                "campaign_version",
            ).get(pk=enrollment_id)
        )
        stop_reason = lead_automation_stop_reason(enrollment.lead)
        if stop_reason:
            decisions.append(
                ReconcileDecision(
                    enrollment_id=enrollment.pk,
                    lane_id=None,
                    channel="all",
                    action="stop" if apply else "would_stop",
                    detail=stop_reason,
                ),
            )
            if apply:
                stop_enrollment_for_reason(
                    enrollment.pk,
                    reason=stop_reason,
                    now=reconcile_now,
                )
            continue

        lane_queryset = DripLane.objects.select_related(
            "enrollment__lead",
            "enrollment__campaign_version",
        ).filter(enrollment=enrollment).order_by("channel")
        if apply:
            lane_queryset = lane_queryset.select_for_update(of=("self",))
        lanes = list(lane_queryset)
        if {lane.channel for lane in lanes} != {
            DripLane.Channel.LINKEDIN,
            DripLane.Channel.GMAIL,
        }:
            decisions.append(
                ReconcileDecision(
                    enrollment_id=enrollment.pk,
                    lane_id=None,
                    channel="all",
                    action="invariant_block",
                    detail="enrollment does not have exactly one lane per channel",
                ),
            )
            continue

        if enrollment.campaign.status != DripCampaign.Status.ACTIVE:
            for lane in lanes:
                held = _reconcile_existing_outstanding(
                    lane,
                    apply=apply,
                    allow_task_creation=False,
                )
                if held is not None:
                    decisions.extend(held.decisions)
            decisions.append(
                ReconcileDecision(
                    enrollment_id=enrollment.pk,
                    lane_id=None,
                    channel="all",
                    action="campaign_inactive",
                    detail=f"campaign is {enrollment.campaign.status}",
                ),
            )
            continue
        if enrollment.status == DripEnrollment.Status.PAUSED:
            for lane in lanes:
                held = _reconcile_existing_outstanding(
                    lane,
                    apply=apply,
                    allow_task_creation=False,
                )
                if held is not None:
                    decisions.extend(held.decisions)
            decisions.append(
                ReconcileDecision(
                    enrollment_id=enrollment.pk,
                    lane_id=None,
                    channel="all",
                    action="paused",
                    detail="enrollment is paused",
                ),
            )
            continue

        outcomes: list[_LaneOutcome] = []
        stopped = False
        for lane in lanes:
            outcome = _reconcile_lane(lane, now=reconcile_now, apply=apply)
            outcomes.append(outcome)
            decisions.extend(outcome.decisions)
            if outcome.stop_reason:
                stopped = True
                if apply:
                    stop_enrollment_for_reason(
                        enrollment.pk,
                        reason=outcome.stop_reason,
                        now=reconcile_now,
                    )
                break
        if stopped:
            continue

        statuses = [outcome.status for outcome in outcomes]
        if all(status == DripLane.Status.COMPLETED for status in statuses):
            decisions.append(
                ReconcileDecision(
                    enrollment_id=enrollment.pk,
                    lane_id=None,
                    channel="all",
                    action="complete_enrollment",
                    detail="both channel lanes completed",
                ),
            )
            if apply:
                enrollment.status = DripEnrollment.Status.COMPLETED
                enrollment.completed_at = reconcile_now
                enrollment.save(
                    update_fields={"status", "completed_at", "updated_at"},
                )
        elif any(status == DripLane.Status.ACTIVE for status in statuses):
            if apply and enrollment.status == DripEnrollment.Status.WAITING:
                enrollment.status = DripEnrollment.Status.ACTIVE
                enrollment.save(update_fields={"status", "updated_at"})

    counts = dict(sorted(Counter(decision.action for decision in decisions).items()))
    workflow_run_id = None
    if apply:
        workflow = WorkflowRun.objects.create(
            name="drip-reconcile",
            operator="",
            summary=(
                f"enrollments={len({decision.enrollment_id for decision in decisions})} "
                f"decisions={len(decisions)}"
            ),
            counts={
                "enrollments": len({decision.enrollment_id for decision in decisions}),
                "decisions": len(decisions),
                "actions": counts,
            },
        )
        workflow_run_id = workflow.pk
    return ReconcileResult(
        applied=apply,
        decisions=tuple(decisions),
        counts=counts,
        workflow_run_id=workflow_run_id,
    )
