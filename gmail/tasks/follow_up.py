"""gmail_follow_up task handler - browserless Gmail cadence sequence."""
from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone

from gmail.client import GmailClient
from gmail.handoff import DEFAULT_GMAIL_SEQUENCE_NAME
from gmail.templates import render_for_icp, steps_for_icp
from linkedin.conf import ENABLE_GMAIL_SEQUENCE
from linkedin.exceptions import SheetsError
from linkedin.icp_outbound import resolve_icp
from linkedin.tasks.follow_up import _delay_seconds_to_active_due
from linkedin.tasks.stop_checks import automation_stop_reason

logger = logging.getLogger(__name__)


def _gmail_step_prefix(*, operator: str, lead_id: int, sequence_name: str, step_index: int) -> str:
    return f"gmail-send:{operator}:{lead_id}:{sequence_name}:step-{step_index}:"


def _has_sent_gmail_step(*, lead, operator: str, sequence_name: str, step_index: int) -> bool:
    from crm.models import Message

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
    ).exists()


def _enqueue_next_step(task, *, delay_hours: float, reference_time=None) -> None:
    from linkedin.models import Task

    payload = dict(task.payload)
    payload["step_index"] = int(payload.get("step_index") or 0) + 1
    delay_seconds = _delay_seconds_to_active_due(
        delay_hours,
        reference_time=reference_time,
    )
    Task.objects.create(
        task_type=Task.TaskType.GMAIL_FOLLOW_UP,
        status=Task.Status.PENDING,
        scheduled_at=timezone.now() + timedelta(seconds=delay_seconds),
        payload=payload,
    )


def _persist_outbound(*, lead, external_id: str, sender: str, body: str, thread_id: str = ""):
    from crm.models import Message

    return Message.objects.get_or_create(
        source=Message.Source.GMAIL,
        external_id=external_id,
        defaults={
            "lead": lead,
            "direction": Message.Direction.OUTBOUND,
            "sender": sender,
            "body": body,
            "sent_at": timezone.now(),
            "thread_external_id": thread_id,
            "raw": {"source": "gmail_follow_up"},
        },
    )[0]


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


def _sync_gmail_for_lead(*, lead, client: GmailClient) -> int:
    if not lead.email:
        return 0
    from linkedin.notifications.gmail_threads import persist_gmail_threads

    threads = client.search_threads_for_email(lead.email)
    return persist_gmail_threads(
        lead=lead,
        threads=threads,
        self_emails=client.send_as_aliases().keys(),
    )


def handle_gmail_follow_up(task) -> None:
    from crm.models import Deal, Lead

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
    if not lead.email:
        raise ValueError(f"gmail_follow_up: lead {lead_id} has no email")

    deal = None
    deal_id = payload.get("deal_id")
    if deal_id:
        deal = Deal.objects.filter(pk=deal_id).select_related("lead").first()

    from linkedin.suppression import lead_suppression_match

    suppression = lead_suppression_match(lead)
    if suppression:
        logger.info(
            "gmail_follow_up: lead %s blocked by suppression %s - skipping",
            lead_id, suppression.value,
        )
        return

    if deal is not None:
        stop_reason = automation_stop_reason(deal)
        if stop_reason:
            logger.info("gmail_follow_up: lead %s stopped - %s", lead_id, stop_reason)
            return

    if _has_sent_gmail_step(
        lead=lead,
        operator=operator,
        sequence_name=sequence_name,
        step_index=step_index,
    ):
        logger.info("gmail_follow_up: step already sent for lead %s", lead_id)
        return

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
    except SheetsError as exc:
        if _is_missing_template_error(exc):
            logger.info(
                "gmail_follow_up: missing template for lead %s - %s",
                lead_id,
                exc,
            )
            return
        raise

    client = GmailClient(operator=operator)
    _sync_gmail_for_lead(lead=lead, client=client)

    if deal is not None:
        stop_reason = automation_stop_reason(deal)
        if stop_reason:
            logger.info("gmail_follow_up: lead %s stopped after sync - %s", lead_id, stop_reason)
            return

    gmail_id = client.send_message(
        to=lead.email,
        subject=rendered.subject,
        body=rendered.body,
    )
    external_id = (
        f"{_gmail_step_prefix(operator=operator, lead_id=lead.id, sequence_name=sequence_name, step_index=step_index)}"
        f"{gmail_id}"
    )
    _persist_outbound(
        lead=lead,
        external_id=external_id,
        sender=client.send_as,
        body=rendered.body,
        thread_id=gmail_id,
    )

    next_step_index = step_index + 1
    if next_step_index < len(steps):
        reference_time = deal.connected_at if deal is not None else None
        _enqueue_next_step(
            task,
            delay_hours=steps[next_step_index].delay_hours,
            reference_time=reference_time,
        )
    logger.info("gmail_follow_up sent to lead=%s step=%s", lead_id, step_index)
