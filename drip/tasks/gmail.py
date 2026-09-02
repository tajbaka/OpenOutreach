"""Executor for one already-claimed ``drip_gmail`` delivery Task."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import timedelta

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from drip.models import (
    DripCampaign,
    DripDelivery,
    DripDeliveryAttempt,
    DripEnrollment,
    DripLane,
)
from gmail.auth import GMAIL_OPERATOR_MAPPING
from gmail.client import (
    GmailClient,
    GmailSendResult,
    scoped_gmail_id,
    validated_provider_rfc_message_id,
)
from gmail.delivery import issue_gmail_delivery_permit
from linkedin.tasks.stop_checks import lead_automation_stop_reason


@dataclass(frozen=True)
class _Reservation:
    delivery_id: int
    attempt_id: int
    task_id: int
    lead_id: int
    operator: str
    account_key: str
    send_as: str
    reply_to: str
    recipient: str
    subject: str
    body: str
    raw_thread_id: str
    in_reply_to: str
    references: tuple[str, ...]
    rfc_message_id: str


def _payload_delivery_id(task) -> int:
    raw_delivery_id = (task.payload or {}).get("delivery_id")
    if isinstance(raw_delivery_id, bool) or not isinstance(raw_delivery_id, int):
        raise ValueError("drip_gmail task delivery_id must be an integer")
    if raw_delivery_id <= 0:
        raise ValueError("drip_gmail task delivery_id must be positive")
    return raw_delivery_id


def _stable_rfc_message_id(*, delivery_id: int, send_as: str) -> str:
    digest = hashlib.sha256(f"drip_gmail:{delivery_id}".encode("utf-8")).hexdigest()[:32]
    domain = send_as.rsplit("@", 1)[-1]
    return f"<openoutreach-drip-{digest}@{domain}>"


def _reference_ids(value) -> tuple[str, ...]:
    if value in (None, "", []):
        return ()
    if isinstance(value, str):
        candidates = value.split()
    elif isinstance(value, list):
        candidates = value
    else:
        raise ValueError("Gmail References metadata must be a string or list")
    references: list[str] = []
    for candidate in candidates:
        if not isinstance(candidate, str):
            raise ValueError("Gmail References metadata contains a non-string value")
        message_id = candidate.strip()
        if not message_id:
            continue
        try:
            validated_provider_rfc_message_id(message_id)
        except ValueError:
            raise ValueError("Gmail References metadata contains an invalid Message-ID")
        if message_id not in references:
            references.append(message_id)
    return tuple(references)


def _validate_rfc_message_id(value: str, *, label: str) -> str:
    try:
        message_id = validated_provider_rfc_message_id(value)
    except ValueError:
        raise ValueError(f"{label} is not a valid RFC Message-ID")
    return message_id


def _delivery_thread_context(
    delivery: DripDelivery,
    lane: DripLane,
) -> tuple[str, str, str, tuple[str, ...]]:
    """Return raw thread ID, subject, immediate parent, and References."""
    previous = (
        DripDelivery.objects.select_for_update()
        .filter(
            lane=lane,
            status=DripDelivery.Status.SENT,
        )
        .exclude(pk=delivery.pk)
        .order_by("-sent_at", "-pk")
        .first()
    )

    if delivery.step_index:
        same_theme_predecessor = (
            DripDelivery.objects.select_for_update()
            .filter(
                lane=lane,
                theme_index=delivery.theme_index,
                step_index=delivery.step_index - 1,
            )
            .first()
        )
        if (
            same_theme_predecessor is None
            or same_theme_predecessor.status != DripDelivery.Status.SENT
            or same_theme_predecessor.sent_at is None
        ):
            raise ValueError("Drip Gmail delivery lacks its successful same-theme predecessor")
        if delivery.scheduled_at < same_theme_predecessor.sent_at:
            raise ValueError("Drip Gmail delivery is scheduled before its predecessor sent")
        if previous is None or previous.pk != same_theme_predecessor.pk:
            raise ValueError("Drip Gmail delivery predecessor is not the lane's latest send")

    raw_thread_id = (lane.gmail_thread_id or "").strip()
    thread_subject = (lane.gmail_thread_subject or "").strip()
    if previous is not None:
        if not raw_thread_id or not thread_subject:
            raise ValueError("Sent drip Gmail history has no owned lane thread binding")
        if previous.provider_account != lane.provider_account:
            raise ValueError("Prior drip Gmail delivery belongs to another mailbox")
        if previous.provider_thread_id != raw_thread_id:
            raise ValueError("Prior drip Gmail delivery belongs to another thread")
        parent = _validate_rfc_message_id(
            previous.rfc_message_id,
            label="Prior drip Gmail Message-ID",
        )
        references = _reference_ids(previous.rfc_references)
        references = tuple(dict.fromkeys((*references, parent)))
        return raw_thread_id, thread_subject, parent, references

    if raw_thread_id:
        if not thread_subject:
            raise ValueError("Inherited Gmail thread has no original subject")
        evidence = lane.handoff_evidence if isinstance(lane.handoff_evidence, dict) else {}
        if str(evidence.get("gmail_account") or "").strip().lower() != lane.provider_account:
            raise ValueError("Inherited Gmail thread evidence names another mailbox")
        if str(evidence.get("send_as") or "").strip().lower() != lane.sender_identity:
            raise ValueError("Inherited Gmail thread evidence names another Send-As alias")
        if str(evidence.get("gmail_thread_id") or "").strip() != raw_thread_id:
            raise ValueError("Inherited Gmail thread evidence does not match the lane")
        parent = _validate_rfc_message_id(
            str(evidence.get("last_rfc_message_id") or ""),
            label="Inherited Gmail Message-ID",
        )
        references = _reference_ids(evidence.get("references"))
        if parent not in references:
            raise ValueError("Inherited Gmail References omit the final current message")
        return raw_thread_id, thread_subject, parent, references

    if lane.current_sequence_status != DripLane.CurrentSequenceStatus.NOT_APPLICABLE:
        raise ValueError("Drip Gmail cannot open a new thread without reviewed not-applicable handoff")
    if previous is not None:
        raise ValueError("Drip Gmail history exists without its lane thread binding")
    subject = (delivery.frozen_subject or "").strip()
    if not subject:
        raise ValueError("The first Gmail delivery requires a frozen subject")
    return "", subject, "", ()


def _manifest_due_at(
    delivery: DripDelivery,
    lane: DripLane,
    enrollment: DripEnrollment,
):
    """Prove frozen same-channel predecessors and return the configured due time."""
    try:
        themes = enrollment.campaign_version.manifest["audiences"][
            enrollment.frozen_icp
        ]["themes"]
        theme = themes[delivery.theme_index]
        steps = theme["senders"][lane.operator][DripLane.Channel.GMAIL]
    except (IndexError, KeyError, TypeError) as exc:
        raise ValueError("Drip Gmail delivery is outside its frozen manifest") from exc
    if theme["key"] != delivery.theme_key:
        raise ValueError("Drip Gmail delivery theme key drifted from its manifest")
    if delivery.step_index < 0 or delivery.step_index >= len(steps):
        raise ValueError("Drip Gmail delivery step is outside its frozen rendition")

    existing = {
        (candidate.theme_index, candidate.step_index): candidate
        for candidate in DripDelivery.objects.select_for_update().filter(
            lane=lane,
            theme_index__lte=delivery.theme_index,
        )
    }
    for earlier_theme_index in range(delivery.theme_index):
        earlier = themes[earlier_theme_index]["senders"][lane.operator].get(
            DripLane.Channel.GMAIL,
        )
        if earlier is None:
            continue
        for earlier_step_index in range(len(earlier)):
            predecessor = existing.get((earlier_theme_index, earlier_step_index))
            if (
                predecessor is None
                or predecessor.status != DripDelivery.Status.SENT
                or predecessor.sent_at is None
            ):
                raise ValueError("An earlier Gmail theme is not complete")

    if delivery.step_index == 0:
        if lane.theme_started_at is None:
            raise ValueError("Drip Gmail lane has no theme timing anchor")
        anchor = lane.theme_started_at
    else:
        predecessor = existing.get((delivery.theme_index, delivery.step_index - 1))
        if (
            predecessor is None
            or predecessor.status != DripDelivery.Status.SENT
            or predecessor.sent_at is None
        ):
            raise ValueError("Drip Gmail delivery lacks its successful predecessor")
        anchor = predecessor.sent_at
    return anchor + timedelta(days=float(steps[delivery.step_index]["delay_days"]))


def _release_without_attempt(delivery: DripDelivery, task, *, reason: str) -> None:
    if delivery.status == DripDelivery.Status.QUEUED:
        delivery.status = DripDelivery.Status.PLANNED
        delivery.current_task = None
        delivery.save(update_fields={"status", "current_task", "updated_at"})
    type(task).objects.filter(pk=task.pk).update(error=reason)


@transaction.atomic
def _reserve_attempt(task) -> _Reservation | None:
    from linkedin.models import Task
    from drip.services.ownership import lock_delivery_graph

    if task.task_type != Task.TaskType.DRIP_GMAIL:
        raise ValueError("Gmail drip handler received another Task type")
    if task.status != Task.Status.RUNNING:
        raise ValueError("Gmail drip handler requires an already-claimed running Task")

    payload = task.payload or {}
    delivery_id = _payload_delivery_id(task)
    operator = (payload.get("operator") or "").strip()
    graph = lock_delivery_graph(delivery_id, task_id=task.pk)
    claimed_task = graph.task
    delivery = graph.delivery
    lane = graph.lane
    enrollment = graph.enrollment

    if claimed_task.task_type != Task.TaskType.DRIP_GMAIL:
        raise ValueError("Gmail drip handler received another Task type")
    if claimed_task.status != Task.Status.RUNNING:
        raise ValueError("Gmail drip handler requires an already-claimed running Task")

    if delivery.current_task_id != claimed_task.pk:
        raise ValueError("Drip Gmail Task does not own the referenced delivery")
    if delivery.status in {
        DripDelivery.Status.SENT,
        DripDelivery.Status.STOPPED,
        DripDelivery.Status.UNCLEAR,
        DripDelivery.Status.FAILED,
    }:
        return None
    if delivery.status != DripDelivery.Status.QUEUED:
        raise ValueError(f"Drip Gmail delivery is not queued: {delivery.status}")
    if lane.channel != DripLane.Channel.GMAIL:
        raise ValueError("Drip Gmail Task references a non-Gmail lane")
    if not operator or operator != lane.operator:
        raise ValueError("Drip Gmail Task operator does not own its lane")

    mapping = GMAIL_OPERATOR_MAPPING.get(operator)
    if mapping is None:
        raise ValueError(f"No Gmail mapping configured for operator {operator!r}")
    account_key = mapping["gmail_account"].strip().lower()
    send_as = mapping["send_as"].strip().lower()
    reply_to = mapping["reply_to"].strip().lower()
    recipient = (lane.recipient_identity or "").strip().lower()
    if lane.provider_account != account_key or delivery.provider_account != account_key:
        raise ValueError("Drip Gmail delivery is assigned to another mailbox")
    if lane.sender_identity != send_as:
        raise ValueError("Drip Gmail lane is assigned to another Send-As alias")
    if not recipient or recipient != (enrollment.lead.email or "").strip().lower():
        raise ValueError("Drip Gmail recipient no longer matches the enrolled Lead")
    if (
        delivery.theme_index != lane.current_theme_index
        or delivery.theme_key != lane.current_theme_key
    ):
        raise ValueError("Drip Gmail delivery is not for the lane's current theme")
    now = timezone.now()
    if delivery.scheduled_at > now:
        raise ValueError("Drip Gmail delivery is not due")

    inactive_reason = ""
    if enrollment.campaign.status != DripCampaign.Status.ACTIVE:
        inactive_reason = f"Drip campaign is {enrollment.campaign.status}"
    elif enrollment.status != DripEnrollment.Status.ACTIVE:
        inactive_reason = f"Drip enrollment is {enrollment.status}"
    elif lane.status != DripLane.Status.ACTIVE:
        inactive_reason = f"Drip Gmail lane is {lane.status}"
    elif not lane.handed_off_at or not lane.theme_started_at:
        raise ValueError("Active Drip Gmail lane lacks handoff timing anchors")
    if inactive_reason:
        _release_without_attempt(delivery, claimed_task, reason=inactive_reason)
        return None

    manifest_due_at = _manifest_due_at(delivery, lane, enrollment)
    if manifest_due_at > now:
        delivery.scheduled_at = max(delivery.scheduled_at, manifest_due_at)
        delivery.save(update_fields={"scheduled_at", "updated_at"})
        _release_without_attempt(
            delivery,
            claimed_task,
            reason="Drip Gmail delivery is not due from its successful predecessor",
        )
        return None

    stop_reason = lead_automation_stop_reason(enrollment.lead)
    if stop_reason:
        from drip.services.stops import stop_enrollment_for_reason

        stop_enrollment_for_reason(enrollment.pk, reason=stop_reason)
        return None

    raw_thread_id, subject, parent, references = _delivery_thread_context(
        delivery,
        lane,
    )
    next_attempt_number = (
        delivery.attempts.aggregate(maximum=Max("attempt_number"))["maximum"] or 0
    ) + 1
    attempt = DripDeliveryAttempt.objects.create(
        delivery=delivery,
        attempt_number=next_attempt_number,
        outcome=DripDeliveryAttempt.Outcome.RESERVED,
    )
    delivery.status = DripDelivery.Status.SENDING
    delivery.save(update_fields={"status", "updated_at"})
    return _Reservation(
        delivery_id=delivery.pk,
        attempt_id=attempt.pk,
        task_id=claimed_task.pk,
        lead_id=enrollment.lead_id,
        operator=operator,
        account_key=account_key,
        send_as=send_as,
        reply_to=reply_to,
        recipient=recipient,
        subject=subject,
        body=delivery.frozen_body,
        raw_thread_id=raw_thread_id,
        in_reply_to=parent,
        references=references,
        rfc_message_id=_stable_rfc_message_id(
            delivery_id=delivery.pk,
            send_as=send_as,
        ),
    )


@transaction.atomic
def _stamp_submission_attempt(reservation: _Reservation) -> None:
    from linkedin.models import Task
    from drip.services.ownership import lock_delivery_graph

    graph = lock_delivery_graph(
        reservation.delivery_id,
        attempt_id=reservation.attempt_id,
        task_id=reservation.task_id,
    )
    delivery = graph.delivery
    lane = graph.lane
    enrollment = graph.enrollment
    attempt = graph.attempt
    task = graph.task

    if (
        task.status != Task.Status.RUNNING
        or delivery.current_task_id != task.pk
        or delivery.status != DripDelivery.Status.SENDING
        or attempt.outcome != DripDeliveryAttempt.Outcome.RESERVED
        or attempt.submission_attempted_at is not None
    ):
        raise ValueError("Drip Gmail submission ownership changed after reservation")
    if (
        enrollment.campaign.status != DripCampaign.Status.ACTIVE
        or enrollment.status != DripEnrollment.Status.ACTIVE
        or lane.status != DripLane.Status.ACTIVE
    ):
        raise ValueError("Drip Gmail state stopped or paused before submission")
    if (
        lane.operator != reservation.operator
        or lane.provider_account != reservation.account_key
        or lane.sender_identity != reservation.send_as
        or lane.recipient_identity != reservation.recipient
    ):
        raise ValueError("Drip Gmail lane ownership changed before submission")
    stop_reason = lead_automation_stop_reason(enrollment.lead)
    if stop_reason:
        raise ValueError(f"Drip Gmail stopped before submission: {stop_reason}")

    attempt.submission_attempted_at = timezone.now()
    attempt.save(update_fields={"submission_attempted_at"})


def _diagnostic_detail(exc: Exception) -> str:
    if isinstance(exc, ValueError):
        detail = str(exc).replace("\r", " ").replace("\n", " ").strip()
        return f"{type(exc).__name__}: {detail}"[:1000]
    return type(exc).__name__


@transaction.atomic
def _record_failure(reservation: _Reservation, exc: Exception) -> None:
    from drip.services.ownership import lock_delivery_graph

    graph = lock_delivery_graph(
        reservation.delivery_id,
        attempt_id=reservation.attempt_id,
    )
    delivery = graph.delivery
    lane = graph.lane
    enrollment = graph.enrollment
    attempt = graph.attempt
    if attempt.outcome != DripDeliveryAttempt.Outcome.RESERVED:
        return

    now = timezone.now()
    attempt.finished_at = now
    attempt.diagnostic_detail = _diagnostic_detail(exc)
    if attempt.submission_attempted_at is None:
        attempt.outcome = DripDeliveryAttempt.Outcome.NOT_SUBMITTED
        if delivery.status == DripDelivery.Status.SENDING:
            stop_reason = lead_automation_stop_reason(enrollment.lead)
            terminal_stop = (
                enrollment.status == DripEnrollment.Status.STOPPED
                or lane.status == DripLane.Status.STOPPED
                or bool(stop_reason)
            )
            if stop_reason and enrollment.status != DripEnrollment.Status.STOPPED:
                from drip.services.stops import stop_enrollment_for_reason

                stop_enrollment_for_reason(enrollment.pk, reason=stop_reason)
            delivery.status = (
                DripDelivery.Status.STOPPED
                if terminal_stop
                else DripDelivery.Status.PLANNED
            )
            delivery.current_task = None
            delivery.save(update_fields={"status", "current_task", "updated_at"})
    else:
        attempt.outcome = DripDeliveryAttempt.Outcome.UNCLEAR
        delivery.status = DripDelivery.Status.UNCLEAR
        delivery.save(update_fields={"status", "updated_at"})
        if lane.status == DripLane.Status.ACTIVE:
            lane.status = DripLane.Status.PAUSED
            lane.save(update_fields={"status", "updated_at"})
    attempt.save(update_fields={
        "outcome",
        "finished_at",
        "diagnostic_detail",
    })


@transaction.atomic
def _record_success(
    reservation: _Reservation,
    result: GmailSendResult,
):
    from crm.models import Message, SalesOwner
    from drip.services.ownership import lock_delivery_graph
    from linkedin.operators import resolve_sales_owner_handle

    graph = lock_delivery_graph(
        reservation.delivery_id,
        attempt_id=reservation.attempt_id,
    )
    delivery = graph.delivery
    lane = graph.lane
    enrollment = graph.enrollment
    attempt = graph.attempt
    if (
        delivery.status != DripDelivery.Status.SENDING
        or delivery.current_task_id != reservation.task_id
        or attempt.outcome != DripDeliveryAttempt.Outcome.RESERVED
        or attempt.submission_attempted_at is None
    ):
        raise ValueError("Drip Gmail success cannot finalize changed delivery state")
    try:
        validated_provider_rfc_message_id(result.rfc_message_id)
    except ValueError:
        raise ValueError("Gmail returned an invalid RFC Message-ID")
    if reservation.raw_thread_id and result.thread_id != reservation.raw_thread_id:
        raise ValueError("Gmail returned another thread for a continuation")
    if lane.gmail_thread_id and lane.gmail_thread_id != result.thread_id:
        raise ValueError("Gmail result does not match the lane thread")

    owner_handle = resolve_sales_owner_handle(reservation.operator)
    message_owner = (
        SalesOwner.objects.filter(handle=owner_handle).first()
        if owner_handle
        else None
    )
    external_id = scoped_gmail_id(reservation.account_key, result.message_id)
    thread_external_id = scoped_gmail_id(reservation.account_key, result.thread_id)
    raw = {
        "source": "drip_gmail",
        "automation_key": f"drip_gmail:delivery:{delivery.pk}",
        "delivery_id": delivery.pk,
        "lane_id": lane.pk,
        "enrollment_id": enrollment.pk,
        "operator": reservation.operator,
        "gmail_account": reservation.account_key,
        "send_as": reservation.send_as,
        "reply_to": reservation.reply_to,
        "gmail_message_id": result.message_id,
        "gmail_thread_id": result.thread_id,
        "rfc_message_id": result.rfc_message_id,
        "thread_subject": reservation.subject,
        "references": list(reservation.references),
        "theme_key": delivery.theme_key,
        "theme_index": delivery.theme_index,
        "step_index": delivery.step_index,
    }
    now = timezone.now()
    message, created = Message.objects.get_or_create(
        source=Message.Source.GMAIL,
        external_id=external_id,
        defaults={
            "lead": enrollment.lead,
            "operator": message_owner,
            "direction": Message.Direction.OUTBOUND,
            "sender": reservation.send_as,
            "body": reservation.body,
            "sent_at": now,
            "thread_external_id": thread_external_id,
            "raw": raw,
        },
    )
    if not created:
        if message.lead_id != enrollment.lead_id:
            raise ValueError("Gmail provider message ID belongs to another Lead")
        existing_key = (
            message.raw.get("automation_key")
            if isinstance(message.raw, dict)
            else None
        )
        if existing_key and existing_key != raw["automation_key"]:
            raise ValueError("Gmail provider message ID belongs to another delivery")
        prior_raw = message.raw if isinstance(message.raw, dict) else {}
        message.operator = message.operator or message_owner
        message.direction = Message.Direction.OUTBOUND
        message.sender = reservation.send_as
        message.body = reservation.body
        message.thread_external_id = thread_external_id
        message.raw = {**prior_raw, **raw}
        message.save(update_fields={
            "operator",
            "direction",
            "sender",
            "body",
            "thread_external_id",
            "raw",
        })

    if not lane.gmail_thread_id:
        lane.gmail_thread_id = result.thread_id
        lane.gmail_thread_subject = reservation.subject
        lane.save(update_fields={
            "gmail_thread_id",
            "gmail_thread_subject",
            "updated_at",
        })
    attempt.outcome = DripDeliveryAttempt.Outcome.SENT
    attempt.finished_at = now
    attempt.diagnostic_detail = ""
    attempt.save(update_fields={"outcome", "finished_at", "diagnostic_detail"})
    delivery.status = DripDelivery.Status.SENT
    delivery.sent_at = now
    delivery.outbound_message = message
    delivery.provider_message_id = result.message_id
    delivery.provider_thread_id = result.thread_id
    delivery.rfc_message_id = result.rfc_message_id
    delivery.rfc_references = " ".join(reservation.references)
    delivery.save(update_fields={
        "status",
        "sent_at",
        "outbound_message",
        "provider_message_id",
        "provider_thread_id",
        "rfc_message_id",
        "rfc_references",
        "updated_at",
    })
    return message


def handle_drip_gmail(task) -> None:
    """Execute one materialized Gmail delivery without touching LinkedIn state."""
    reservation = _reserve_attempt(task)
    if reservation is None:
        return

    try:
        delivery_permit = issue_gmail_delivery_permit(
            task=task,
            operator=reservation.operator,
            account_key=reservation.account_key,
            to=reservation.recipient,
            subject=reservation.subject,
            body=reservation.body,
            thread_id=reservation.raw_thread_id,
            in_reply_to=reservation.in_reply_to,
            references=reservation.references,
            rfc_message_id=reservation.rfc_message_id,
        )
        client = GmailClient(operator=reservation.operator)
        if (
            client.account_key != reservation.account_key
            or client.send_as != reservation.send_as
            or client.reply_to != reservation.reply_to
        ):
            raise ValueError("Resolved Gmail client does not match delivery ownership")
        result = client.send_message(
            to=reservation.recipient,
            subject=reservation.subject,
            body=reservation.body,
            delivery_permit=delivery_permit,
            thread_id=reservation.raw_thread_id,
            in_reply_to=reservation.in_reply_to,
            references=reservation.references,
            rfc_message_id=reservation.rfc_message_id,
            on_submit_attempt=lambda: _stamp_submission_attempt(reservation),
        )
        _record_success(reservation, result)
    except Exception as exc:
        _record_failure(reservation, exc)
        raise


@transaction.atomic
def recover_stale_drip_gmail_task(task_id: int) -> bool:
    """Recover one stale running Task without risking a duplicate Gmail send."""
    from linkedin.models import Task
    from drip.services.ownership import lock_delivery_graph

    candidate = Task.objects.filter(pk=task_id).first()
    if candidate is None or candidate.task_type != Task.TaskType.DRIP_GMAIL:
        return False
    if candidate.status != Task.Status.RUNNING:
        return False
    delivery_id = _payload_delivery_id(candidate)
    if not DripDelivery.objects.filter(pk=delivery_id).exists():
        task = Task.objects.select_for_update(of=("self",)).get(pk=task_id)
        task.status = Task.Status.FAILED
        task.error = "Stale drip Gmail Task lost delivery ownership"
        task.save(update_fields={"status", "error"})
        return True

    attempt_id = DripDeliveryAttempt.objects.filter(
        delivery_id=delivery_id,
        outcome=DripDeliveryAttempt.Outcome.RESERVED,
    ).order_by("-attempt_number").values_list("pk", flat=True).first()
    graph = lock_delivery_graph(
        delivery_id,
        attempt_id=attempt_id,
        task_id=task_id,
    )
    task = graph.task
    delivery = graph.delivery
    lane = graph.lane
    attempt = graph.attempt
    if task.status != Task.Status.RUNNING:
        return False
    if delivery.current_task_id != task.pk:
        task.status = Task.Status.FAILED
        task.error = "Stale drip Gmail Task lost delivery ownership"
        task.save(update_fields={"status", "error"})
        return True

    now = timezone.now()
    if delivery.status != DripDelivery.Status.SENDING:
        if delivery.status in {
            DripDelivery.Status.SENT,
            DripDelivery.Status.STOPPED,
            DripDelivery.Status.UNCLEAR,
            DripDelivery.Status.FAILED,
        }:
            task.status = Task.Status.COMPLETED
            task.completed_at = now
            task.error = "Delivery reached terminal state before stale recovery"
        else:
            task.status = Task.Status.FAILED
            task.error = f"Cannot recover delivery state {delivery.status}"
        task.save(update_fields={"status", "completed_at", "error"})
        return True
    if (
        attempt is None
        or attempt.outcome != DripDeliveryAttempt.Outcome.RESERVED
        or attempt.finished_at is not None
    ):
        delivery.status = DripDelivery.Status.UNCLEAR
        delivery.save(update_fields={"status", "updated_at"})
        if lane.status == DripLane.Status.ACTIVE:
            lane.status = DripLane.Status.PAUSED
            lane.save(update_fields={"status", "updated_at"})
        task.status = Task.Status.FAILED
        task.error = "Gmail stale recovery found an invalid attempt ledger"
        task.save(update_fields={"status", "error"})
        return True
    if attempt is not None and attempt.submission_attempted_at is not None:
        attempt.outcome = DripDeliveryAttempt.Outcome.UNCLEAR
        attempt.finished_at = now
        attempt.diagnostic_detail = "Worker stopped after Gmail submission began"
        attempt.save(update_fields={"outcome", "finished_at", "diagnostic_detail"})
        delivery.status = DripDelivery.Status.UNCLEAR
        delivery.save(update_fields={"status", "updated_at"})
        if lane.status == DripLane.Status.ACTIVE:
            lane.status = DripLane.Status.PAUSED
            lane.save(update_fields={"status", "updated_at"})
        task.status = Task.Status.FAILED
        task.error = "Gmail submission outcome is unclear after worker restart"
        task.save(update_fields={"status", "error"})
        return True

    attempt.outcome = DripDeliveryAttempt.Outcome.NOT_SUBMITTED
    attempt.finished_at = now
    attempt.diagnostic_detail = "Worker stopped before Gmail submission began"
    attempt.save(update_fields={"outcome", "finished_at", "diagnostic_detail"})
    controls_active = (
        graph.enrollment.campaign.status == DripCampaign.Status.ACTIVE
        and graph.enrollment.status == DripEnrollment.Status.ACTIVE
        and lane.status == DripLane.Status.ACTIVE
    )
    if not controls_active:
        delivery.status = DripDelivery.Status.PLANNED
        delivery.current_task = None
        delivery.save(update_fields={"status", "current_task", "updated_at"})
        task.status = Task.Status.COMPLETED
        task.completed_at = now
        task.error = "Drip controls inactive during stale recovery"
        task.save(update_fields={"status", "completed_at", "error"})
        return True
    delivery.status = DripDelivery.Status.QUEUED
    delivery.save(update_fields={"status", "updated_at"})

    task.status = Task.Status.PENDING
    task.started_at = None
    task.completed_at = None
    task.error = ""
    task.save(update_fields={"status", "started_at", "completed_at", "error"})
    return True
