"""gmail_follow_up task handler - browserless Gmail cadence sequence."""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from gmail.addresses import usable_email
from gmail.auth import GMAIL_OPERATOR_MAPPING
from gmail.client import (
    GmailClient,
    GmailSendResult,
    scoped_gmail_id,
    validated_provider_rfc_message_id,
)
from gmail.delivery import issue_gmail_delivery_permit
from gmail.exceptions import GmailSubmissionDeferred
from gmail.handoff import DEFAULT_GMAIL_SEQUENCE_NAME, enqueue_gmail_follow_up
from gmail.submission import (
    current_gmail_automation_key,
    defer_early_current_gmail_task,
    stamp_submission_attempt,
    submission_attempted,
)
from gmail.templates import render_for_icp, steps_for_icp
from linkedin.conf import ENABLE_GMAIL_SEQUENCE
from linkedin.exceptions import SheetsError
from linkedin.icp_outbound import resolve_icp
from linkedin.tasks.follow_up import _delay_seconds_to_active_due
from linkedin.tasks.stop_checks import lead_automation_stop_reason

logger = logging.getLogger(__name__)


def _set_delivery_status(delivery, status: str, *, sent_at=None) -> None:
    if delivery is None:
        return
    from linkedin.message_delivery_runtime import mark_delivery_status

    mark_delivery_status(delivery, status)
    if sent_at is not None and delivery.sent_at != sent_at:
        delivery.sent_at = sent_at
        delivery.save(update_fields={"sent_at", "updated_at"})


def _drip_owns_gmail(lead_id: int) -> bool:
    from drip.models import DripLane
    from drip.services.ownership import drip_owns_channel

    return drip_owns_channel(
        lead_id=lead_id,
        channel=DripLane.Channel.GMAIL,
    )


def _gmail_step_prefix(
    *,
    operator: str,
    lead_id: int,
    sequence_name: str,
    step_index: int,
) -> str:
    """Legacy pre-provider-ID key retained only for sent-step discovery."""
    return f"gmail-send:{operator}:{lead_id}:{sequence_name}:step-{step_index}:"


def _gmail_automation_key(
    *,
    operator: str,
    lead_id: int,
    sequence_name: str,
    step_index: int,
    delivery_id: int | None = None,
    message_version_id: int | None = None,
) -> str:
    payload = {
        "operator": operator,
        "lead_id": lead_id,
        "sequence_name": sequence_name,
        "step_index": step_index,
    }
    if delivery_id is not None or message_version_id is not None:
        payload["delivery_id"] = delivery_id
        payload["message_version_id"] = message_version_id
    automation_key = current_gmail_automation_key(payload)
    if not automation_key:
        raise ValueError("Current Gmail automation identity is invalid")
    return automation_key


def _sent_gmail_step(
    *,
    lead,
    operator: str,
    sequence_name: str,
    step_index: int,
    delivery_id: int | None = None,
    message_version_id: int | None = None,
    enrollment_id: int | None = None,
):
    from crm.models import Message

    if delivery_id is not None:
        return Message.objects.filter(
            lead=lead,
            source=Message.Source.GMAIL,
            direction=Message.Direction.OUTBOUND,
            raw__delivery_id=delivery_id,
            raw__message_version_id=message_version_id,
        ).order_by("pk").first()
    if enrollment_id is not None:
        return Message.objects.filter(
            lead=lead,
            source=Message.Source.GMAIL,
            direction=Message.Direction.OUTBOUND,
            raw__enrollment_id=enrollment_id,
            raw__message_version_id=message_version_id,
            raw__step_index=step_index,
        ).order_by("pk").first()
    automation_key = _gmail_automation_key(
        operator=operator,
        lead_id=lead.id,
        sequence_name=sequence_name,
        step_index=step_index,
    )
    canonical = Message.objects.filter(
        lead=lead,
        source=Message.Source.GMAIL,
        direction=Message.Direction.OUTBOUND,
        raw__automation_key=automation_key,
    ).order_by("pk").first()
    if canonical is not None:
        return canonical
    return Message.objects.filter(
        lead=lead,
        source=Message.Source.GMAIL,
        direction=Message.Direction.OUTBOUND,
        external_id__startswith=_gmail_step_prefix(
            operator=operator,
            lead_id=lead.id,
            sequence_name=sequence_name,
            step_index=step_index,
        ),
    ).order_by("pk").first()


def _enqueue_next_step(
    task,
    *,
    reference_time,
    delay_hours: float | None = None,
    delivery=None,
):
    payload = task.payload or {}
    if delivery is not None:
        from linkedin.message_delivery import (
            get_or_create_delivery,
            list_message_steps,
        )
        from linkedin.models import OutboundDelivery

        enrollment = delivery.enrollment
        steps = list_message_steps(
            enrollment=enrollment,
            channel=OutboundDelivery.Channel.GMAIL,
        )
        current_positions = [
            index
            for index, step in enumerate(steps)
            if step.step_key == delivery.step_key
            and step.step_index == delivery.step_index
        ]
        if len(current_positions) != 1:
            raise ValueError("frozen Gmail delivery is absent from its message version")
        next_position = current_positions[0] + 1
        if next_position >= len(steps):
            return None
        next_step = steps[next_position]
        next_delivery, _created = get_or_create_delivery(
            enrollment=enrollment,
            channel=OutboundDelivery.Channel.GMAIL,
            step_key=next_step.step_key,
            step_index=next_step.step_index,
            reference_at=reference_time,
        )
        if next_delivery.frozen_media:
            _set_delivery_status(next_delivery, next_delivery.Status.FAILED)
            raise ValueError(
                "versioned Gmail delivery includes media, but Gmail attachments "
                "are not supported"
            )
        next_task = enqueue_gmail_follow_up(
            lead_id=enrollment.deal.lead_id,
            operator=enrollment.operator,
            deal_id=enrollment.deal_id,
            sequence_name=(
                payload.get("sequence_name") or DEFAULT_GMAIL_SEQUENCE_NAME
            ),
            step_index=next_delivery.step_index,
            delivery_id=next_delivery.pk,
            message_version_id=enrollment.message_version_id,
        )
        if next_task is None:
            _set_delivery_status(next_delivery, next_delivery.Status.STOPPED)
        return next_task

    if delay_hours is None:
        raise ValueError("legacy Gmail successor requires delay_hours")
    delay_seconds = _delay_seconds_to_active_due(
        delay_hours,
        reference_time=reference_time,
    )
    return enqueue_gmail_follow_up(
        lead_id=int(payload["lead_id"]),
        operator=payload["operator"],
        deal_id=payload.get("deal_id"),
        sequence_name=(
            payload.get("sequence_name") or DEFAULT_GMAIL_SEQUENCE_NAME
        ),
        step_index=int(payload.get("step_index") or 0) + 1,
        delay_seconds=delay_seconds,
    )


@dataclass(frozen=True)
class _ThreadContext:
    raw_thread_id: str = ""
    subject: str = ""
    in_reply_to: str = ""
    references: tuple[str, ...] = ()


def _rfc_message_id(*, automation_key: str, send_as: str) -> str:
    """Create a stable RFC Message-ID for retries of one automation step."""
    digest = hashlib.sha256(automation_key.encode("utf-8")).hexdigest()[:32]
    domain = send_as.rsplit("@", 1)[-1]
    return f"<openoutreach-{digest}@{domain}>"


def _required_binding(
    message,
    *,
    account_key: str,
    send_as: str,
) -> tuple[str, str, str, tuple[str, ...]]:
    """Read and validate an exact owned Gmail continuation binding."""
    raw = message.raw if isinstance(message.raw, dict) else {}
    raw_thread_id = str(raw.get("gmail_thread_id") or "").strip()
    rfc_message_id = str(raw.get("rfc_message_id") or "").strip()
    thread_subject = str(raw.get("thread_subject") or "").strip()
    references_value = raw.get("references", [])
    if not isinstance(references_value, list) or not all(
        isinstance(value, str) and value.strip() for value in references_value
    ):
        raise ValueError("stored Gmail References metadata is invalid")
    try:
        references = tuple(
            validated_provider_rfc_message_id(value)
            for value in references_value
        )
    except ValueError:
        raise ValueError("stored Gmail References metadata is invalid")

    if raw.get("gmail_account") != account_key:
        raise ValueError("stored Gmail message belongs to another mailbox")
    if str(raw.get("send_as") or message.sender).strip().lower() != send_as.lower():
        raise ValueError("stored Gmail message belongs to another Send-As alias")
    if not raw_thread_id or not rfc_message_id or not thread_subject:
        raise ValueError("stored Gmail message lacks exact thread metadata")
    try:
        validated_provider_rfc_message_id(rfc_message_id)
    except ValueError:
        raise ValueError("stored Gmail RFC Message-ID is invalid")
    if message.thread_external_id != scoped_gmail_id(account_key, raw_thread_id):
        raise ValueError("stored Gmail CRM thread ID does not match its raw binding")
    return raw_thread_id, rfc_message_id, thread_subject, references


def _thread_context(
    *,
    lead,
    operator: str,
    sequence_name: str,
    step_index: int,
    account_key: str,
    send_as: str,
    enrollment_id: int | None = None,
    message_version_id: int | None = None,
    delivery=None,
) -> _ThreadContext:
    if delivery is not None:
        from linkedin.message_delivery import list_message_steps
        from linkedin.models import OutboundDelivery

        steps = list_message_steps(
            enrollment=delivery.enrollment,
            channel=OutboundDelivery.Channel.GMAIL,
        )
        positions = [
            index
            for index, step in enumerate(steps)
            if step.step_key == delivery.step_key
            and step.step_index == delivery.step_index
        ]
        if len(positions) != 1:
            raise ValueError("frozen Gmail delivery is absent from its message version")
        position = positions[0]
        if position == 0:
            return _ThreadContext()
        first_step_index = steps[0].step_index
        previous_step_index = steps[position - 1].step_index
    else:
        if step_index == 0:
            return _ThreadContext()
        first_step_index = 0
        previous_step_index = step_index - 1

    first = _sent_gmail_step(
        lead=lead,
        operator=operator,
        sequence_name=sequence_name,
        step_index=first_step_index,
        enrollment_id=enrollment_id,
        message_version_id=message_version_id,
    )
    previous = _sent_gmail_step(
        lead=lead,
        operator=operator,
        sequence_name=sequence_name,
        step_index=previous_step_index,
        enrollment_id=enrollment_id,
        message_version_id=message_version_id,
    )
    if first is None or previous is None:
        raise ValueError(
            f"Gmail step {step_index} cannot send before its exact predecessor"
        )

    first_thread, _first_rfc_id, subject, _first_references = _required_binding(
        first,
        account_key=account_key,
        send_as=send_as,
    )
    previous_thread, previous_rfc_id, _subject, previous_references = (
        _required_binding(
            previous,
            account_key=account_key,
            send_as=send_as,
        )
    )
    if previous_thread != first_thread:
        raise ValueError("current Gmail sequence is split across multiple threads")
    references = tuple(dict.fromkeys((*previous_references, previous_rfc_id)))
    return _ThreadContext(
        raw_thread_id=first_thread,
        subject=subject,
        in_reply_to=previous_rfc_id,
        references=references,
    )


def _persist_outbound(
    *,
    lead,
    send_result: GmailSendResult,
    account_key: str,
    sender: str,
    reply_to: str,
    subject: str,
    body: str,
    operator: str,
    sequence_name: str,
    step_index: int,
    references: tuple[str, ...],
    delivery=None,
):
    from crm.models import Message, SalesOwner
    from linkedin.operators import resolve_sales_owner_handle

    owner_handle = resolve_sales_owner_handle(operator)
    message_owner = (
        SalesOwner.objects.filter(handle=owner_handle).first()
        if owner_handle
        else None
    )
    automation_key = _gmail_automation_key(
        operator=operator,
        lead_id=lead.id,
        sequence_name=sequence_name,
        step_index=step_index,
        delivery_id=delivery.pk if delivery is not None else None,
        message_version_id=(
            delivery.enrollment.message_version_id
            if delivery is not None
            else None
        ),
    )
    external_id = scoped_gmail_id(account_key, send_result.message_id)
    thread_external_id = scoped_gmail_id(account_key, send_result.thread_id)
    raw = {
        "source": "gmail_follow_up",
        "automation_key": automation_key,
        "operator": operator,
        "sequence_name": sequence_name,
        "step_index": step_index,
        "gmail_account": account_key,
        "send_as": sender.lower(),
        "reply_to": reply_to.lower(),
        "gmail_message_id": send_result.message_id,
        "gmail_thread_id": send_result.thread_id,
        "rfc_message_id": send_result.rfc_message_id,
        "thread_subject": subject,
        "references": list(references),
    }
    if delivery is not None:
        raw.update({
            "delivery_id": delivery.pk,
            "message_version_id": delivery.enrollment.message_version_id,
            "enrollment_id": delivery.enrollment_id,
            "audience_key": delivery.enrollment.audience_key,
            "step_key": delivery.step_key,
            "variant_key": delivery.variant_key,
        })
    defaults = {
        "lead": lead,
        "operator": message_owner,
        "direction": Message.Direction.OUTBOUND,
        "sender": sender,
        "body": body,
        "sent_at": timezone.now(),
        "thread_external_id": thread_external_id,
        "raw": raw,
    }
    with transaction.atomic():
        message, created = Message.objects.get_or_create(
            source=Message.Source.GMAIL,
            external_id=external_id,
            defaults=defaults,
        )
        if created:
            return message
        if message.lead_id != lead.id:
            raise ValueError(
                "Gmail provider message ID is already owned by another Lead"
            )
        existing_key = (
            message.raw.get("automation_key")
            if isinstance(message.raw, dict)
            else None
        )
        if existing_key and existing_key != automation_key:
            raise ValueError(
                "Gmail provider message ID is already owned by another delivery"
            )
        message.operator = message.operator or message_owner
        message.direction = Message.Direction.OUTBOUND
        message.sender = sender
        message.body = body
        message.thread_external_id = thread_external_id
        prior_raw = message.raw if isinstance(message.raw, dict) else {}
        message.raw = {**prior_raw, **raw}
        message.save(update_fields={
            "operator",
            "direction",
            "sender",
            "body",
            "thread_external_id",
            "raw",
        })
        return message


def _is_missing_template_error(exc: SheetsError) -> bool:
    """Missing or blank Gmail copy disables only the Gmail lane."""
    message = str(exc)
    return (
        "has no block" in message
        or "has no ICP" in message
        or "has no step" in message
        or "has no ICP mapping" in message
        or "must be a non-empty step list" in message
        or "needs subject_variants" in message
        or "needs body_variants" in message
    )


def handle_gmail_follow_up(task) -> None:
    from crm.models import Lead

    if not ENABLE_GMAIL_SEQUENCE:
        logger.info("gmail_follow_up: ENABLE_GMAIL_SEQUENCE=false - skipping")
        return

    payload = task.payload or {}
    lead_id = payload["lead_id"]
    operator = payload["operator"]
    sequence_name = payload.get("sequence_name") or DEFAULT_GMAIL_SEQUENCE_NAME
    step_index = int(payload.get("step_index") or 0)

    lead = Lead.objects.filter(pk=lead_id).first()
    if lead is None:
        logger.warning("gmail_follow_up: lead %s not found - skipping", lead_id)
        return
    from gmail.handoff import (
        _bound_gmail_delivery,
        _current_deal_campaign_is_active,
    )

    delivery = _bound_gmail_delivery(
        deal_id=payload.get("deal_id"),
        lead_id=lead.id,
        operator=operator,
        step_index=step_index,
        delivery_id=payload.get("delivery_id"),
        message_version_id=payload.get("message_version_id"),
        task_id=task.pk,
    )
    if delivery is not None and delivery.frozen_media:
        _set_delivery_status(delivery, delivery.Status.FAILED)
        raise ValueError(
            "versioned Gmail delivery includes media, but Gmail attachments "
            "are not supported"
        )
    if delivery is not None and delivery.status in {
        delivery.Status.STOPPED,
        delivery.Status.UNCLEAR,
        delivery.Status.FAILED,
        delivery.Status.PLANNED,
    }:
        raise ValueError(
            f"versioned Gmail delivery {delivery.pk} status "
            f"{delivery.status!r} is not executable"
        )
    if not _current_deal_campaign_is_active(payload.get("deal_id")):
        logger.info(
            "gmail_follow_up: Deal %s campaign is not active - skipping",
            payload.get("deal_id"),
        )
        if delivery is not None:
            _set_delivery_status(delivery, delivery.Status.STOPPED)
        return
    if _drip_owns_gmail(lead.id):
        logger.info("gmail_follow_up: lead %s skipped - drip owns Gmail", lead_id)
        if delivery is not None:
            _set_delivery_status(delivery, delivery.Status.STOPPED)
        return
    stop_reason = lead_automation_stop_reason(lead)
    if stop_reason:
        logger.info("gmail_follow_up: lead %s stopped - %s", lead_id, stop_reason)
        if delivery is not None:
            _set_delivery_status(delivery, delivery.Status.STOPPED)
        return

    if delivery is not None:
        rendered_subject = delivery.frozen_subject
        rendered_body = delivery.frozen_body
        steps = None
    else:
        try:
            icp = resolve_icp(lead)
            if not icp:
                logger.info("gmail_follow_up: missing ICP for lead %s - skipping", lead_id)
                return
            rendered = render_for_icp(
                sender=operator,
                icp=icp,
                sequence_name=sequence_name,
                lead=lead,
                step_index=step_index,
            )
            steps = steps_for_icp(sender=operator, icp=icp, sequence_name=sequence_name)
            rendered_subject = rendered.subject
            rendered_body = rendered.body
        except SheetsError as exc:
            if _is_missing_template_error(exc):
                logger.info(
                    "gmail_follow_up: missing template for lead %s - %s",
                    lead_id,
                    exc,
                )
                return
            raise

    next_step_index = step_index + 1
    sent_step = _sent_gmail_step(
        lead=lead,
        operator=operator,
        sequence_name=sequence_name,
        step_index=step_index,
        delivery_id=delivery.pk if delivery is not None else None,
        message_version_id=(
            delivery.enrollment.message_version_id
            if delivery is not None
            else None
        ),
    )
    if sent_step is not None:
        if delivery is not None:
            _set_delivery_status(
                delivery,
                delivery.Status.SENT,
                sent_at=sent_step.sent_at,
            )
            _enqueue_next_step(
                task,
                delivery=delivery,
                reference_time=sent_step.sent_at,
            )
        elif next_step_index < len(steps):
            mapping = GMAIL_OPERATOR_MAPPING.get(operator)
            if mapping is None:
                raise ValueError(f"No Gmail mapping configured for operator {operator!r}")
            try:
                _thread_context(
                    lead=lead,
                    operator=operator,
                    sequence_name=sequence_name,
                    step_index=next_step_index,
                    account_key=mapping["gmail_account"],
                    send_as=mapping["send_as"],
                )
            except ValueError as exc:
                logger.error(
                    "gmail_follow_up: existing step for lead %s has no safe "
                    "continuation binding - %s",
                    lead_id,
                    exc,
                )
                return
            _enqueue_next_step(
                task,
                delay_hours=steps[next_step_index].delay_hours,
                reference_time=sent_step.sent_at,
            )
        logger.info("gmail_follow_up: step already sent for lead %s", lead_id)
        return
    if delivery is not None and delivery.status == delivery.Status.SENT:
        raise ValueError(
            f"versioned Gmail delivery {delivery.pk} is marked sent without "
            "persisted Message evidence"
        )
    if delivery is not None and delivery.status == delivery.Status.SENDING:
        _set_delivery_status(delivery, delivery.Status.UNCLEAR)
        raise ValueError(
            f"versioned Gmail delivery {delivery.pk} has an unresolved "
            "submission attempt"
        )
    if submission_attempted(task.payload):
        if delivery is not None:
            _set_delivery_status(delivery, delivery.Status.UNCLEAR)
        raise ValueError(
            "gmail_follow_up: prior Gmail submission outcome is unclear; "
            "automatic retry is blocked"
        )

    if not usable_email(lead.email):
        if delivery is not None:
            _set_delivery_status(delivery, delivery.Status.FAILED)
        raise ValueError(f"gmail_follow_up: lead {lead_id} has no usable email")

    if defer_early_current_gmail_task(task):
        logger.info("gmail_follow_up: Task %s deferred until %s", task.pk, task.scheduled_at)
        return

    mapping = GMAIL_OPERATOR_MAPPING.get(operator)
    if mapping is None:
        raise ValueError(f"No Gmail mapping configured for operator {operator!r}")
    context = _thread_context(
        lead=lead,
        operator=operator,
        sequence_name=sequence_name,
        step_index=step_index,
        account_key=mapping["gmail_account"],
        send_as=mapping["send_as"],
        enrollment_id=delivery.enrollment_id if delivery is not None else None,
        message_version_id=(
            delivery.enrollment.message_version_id
            if delivery is not None
            else None
        ),
        delivery=delivery,
    )
    subject = (
        rendered_subject
        if delivery is not None
        else context.subject or rendered_subject
    )
    automation_key = _gmail_automation_key(
        operator=operator,
        lead_id=lead.id,
        sequence_name=sequence_name,
        step_index=step_index,
        delivery_id=delivery.pk if delivery is not None else None,
        message_version_id=(
            delivery.enrollment.message_version_id
            if delivery is not None
            else None
        ),
    )

    # Hourly context sync is the sole Gmail reply-ingestion path. This final
    # DB-only recheck closes known-stop races immediately before submission.
    stop_reason = lead_automation_stop_reason(lead)
    if stop_reason:
        logger.info(
            "gmail_follow_up: lead %s stopped before send - %s",
            lead_id,
            stop_reason,
        )
        if delivery is not None:
            _set_delivery_status(delivery, delivery.Status.STOPPED)
        return
    if _drip_owns_gmail(lead.id):
        logger.info(
            "gmail_follow_up: lead %s handed off before send - skipping",
            lead_id,
        )
        if delivery is not None:
            _set_delivery_status(delivery, delivery.Status.STOPPED)
        return

    def _recheck_before_submission() -> None:
        if not _current_deal_campaign_is_active(payload.get("deal_id")):
            if delivery is not None:
                _set_delivery_status(delivery, delivery.Status.STOPPED)
            raise ValueError("gmail_follow_up: campaign stopped before submission")
        if _drip_owns_gmail(lead.id):
            if delivery is not None:
                _set_delivery_status(delivery, delivery.Status.STOPPED)
            raise ValueError(
                f"gmail_follow_up: lead {lead_id} handed off before submission"
            )
        callback_stop_reason = lead_automation_stop_reason(lead)
        if callback_stop_reason:
            if delivery is not None:
                _set_delivery_status(delivery, delivery.Status.STOPPED)
            raise ValueError(
                f"gmail_follow_up: lead {lead_id} stopped before submission - "
                f"{callback_stop_reason}"
            )
        if not stamp_submission_attempt(task):
            raise GmailSubmissionDeferred(
                f"gmail_follow_up: Task {task.pk} deferred until {task.scheduled_at}"
            )

    rfc_message_id = _rfc_message_id(
        automation_key=automation_key,
        send_as=mapping["send_as"],
    )
    delivery_permit = issue_gmail_delivery_permit(
        task=task,
        operator=operator,
        account_key=mapping["gmail_account"],
        to=lead.email,
        subject=subject,
        body=rendered_body,
        thread_id=context.raw_thread_id,
        in_reply_to=context.in_reply_to,
        references=context.references,
        rfc_message_id=rfc_message_id,
    )
    client = GmailClient(operator=operator)
    if (
        client.account_key != mapping["gmail_account"]
        or client.send_as != mapping["send_as"].lower()
        or client.reply_to != mapping["reply_to"].lower()
    ):
        raise ValueError("Resolved Gmail client does not match Task ownership")
    try:
        send_result = client.send_message(
            to=lead.email,
            subject=subject,
            body=rendered_body,
            delivery_permit=delivery_permit,
            thread_id=context.raw_thread_id,
            in_reply_to=context.in_reply_to,
            references=context.references,
            rfc_message_id=rfc_message_id,
            on_submit_attempt=_recheck_before_submission,
        )
        sent_message = _persist_outbound(
            lead=lead,
            send_result=send_result,
            account_key=client.account_key,
            sender=client.send_as,
            reply_to=client.reply_to,
            subject=subject,
            body=rendered_body,
            operator=operator,
            sequence_name=sequence_name,
            step_index=step_index,
            references=context.references,
            delivery=delivery,
        )
    except GmailSubmissionDeferred:
        logger.info("gmail_follow_up: Task %s deferred before submission", task.pk)
        return
    except Exception:
        if delivery is not None:
            delivery.refresh_from_db(fields=["status"])
        if delivery is not None and delivery.status not in {
            delivery.Status.SENT,
            delivery.Status.STOPPED,
            delivery.Status.UNCLEAR,
        }:
            task.refresh_from_db(fields=["payload"])
            status = (
                delivery.Status.UNCLEAR
                if submission_attempted(task.payload)
                else delivery.Status.FAILED
            )
            _set_delivery_status(delivery, status)
        raise

    if delivery is not None:
        _set_delivery_status(
            delivery,
            delivery.Status.SENT,
            sent_at=sent_message.sent_at,
        )

    if delivery is not None:
        _enqueue_next_step(
            task,
            delivery=delivery,
            reference_time=sent_message.sent_at,
        )
    elif next_step_index < len(steps):
        _enqueue_next_step(
            task,
            delay_hours=steps[next_step_index].delay_hours,
            reference_time=sent_message.sent_at,
        )
    logger.info("gmail_follow_up sent to lead=%s step=%s", lead_id, step_index)
