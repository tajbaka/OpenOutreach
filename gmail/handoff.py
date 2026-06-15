"""LinkedIn-to-Gmail fallback handoff.

This module owns the durable task contract for the browserless Gmail lane. The
LinkedIn follow-up handler calls only `maybe_handoff_to_gmail` after the final
LinkedIn sequence step succeeds.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone

from linkedin.conf import ENABLE_GMAIL_SEQUENCE

logger = logging.getLogger(__name__)

DEFAULT_GMAIL_SEQUENCE_NAME = "gmail_fallback"
DEFAULT_GMAIL_STEP_INDEX = 0


def _pending_or_running():
    from linkedin.models import Task

    return [Task.Status.PENDING, Task.Status.RUNNING]


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


def enqueue_email_enrichment(
    *,
    lead_id: int,
    operator: str,
    deal_id: int | None = None,
    sequence_name: str = DEFAULT_GMAIL_SEQUENCE_NAME,
    step_index: int = DEFAULT_GMAIL_STEP_INDEX,
):
    """Create an email-enrichment task unless one is already queued."""
    from linkedin.models import Task

    if not ENABLE_GMAIL_SEQUENCE:
        return None
    if not operator:
        raise ValueError("enqueue_email_enrichment requires a non-empty operator")

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
        scheduled_at=timezone.now(),
        payload=payload,
    )


def maybe_handoff_to_gmail(*, deal, operator: str):
    """Queue the Gmail fallback lane when the final LinkedIn step gets no reply."""
    if not ENABLE_GMAIL_SEQUENCE:
        return None

    from linkedin.suppression import lead_suppression_match
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

    if deal.lead.email:
        return enqueue_gmail_follow_up(
            lead_id=deal.lead_id,
            operator=operator,
            deal_id=deal.pk,
        )

    return enqueue_email_enrichment(
        lead_id=deal.lead_id,
        operator=operator,
        deal_id=deal.pk,
    )
