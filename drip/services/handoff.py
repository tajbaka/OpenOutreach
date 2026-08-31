from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from django.db import transaction
from django.utils import timezone

from drip.exceptions import HandoffReviewError
from drip.models import DripLane


@dataclass(frozen=True)
class HandoffEvaluation:
    eligible: bool
    reason: str
    completed_at: Any = None
    evidence: dict[str, Any] = field(default_factory=dict)
    wait_status: str = DripLane.Status.WAITING_CURRENT
    gmail_thread_id: str = ""
    gmail_thread_subject: str = ""


@dataclass(frozen=True)
class HandoffReviewResult:
    lane_id: int
    applied: bool
    detail: str


def _current_task_statuses():
    from linkedin.models import Task

    return (Task.Status.PENDING, Task.Status.RUNNING)


def _linkedin_public_ids(lead) -> set[str]:
    from linkedin.db.urls import url_to_public_id

    return {
        value
        for value in (
            (lead.public_identifier or "").strip(),
            url_to_public_id(lead.linkedin_url or ""),
        )
        if value
    }


def _has_current_linkedin_task(*, lead, operator: str) -> bool:
    from linkedin.models import Task
    from linkedin.operators import resolve_operator

    public_ids = _linkedin_public_ids(lead)
    tasks = Task.objects.filter(
        task_type=Task.TaskType.FOLLOW_UP,
        status__in=_current_task_statuses(),
    ).only("payload")
    for task in tasks:
        payload = task.payload if isinstance(task.payload, dict) else {}
        if (
            payload.get("public_id") in public_ids
            and resolve_operator(payload.get("operator")) == operator
        ):
            return True
    return False


def _has_current_gmail_or_enrich_task(*, lead) -> bool:
    from linkedin.models import Task

    return Task.objects.filter(
        task_type__in=(Task.TaskType.GMAIL_FOLLOW_UP, Task.TaskType.ENRICH_EMAIL),
        status__in=_current_task_statuses(),
        payload__lead_id=lead.pk,
    ).exists()


def _has_unresolved_current_gmail_submission(*, lead, operator: str) -> bool:
    """Block handoff after a possibly submitted current Gmail Task.

    A provider submission marker without its exact persisted outbound Message
    is intentionally permanent automation-stop evidence. If the Message does
    exist, normal current-sequence completion validation owns the decision.
    """
    from gmail.submission import persisted_submission_evidence, submission_attempted
    from linkedin.models import Task

    tasks = Task.objects.filter(
        task_type=Task.TaskType.GMAIL_FOLLOW_UP,
        payload__lead_id=lead.pk,
        payload__operator=operator,
    ).only("payload")
    for task in tasks:
        payload = task.payload if isinstance(task.payload, dict) else {}
        if not submission_attempted(payload):
            continue
        if not persisted_submission_evidence(payload):
            return True
    return False


def _has_unresolved_current_linkedin_submission(*, lead, operator: str) -> bool:
    """Block handoff after a possibly submitted current LinkedIn media Task."""
    from linkedin.tasks.follow_up_submission import has_unresolved_submission

    return has_unresolved_submission(lead_id=lead.pk, operator=operator)


def _eligible_linkedin_deals(*, lead, operator: str):
    from crm.models import Deal
    from drip.services.linkedin_connection import sender_owned_connected_deal_proofs
    from linkedin.enums import ProfileState

    proofs = sender_owned_connected_deal_proofs(
        lead=lead,
        operator=operator,
        allowed_states=(ProfileState.CONNECTED, ProfileState.COMPLETED),
    )
    deal_ids = {proof.deal_id for proof in proofs}
    return Deal.objects.filter(pk__in=deal_ids).order_by("pk")


def _review_is_complete(lane: DripLane) -> bool:
    return bool(lane.current_sequence_reviewed_at and lane.current_sequence_reviewed_by.strip())


def evaluate_linkedin_handoff(lane: DripLane) -> HandoffEvaluation:
    from crm.models import Message
    from linkedin.exceptions import SheetsError
    from linkedin.icp_outbound import channel_steps
    from linkedin.operators import resolve_operator
    from linkedin.tasks.follow_up import DEFAULT_CHANNEL, DEFAULT_SEQUENCE_NAME

    lead = lane.enrollment.lead
    operator = lane.operator
    if lane.channel != DripLane.Channel.LINKEDIN:
        return HandoffEvaluation(False, "not_linkedin_lane")
    from drip.services.linkedin_identity import frozen_linkedin_identity_errors

    identity_errors = frozen_linkedin_identity_errors(
        lead=lead,
        recipient_identity=lane.recipient_identity,
        member_urn=lane.linkedin_member_urn,
    )
    if identity_errors:
        return HandoffEvaluation(
            False,
            "linkedin_identity_invalid:" + ",".join(identity_errors),
        )
    if _has_current_linkedin_task(lead=lead, operator=operator):
        return HandoffEvaluation(False, "current_linkedin_task_outstanding")
    if _has_unresolved_current_linkedin_submission(
        lead=lead,
        operator=operator,
    ):
        return HandoffEvaluation(False, "current_linkedin_submission_unclear")

    deals = list(_eligible_linkedin_deals(lead=lead, operator=operator))
    if not deals:
        return HandoffEvaluation(
            False,
            "sender_owned_linkedin_connection_not_proven",
            wait_status=DripLane.Status.WAITING_CONNECTION,
        )

    if lane.current_sequence_status == DripLane.CurrentSequenceStatus.NOT_APPLICABLE:
        if not _review_is_complete(lane):
            return HandoffEvaluation(False, "not_applicable_review_incomplete")
        if _current_sequence_evidence_exists(lane):
            return HandoffEvaluation(
                False,
                "current_linkedin_evidence_after_not_applicable_review",
            )
        return HandoffEvaluation(
            True,
            "reviewed_not_applicable",
            completed_at=lane.current_sequence_reviewed_at,
            evidence={
                "mode": "reviewed_not_applicable",
                "reviewed_at": lane.current_sequence_reviewed_at.isoformat(),
                "reviewed_by": lane.current_sequence_reviewed_by,
                "connected_deal_ids": [deal.pk for deal in deals],
            },
        )

    try:
        steps = channel_steps(
            sender=operator,
            icp=lane.enrollment.frozen_icp,
            channel=DEFAULT_CHANNEL,
        )
    except SheetsError as exc:
        return HandoffEvaluation(False, f"current_linkedin_template_invalid:{exc}")
    if not steps:
        return HandoffEvaluation(False, "current_linkedin_template_has_no_steps")
    final_step = len(steps) - 1

    candidates = []
    for deal in deals:
        prefix = (
            f"daemon-send:{operator}:{deal.pk}:{DEFAULT_SEQUENCE_NAME}:"
            f"step-{final_step}:"
        )
        messages = list(
            Message.objects.filter(
                lead=lead,
                source=Message.Source.LINKEDIN,
                direction=Message.Direction.OUTBOUND,
                external_id__startswith=prefix,
            ).select_related("operator").order_by("sent_at", "pk"),
        )
        for message in messages:
            exact_owner = (
                message.operator_id
                and message.operator.handle == operator
            )
            legacy_sender_owner = (
                not message.operator_id
                and resolve_operator(message.sender) == operator
            )
            if exact_owner or legacy_sender_owner:
                candidates.append((message, deal))
    if not candidates:
        return HandoffEvaluation(False, "current_linkedin_final_step_not_persisted")
    message, deal = max(candidates, key=lambda item: (item[0].sent_at, item[0].pk))
    return HandoffEvaluation(
        True,
        "current_linkedin_sequence_completed",
        completed_at=message.sent_at,
        evidence={
            "mode": "persisted_final_step",
            "deal_id": deal.pk,
            "message_id": message.pk,
            "external_id": message.external_id,
            "sequence_name": DEFAULT_SEQUENCE_NAME,
            "final_step_index": final_step,
            "template_step_count": len(steps),
        },
    )


def _gmail_sequence_messages(*, lead, operator: str, step_count: int):
    from crm.models import Message
    from gmail.handoff import DEFAULT_GMAIL_SEQUENCE_NAME

    messages = []
    for step_index in range(step_count):
        automation_key = (
            f"gmail_follow_up:{operator}:{lead.pk}:"
            f"{DEFAULT_GMAIL_SEQUENCE_NAME}:step-{step_index}"
        )
        matches = list(
            Message.objects.filter(
                lead=lead,
                source=Message.Source.GMAIL,
                direction=Message.Direction.OUTBOUND,
                raw__automation_key=automation_key,
            ).order_by("pk"),
        )
        if len(matches) != 1:
            return [], f"gmail_step_{step_index}_persisted_count_{len(matches)}"
        messages.append(matches[0])
    return messages, ""


def _safe_gmail_binding(message, *, account_key: str, send_as: str):
    from gmail.client import scoped_gmail_id, validated_provider_rfc_message_id

    raw = message.raw if isinstance(message.raw, dict) else {}
    thread_id = str(raw.get("gmail_thread_id") or "").strip()
    rfc_message_id = str(raw.get("rfc_message_id") or "").strip()
    subject = str(raw.get("thread_subject") or "").strip()
    references = raw.get("references", [])
    if raw.get("gmail_account") != account_key:
        return None, "gmail_account_mismatch"
    if str(raw.get("send_as") or message.sender).strip().lower() != send_as:
        return None, "gmail_send_as_mismatch"
    if not thread_id or not rfc_message_id or not subject:
        return None, "gmail_binding_incomplete"
    if not isinstance(references, list) or not all(
        isinstance(value, str) and value.strip() for value in references
    ):
        return None, "gmail_references_invalid"
    try:
        validated_references = [
            validated_provider_rfc_message_id(value)
            for value in references
        ]
    except ValueError:
        return None, "gmail_references_invalid"
    try:
        validated_provider_rfc_message_id(rfc_message_id)
    except ValueError:
        return None, "gmail_rfc_message_id_invalid"
    try:
        expected_thread_external_id = scoped_gmail_id(account_key, thread_id)
    except ValueError:
        return None, "gmail_thread_id_invalid"
    if message.thread_external_id != expected_thread_external_id:
        return None, "gmail_scoped_thread_mismatch"
    return {
        "thread_id": thread_id,
        "subject": subject,
        "rfc_message_id": rfc_message_id,
        "references": validated_references,
    }, ""


def evaluate_gmail_handoff(lane: DripLane) -> HandoffEvaluation:
    from gmail.templates import steps_for_icp
    from linkedin.exceptions import SheetsError

    lead = lane.enrollment.lead
    if lane.channel != DripLane.Channel.GMAIL:
        return HandoffEvaluation(False, "not_gmail_lane")
    if not lead.email or lane.recipient_identity != lead.email.strip().lower():
        return HandoffEvaluation(False, "gmail_recipient_identity_changed")
    if _has_unresolved_current_gmail_submission(
        lead=lead,
        operator=lane.operator,
    ):
        return HandoffEvaluation(False, "current_gmail_submission_unclear")
    if _has_current_gmail_or_enrich_task(lead=lead):
        return HandoffEvaluation(False, "current_gmail_or_enrich_task_outstanding")
    if lane.current_sequence_status == DripLane.CurrentSequenceStatus.NOT_APPLICABLE:
        if not _review_is_complete(lane):
            return HandoffEvaluation(False, "not_applicable_review_incomplete")
        if _current_sequence_evidence_exists(lane):
            return HandoffEvaluation(
                False,
                "current_gmail_evidence_after_not_applicable_review",
            )
        return HandoffEvaluation(
            True,
            "reviewed_not_applicable",
            completed_at=lane.current_sequence_reviewed_at,
            evidence={
                "mode": "reviewed_not_applicable",
                "reviewed_at": lane.current_sequence_reviewed_at.isoformat(),
                "reviewed_by": lane.current_sequence_reviewed_by,
                "gmail_account": lane.provider_account,
                "send_as": lane.sender_identity,
            },
        )

    try:
        steps = steps_for_icp(
            sender=lane.operator,
            icp=lane.enrollment.frozen_icp,
            sequence_name="gmail_fallback",
        )
    except SheetsError as exc:
        return HandoffEvaluation(False, f"current_gmail_template_invalid:{exc}")
    if not steps:
        return HandoffEvaluation(False, "current_gmail_template_has_no_steps")

    messages, error = _gmail_sequence_messages(
        lead=lead,
        operator=lane.operator,
        step_count=len(steps),
    )
    if error:
        return HandoffEvaluation(False, error)
    bindings = []
    for message in messages:
        binding, error = _safe_gmail_binding(
            message,
            account_key=lane.provider_account,
            send_as=lane.sender_identity,
        )
        if error:
            return HandoffEvaluation(False, error)
        bindings.append(binding)
    thread_ids = {binding["thread_id"] for binding in bindings}
    subjects = {binding["subject"] for binding in bindings}
    if len(thread_ids) != 1:
        return HandoffEvaluation(False, "current_gmail_sequence_split_threads")
    if len(subjects) != 1:
        return HandoffEvaluation(False, "current_gmail_sequence_subject_changed")
    final_message = messages[-1]
    final_binding = bindings[-1]
    accumulated_references = list(
        dict.fromkeys((*final_binding["references"], final_binding["rfc_message_id"])),
    )
    return HandoffEvaluation(
        True,
        "current_gmail_sequence_completed",
        completed_at=final_message.sent_at,
        evidence={
            "mode": "persisted_final_step",
            "message_id": final_message.pk,
            "automation_key": final_message.raw["automation_key"],
            "final_step_index": len(steps) - 1,
            "template_step_count": len(steps),
            "gmail_account": lane.provider_account,
            "send_as": lane.sender_identity,
            "gmail_thread_id": final_binding["thread_id"],
            "last_rfc_message_id": final_binding["rfc_message_id"],
            "references": accumulated_references,
        },
        gmail_thread_id=final_binding["thread_id"],
        gmail_thread_subject=final_binding["subject"],
    )


def evaluate_handoff(lane: DripLane) -> HandoffEvaluation:
    if lane.channel == DripLane.Channel.LINKEDIN:
        return evaluate_linkedin_handoff(lane)
    return evaluate_gmail_handoff(lane)


def _current_sequence_evidence_exists(lane: DripLane) -> bool:
    from crm.models import Deal, Message
    from linkedin.tasks.follow_up import DEFAULT_SEQUENCE_NAME

    lead = lane.enrollment.lead
    if lane.channel == DripLane.Channel.LINKEDIN:
        for deal_id in Deal.objects.filter(lead=lead).values_list("pk", flat=True):
            prefix = f"daemon-send:{lane.operator}:{deal_id}:{DEFAULT_SEQUENCE_NAME}:step-"
            if Message.objects.filter(
                lead=lead,
                source=Message.Source.LINKEDIN,
                direction=Message.Direction.OUTBOUND,
                external_id__startswith=prefix,
            ).exists():
                return True
        return False

    prefix = f"gmail_follow_up:{lane.operator}:{lead.pk}:"
    legacy_external_prefix = f"gmail-send:{lane.operator}:{lead.pk}:"
    messages = Message.objects.filter(
        lead=lead,
        source=Message.Source.GMAIL,
        direction=Message.Direction.OUTBOUND,
    ).only("external_id", "raw")
    return any(
        message.external_id.startswith(legacy_external_prefix)
        or (
            isinstance(message.raw, dict)
            and str(message.raw.get("automation_key") or "").startswith(prefix)
        )
        for message in messages
    )


@transaction.atomic
def review_handoff_not_applicable(
    *,
    lane_id: int,
    reviewed_by: str,
    apply: bool,
) -> HandoffReviewResult:
    reviewer = (reviewed_by or "").strip()
    if not reviewer:
        raise HandoffReviewError("A non-empty reviewer is required.")
    if len(reviewer) > 150:
        raise HandoffReviewError("Reviewer must be at most 150 characters.")
    if not DripLane.objects.filter(pk=lane_id).exists():
        raise HandoffReviewError(f"Unknown DripLane ID: {lane_id}")
    from drip.services.ownership import lock_lane_graph

    enrollment, lane = lock_lane_graph(lane_id)
    lane.enrollment = enrollment
    if lane.handed_off_at:
        raise HandoffReviewError("Lane already handed off; its predecessor decision is immutable.")
    if lane.current_sequence_status == DripLane.CurrentSequenceStatus.COMPLETED:
        raise HandoffReviewError("Persisted completion evidence already owns this handoff decision.")
    if _current_sequence_evidence_exists(lane):
        raise HandoffReviewError(
            "Current-sequence outbound evidence exists; not-applicable would be false.",
        )
    if (
        lane.channel == DripLane.Channel.GMAIL
        and _has_unresolved_current_gmail_submission(
            lead=lane.enrollment.lead,
            operator=lane.operator,
        )
    ):
        raise HandoffReviewError(
            "Current Gmail submission outcome is unclear; not-applicable is unsafe.",
        )
    if (
        lane.channel == DripLane.Channel.LINKEDIN
        and _has_unresolved_current_linkedin_submission(
            lead=lane.enrollment.lead,
            operator=lane.operator,
        )
    ):
        raise HandoffReviewError(
            "Current LinkedIn submission outcome is unclear; not-applicable is unsafe.",
        )
    if lane.channel == DripLane.Channel.LINKEDIN:
        has_task = _has_current_linkedin_task(
            lead=lane.enrollment.lead,
            operator=lane.operator,
        )
    else:
        has_task = _has_current_gmail_or_enrich_task(lead=lane.enrollment.lead)
    if has_task:
        raise HandoffReviewError(
            "Current-sequence work is pending or running; not-applicable is unsafe.",
        )
    if not apply:
        return HandoffReviewResult(
            lane_id=lane.pk,
            applied=False,
            detail="would record reviewed current-sequence not-applicable",
        )

    reviewed_at = timezone.now()
    lane.current_sequence_status = DripLane.CurrentSequenceStatus.NOT_APPLICABLE
    lane.current_sequence_reviewed_at = reviewed_at
    lane.current_sequence_reviewed_by = reviewer
    lane.handoff_evidence = {
        **(lane.handoff_evidence if isinstance(lane.handoff_evidence, dict) else {}),
        "current_sequence_review": {
            "decision": "not_applicable",
            "reviewed_at": reviewed_at.isoformat(),
            "reviewed_by": reviewer,
        },
    }
    lane.full_clean()
    lane.save(
        update_fields={
            "current_sequence_status",
            "current_sequence_reviewed_at",
            "current_sequence_reviewed_by",
            "handoff_evidence",
            "updated_at",
        },
    )
    return HandoffReviewResult(
        lane_id=lane.pk,
        applied=True,
        detail="recorded reviewed current-sequence not-applicable",
    )
