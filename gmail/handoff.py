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

from gmail.submission import persisted_submission_evidence, submission_attempted
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


def _current_deal_campaign_is_active(deal_id: int | None) -> bool:
    """Return false only when an exact current-outbound Deal is inactive.

    Current Gmail Tasks created by the post-accept handoff freeze ``deal_id``.
    Older manually queued Tasks may not have one, so absence cannot safely be
    mapped to an arbitrary Deal for a Lead that appears in several Campaigns.
    """
    if deal_id is None:
        return True

    from crm.models import Deal
    from linkedin.models import Campaign

    return Deal.objects.filter(
        pk=deal_id,
        campaign__status=Campaign.Status.ACTIVE,
    ).exists()


def _bound_gmail_delivery(
    *,
    deal_id: int | None,
    lead_id: int,
    operator: str,
    step_index: int,
    delivery_id: int | None,
    message_version_id: int | None,
    task_id: int | None = None,
):
    """Validate one exact versioned Gmail identity, or return legacy ``None``.

    A Campaign with an active version, or a Deal that already has a frozen
    enrollment, is permanently on the versioned path.  Partial/missing
    delivery identity is therefore an error rather than permission to fall
    back to mutable Lead/JSON routing.
    """
    if deal_id is None:
        if delivery_id is not None or message_version_id is not None:
            raise ValueError("versioned Gmail delivery requires an exact Deal")
        return None

    from crm.models import Deal
    from linkedin.message_delivery import (
        MessageDeliveryError,
        get_or_create_delivery,
        get_message_enrollment,
    )
    from linkedin.models import CampaignMessageEnrollment, OutboundDelivery

    deal = (
        Deal.objects.select_related("campaign__active_message_version__program", "lead")
        .filter(pk=deal_id)
        .first()
    )
    if deal is None:
        raise ValueError(f"Gmail Deal {deal_id} does not exist")
    if deal.lead_id != lead_id:
        raise ValueError("Gmail Task lead does not match its Deal")
    has_enrollment = CampaignMessageEnrollment.objects.filter(deal_id=deal.pk).exists()
    is_versioned = bool(deal.campaign.active_message_version_id or has_enrollment)
    if not is_versioned:
        if delivery_id is not None or message_version_id is not None:
            raise ValueError("legacy Gmail Deal cannot carry versioned delivery identity")
        return None
    if (
        isinstance(delivery_id, bool)
        or not isinstance(delivery_id, int)
        or delivery_id <= 0
        or isinstance(message_version_id, bool)
        or not isinstance(message_version_id, int)
        or message_version_id <= 0
    ):
        raise ValueError("versioned Gmail Task requires delivery_id and message_version_id")

    try:
        enrollment = get_message_enrollment(deal=deal)
    except MessageDeliveryError as exc:
        raise ValueError(f"invalid Gmail message enrollment: {exc}") from exc
    delivery = (
        OutboundDelivery.objects.select_related(
            "enrollment__message_version__program",
            "enrollment__deal__campaign",
            "enrollment__deal__lead",
            "task",
        )
        .filter(pk=delivery_id)
        .first()
    )
    if delivery is None:
        raise ValueError(f"versioned Gmail delivery {delivery_id} does not exist")
    if delivery.channel != OutboundDelivery.Channel.GMAIL:
        raise ValueError("versioned Gmail Task references another delivery channel")
    if delivery.enrollment_id != enrollment.pk:
        raise ValueError("versioned Gmail delivery belongs to another enrollment")
    if enrollment.message_version_id != message_version_id:
        raise ValueError("versioned Gmail Task message version does not match enrollment")
    if delivery.operator != operator or enrollment.operator != operator:
        raise ValueError("versioned Gmail delivery belongs to another operator")
    if delivery.step_index != step_index:
        raise ValueError("versioned Gmail delivery step does not match Task payload")
    try:
        validated_delivery, created = get_or_create_delivery(
            enrollment=enrollment,
            channel=OutboundDelivery.Channel.GMAIL,
            step_key=delivery.step_key,
            step_index=delivery.step_index,
        )
    except MessageDeliveryError as exc:
        raise ValueError(f"invalid frozen Gmail delivery: {exc}") from exc
    if created or validated_delivery.pk != delivery.pk:
        raise ValueError("versioned Gmail delivery route changed during validation")
    delivery = validated_delivery
    active_version = deal.campaign.active_message_version
    if (
        active_version is not None
        and active_version.program_id != enrollment.message_version.program_id
    ):
        raise ValueError("Gmail Deal campaign is bound to another message program")
    if task_id is not None and delivery.task_id != task_id:
        raise ValueError("versioned Gmail delivery is not bound to this Task")
    return delivery


@transaction.atomic
def enqueue_gmail_follow_up(
    *,
    lead_id: int,
    operator: str,
    deal_id: int | None = None,
    sequence_name: str = DEFAULT_GMAIL_SEQUENCE_NAME,
    step_index: int = DEFAULT_GMAIL_STEP_INDEX,
    delay_seconds: float = 0,
    delivery_id: int | None = None,
    message_version_id: int | None = None,
):
    """Create a Gmail follow-up task unless the same step is already queued."""
    from linkedin.models import Task

    if not ENABLE_GMAIL_SEQUENCE:
        return None
    if not operator:
        raise ValueError("enqueue_gmail_follow_up requires a non-empty operator")
    if not _current_deal_campaign_is_active(deal_id):
        logger.info(
            "gmail_follow_up enqueue skipped for lead %s: Deal %s campaign "
            "is not active",
            lead_id,
            deal_id,
        )
        return None
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

    delivery = _bound_gmail_delivery(
        deal_id=deal_id,
        lead_id=lead_id,
        operator=operator,
        step_index=step_index,
        delivery_id=delivery_id,
        message_version_id=message_version_id,
    )
    payload = {
        "lead_id": lead_id,
        "operator": operator,
        "sequence_name": sequence_name,
        "step_index": step_index,
    }
    if deal_id is not None:
        payload["deal_id"] = deal_id
    if delivery is not None:
        payload["delivery_id"] = delivery.pk
        payload["message_version_id"] = delivery.enrollment.message_version_id

    if delivery is not None and delivery.task_id is not None:
        candidates = [delivery.task]
    else:
        candidates = list(Task.objects.filter(
            task_type=Task.TaskType.GMAIL_FOLLOW_UP,
            payload__lead_id=lead_id,
            payload__operator=operator,
            payload__sequence_name=sequence_name,
            payload__step_index=step_index,
            **({"payload__delivery_id": delivery.pk} if delivery is not None else {}),
        ).order_by("pk"))
    for candidate in candidates:
        if delivery is not None:
            candidate_payload = candidate.payload or {}
            if (
                candidate.task_type != Task.TaskType.GMAIL_FOLLOW_UP
                or candidate_payload.get("deal_id") != deal_id
                or candidate_payload.get("lead_id") != lead_id
                or candidate_payload.get("operator") != operator
                or candidate_payload.get("step_index") != step_index
            ):
                raise ValueError(
                    "versioned Gmail delivery is bound to a mismatched Task"
                )
            _bound_gmail_delivery(
                deal_id=deal_id,
                lead_id=lead_id,
                operator=operator,
                step_index=step_index,
                delivery_id=candidate_payload.get("delivery_id"),
                message_version_id=candidate_payload.get("message_version_id"),
                task_id=candidate.pk,
            )
        if candidate.status in _pending_or_running():
            return candidate
    for candidate in candidates:
        if submission_attempted(candidate.payload):
            if (
                candidate.status == Task.Status.FAILED
                and persisted_submission_evidence(candidate.payload)
            ):
                candidate.status = Task.Status.PENDING
                candidate.started_at = None
                candidate.scheduled_at = timezone.now()
                candidate.error = ""
                candidate.save(update_fields={
                    "status",
                    "started_at",
                    "scheduled_at",
                    "error",
                })
                if delivery is not None:
                    from linkedin.message_delivery_runtime import mark_delivery_status

                    mark_delivery_status(delivery, delivery.Status.SENT)
            return candidate

    if delivery is not None and candidates:
        candidate = candidates[0]
        if candidate.status == Task.Status.FAILED:
            candidate.status = Task.Status.PENDING
            candidate.started_at = None
            candidate.scheduled_at = max(delivery.scheduled_at, timezone.now())
            candidate.error = ""
            candidate.save(update_fields={
                "status",
                "started_at",
                "scheduled_at",
                "error",
            })
            from linkedin.message_delivery_runtime import bind_delivery_task

            bind_delivery_task(delivery=delivery, task=candidate)
        return candidate

    task = Task.objects.create(
        task_type=Task.TaskType.GMAIL_FOLLOW_UP,
        scheduled_at=(
            delivery.scheduled_at
            if delivery is not None
            else timezone.now() + timedelta(seconds=max(delay_seconds, 0))
        ),
        payload=payload,
    )
    if delivery is not None:
        from linkedin.message_delivery_runtime import bind_delivery_task

        bind_delivery_task(delivery=delivery, task=task)
    return task


@transaction.atomic
def enqueue_email_enrichment(
    *,
    lead_id: int,
    operator: str,
    deal_id: int | None = None,
    sequence_name: str = DEFAULT_GMAIL_SEQUENCE_NAME,
    step_index: int = DEFAULT_GMAIL_STEP_INDEX,
    delay_seconds: float = 0,
    delivery_id: int | None = None,
    message_version_id: int | None = None,
):
    """Create an email-enrichment task unless one is already queued."""
    from linkedin.models import Task

    if not ENABLE_GMAIL_SEQUENCE:
        return None
    if not operator:
        raise ValueError("enqueue_email_enrichment requires a non-empty operator")
    if not _current_deal_campaign_is_active(deal_id):
        logger.info(
            "email enrichment enqueue skipped for lead %s: Deal %s campaign "
            "is not active",
            lead_id,
            deal_id,
        )
        return None
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

    delivery = _bound_gmail_delivery(
        deal_id=deal_id,
        lead_id=lead_id,
        operator=operator,
        step_index=step_index,
        delivery_id=delivery_id,
        message_version_id=message_version_id,
    )
    payload = {
        "lead_id": lead_id,
        "operator": operator,
        "sequence_name": sequence_name,
        "step_index": step_index,
        "bettercontact_email_request_id": "",
    }
    if deal_id is not None:
        payload["deal_id"] = deal_id
    if delivery is not None:
        payload["delivery_id"] = delivery.pk
        payload["message_version_id"] = delivery.enrollment.message_version_id

    existing = Task.objects.filter(
        task_type=Task.TaskType.ENRICH_EMAIL,
        status__in=_pending_or_running(),
        payload__lead_id=lead_id,
        payload__operator=operator,
    ).first()
    if existing is not None:
        if delivery is not None:
            existing_payload = existing.payload or {}
            if (
                existing_payload.get("deal_id") != deal_id
                or existing_payload.get("lead_id") != lead_id
                or existing_payload.get("operator") != operator
                or existing_payload.get("step_index") != step_index
            ):
                raise ValueError(
                    "versioned Gmail delivery has a mismatched enrichment Task"
                )
            _bound_gmail_delivery(
                deal_id=deal_id,
                lead_id=lead_id,
                operator=operator,
                step_index=step_index,
                delivery_id=existing_payload.get("delivery_id"),
                message_version_id=existing_payload.get("message_version_id"),
            )
        return existing

    return Task.objects.create(
        task_type=Task.TaskType.ENRICH_EMAIL,
        scheduled_at=(
            delivery.scheduled_at
            if delivery is not None
            else timezone.now() + timedelta(seconds=max(delay_seconds, 0))
        ),
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
    """Queue the first Gmail step from the post-accept cadence when eligible."""
    if not ENABLE_GMAIL_SEQUENCE:
        return None
    if not _current_deal_campaign_is_active(deal.pk):
        logger.info(
            "gmail cadence skipped for lead %s: campaign %s is not active",
            deal.lead_id,
            deal.campaign_id,
        )
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

    from linkedin.models import CampaignMessageEnrollment, OutboundDelivery

    existing_enrollment = CampaignMessageEnrollment.objects.filter(
        deal_id=deal.pk,
    ).exists()
    if deal.campaign.active_message_version_id or existing_enrollment:
        from linkedin.message_delivery import (
            ensure_message_enrollment,
            get_message_enrollment,
            get_or_create_delivery,
            index_message_version,
        )

        if existing_enrollment:
            enrollment = get_message_enrollment(deal=deal)
            if enrollment.operator != operator:
                raise ValueError(
                    "existing Gmail message enrollment belongs to another operator"
                )
        else:
            audience_input = str(deal.lead.icp or "").strip()
            if not audience_input:
                logger.info(
                    "gmail cadence skipped for lead %s: no frozen audience input",
                    deal.lead_id,
                )
                return None
            enrollment = ensure_message_enrollment(
                deal=deal,
                audience_key=audience_input,
                operator=operator,
            )
        routes = index_message_version(enrollment.message_version).effective_routes(
            audience_key=enrollment.audience_key,
            operator=enrollment.operator,
            channel=OutboundDelivery.Channel.GMAIL,
        )
        if not routes:
            logger.info("gmail cadence skipped for lead %s: no configured Gmail steps", deal.lead_id)
            return None
        first_step = routes[0][0]
        delivery, _created = get_or_create_delivery(
            enrollment=enrollment,
            channel=OutboundDelivery.Channel.GMAIL,
            step_key=first_step.step_key,
            step_index=first_step.step_index,
            reference_at=deal.connected_at or timezone.now(),
        )
        if delivery.frozen_media:
            from linkedin.message_delivery_runtime import mark_delivery_status

            mark_delivery_status(delivery, delivery.Status.FAILED)
            raise ValueError(
                "versioned Gmail delivery includes media, but Gmail attachments "
                "are not supported"
            )
        enqueue_kwargs = {
            "lead_id": deal.lead_id,
            "operator": operator,
            "deal_id": deal.pk,
            "step_index": delivery.step_index,
            "delivery_id": delivery.pk,
            "message_version_id": enrollment.message_version_id,
        }
        if deal.lead.email:
            queued_task = enqueue_gmail_follow_up(**enqueue_kwargs)
        else:
            queued_task = enqueue_email_enrichment(**enqueue_kwargs)
        if queued_task is None:
            from linkedin.message_delivery_runtime import mark_delivery_status

            mark_delivery_status(delivery, delivery.Status.STOPPED)
        return queued_task

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
