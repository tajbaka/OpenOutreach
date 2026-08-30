"""Post-accept Gmail cadence scheduling.

This module owns the durable task contract for the browserless Gmail lane. The
connect/sweep paths call `maybe_schedule_gmail_sequence` when a lead reaches
CONNECTED, so Gmail timing is independent from the LinkedIn follow-up sequence.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from linkedin.conf import ENABLE_GMAIL_SEQUENCE

logger = logging.getLogger(__name__)

DEFAULT_GMAIL_SEQUENCE_NAME = "gmail_fallback"
DEFAULT_GMAIL_STEP_INDEX = 0


def _pending_or_running():
    from linkedin.models import Task

    return [Task.Status.PENDING, Task.Status.RUNNING]


def _known_stop_reason(lead_id: int) -> str:
    from crm.models import Lead
    from linkedin.tasks.stop_checks import lead_automation_stop_reason

    lead = Lead.objects.filter(pk=lead_id).first()
    if lead is None:
        return ""
    return lead_automation_stop_reason(lead)


@transaction.atomic
def enqueue_gmail_follow_up(
    *,
    lead_id: int,
    operator: str,
    deal_id: int | None = None,
    sequence_name: str = DEFAULT_GMAIL_SEQUENCE_NAME,
    step_index: int = DEFAULT_GMAIL_STEP_INDEX,
    delay_seconds: float = 0,
):
    """Create a Gmail follow-up task unless the same step is already queued."""
    from linkedin.models import Task

    if not ENABLE_GMAIL_SEQUENCE:
        return None
    if not operator:
        raise ValueError("enqueue_gmail_follow_up requires a non-empty operator")
    from drip.models import DripLane
    from drip.services.ownership import (
        drip_owns_channel,
        lock_lead_outbound_ownership,
    )

    lock_lead_outbound_ownership(lead_id)
    if drip_owns_channel(lead_id=lead_id, channel=DripLane.Channel.GMAIL):
        logger.info(
            "gmail_follow_up enqueue skipped for lead %s: drip owns Gmail",
            lead_id,
        )
        return None
    stop_reason = _known_stop_reason(lead_id)
    if stop_reason:
        logger.info(
            "gmail_follow_up enqueue skipped for lead %s: %s",
            lead_id,
            stop_reason,
        )
        return None

    payload = {
        "lead_id": lead_id,
        "operator": operator,
        "sequence_name": sequence_name,
        "step_index": step_index,
    }
    if deal_id is not None:
        payload["deal_id"] = deal_id

    existing = Task.objects.filter(
        task_type=Task.TaskType.GMAIL_FOLLOW_UP,
        status__in=_pending_or_running(),
        payload__lead_id=lead_id,
        payload__operator=operator,
        payload__sequence_name=sequence_name,
        payload__step_index=step_index,
    ).first()
    if existing is not None:
        return existing

    return Task.objects.create(
        task_type=Task.TaskType.GMAIL_FOLLOW_UP,
        scheduled_at=timezone.now() + timedelta(seconds=max(delay_seconds, 0)),
        payload=payload,
    )


@transaction.atomic
def enqueue_email_enrichment(
    *,
    lead_id: int,
    operator: str,
    deal_id: int | None = None,
    sequence_name: str = DEFAULT_GMAIL_SEQUENCE_NAME,
    step_index: int = DEFAULT_GMAIL_STEP_INDEX,
    delay_seconds: float = 0,
):
    """Create an email-enrichment task unless one is already queued."""
    from linkedin.models import Task

    if not ENABLE_GMAIL_SEQUENCE:
        return None
    if not operator:
        raise ValueError("enqueue_email_enrichment requires a non-empty operator")
    from drip.models import DripLane
    from drip.services.ownership import (
        drip_owns_channel,
        lock_lead_outbound_ownership,
    )

    lock_lead_outbound_ownership(lead_id)
    if drip_owns_channel(lead_id=lead_id, channel=DripLane.Channel.GMAIL):
        logger.info(
            "email enrichment enqueue skipped for lead %s: drip owns Gmail",
            lead_id,
        )
        return None
    stop_reason = _known_stop_reason(lead_id)
    if stop_reason:
        logger.info(
            "email enrichment enqueue skipped for lead %s: %s",
            lead_id,
            stop_reason,
        )
        return None

    payload = {
        "lead_id": lead_id,
        "operator": operator,
        "sequence_name": sequence_name,
        "step_index": step_index,
        "bettercontact_email_request_id": "",
    }
    if deal_id is not None:
        payload["deal_id"] = deal_id

    existing = Task.objects.filter(
        task_type=Task.TaskType.ENRICH_EMAIL,
        status__in=_pending_or_running(),
        payload__lead_id=lead_id,
        payload__operator=operator,
    ).first()
    if existing is not None:
        return existing

    return Task.objects.create(
        task_type=Task.TaskType.ENRICH_EMAIL,
        scheduled_at=timezone.now() + timedelta(seconds=max(delay_seconds, 0)),
        payload=payload,
    )


def _operator_can_send_gmail(operator: str) -> bool:
    from gmail.auth import GMAIL_OPERATOR_MAPPING

    return operator in GMAIL_OPERATOR_MAPPING


def maybe_schedule_gmail_sequence(*, deal, operator: str):
    """Best-effort Gmail scheduler.

    Gmail is an additive lane. Any failure here must never alter or fail the
    LinkedIn connect/sweep/follow-up path that called it.
    """
    try:
        return _maybe_schedule_gmail_sequence(deal=deal, operator=operator)
    except Exception:
        logger.exception(
            "gmail cadence scheduling failed for lead %s; LinkedIn flow continues",
            getattr(deal, "lead_id", "unknown"),
        )
        return None


def _maybe_schedule_gmail_sequence(*, deal, operator: str):
    """Queue Gmail step 0 from the post-accept cadence when eligible."""
    if not ENABLE_GMAIL_SEQUENCE:
        return None
    if not _operator_can_send_gmail(operator):
        logger.info("gmail cadence skipped for operator %s: no Gmail mapping", operator)
        return None

    from drip.models import DripLane
    from drip.services.ownership import drip_owns_channel

    if drip_owns_channel(
        lead_id=deal.lead_id,
        channel=DripLane.Channel.GMAIL,
    ):
        logger.info("gmail cadence skipped for lead %s: drip owns Gmail", deal.lead_id)
        return None

    from gmail.templates import steps_for_icp
    from linkedin.exceptions import SheetsError
    from linkedin.icp_outbound import resolve_icp
    from linkedin.suppression import lead_suppression_match
    from linkedin.tasks.follow_up import _delay_seconds_to_active_due
    from linkedin.tasks.stop_checks import automation_stop_reason

    stop_reason = automation_stop_reason(deal)
    if stop_reason:
        logger.info("gmail handoff skipped for lead %s: %s", deal.lead_id, stop_reason)
        return None

    suppression = lead_suppression_match(deal.lead)
    if suppression:
        logger.info(
            "gmail handoff skipped for lead %s: suppression %s",
            deal.lead_id, suppression.value,
        )
        return None

    try:
        icp = resolve_icp(deal.lead)
        if not icp:
            logger.info("gmail cadence skipped for lead %s: no ICP", deal.lead_id)
            return None
        steps = steps_for_icp(
            sender=operator,
            icp=icp,
            sequence_name=DEFAULT_GMAIL_SEQUENCE_NAME,
        )
    except SheetsError as exc:
        logger.info("gmail cadence skipped for lead %s: %s", deal.lead_id, exc)
        return None
    if not steps:
        return None

    delay_seconds = _delay_seconds_to_active_due(
        steps[0].delay_hours,
        reference_time=deal.connected_at,
    )
    if deal.lead.email:
        return enqueue_gmail_follow_up(
            lead_id=deal.lead_id,
            operator=operator,
            deal_id=deal.pk,
            delay_seconds=delay_seconds,
        )

    return enqueue_email_enrichment(
        lead_id=deal.lead_id,
        operator=operator,
        deal_id=deal.pk,
        delay_seconds=delay_seconds,
    )
