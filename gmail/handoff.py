"""Campaign-selected Gmail cadence scheduling.

This module owns the durable task contract for the browserless Gmail lane. The
connect/sweep paths call `maybe_schedule_gmail_sequence` after acceptance or a
confirmed invitation. The Campaign chooses the anchor; acceptance never starts
a second email sequence. Gmail remains independent from LinkedIn follow-ups.
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
    if delivery is not None and delivery.status in {
        delivery.Status.STOPPED, delivery.Status.UNCLEAR,
    }:
        logger.info("gmail_follow_up held for delivery %s: %s", delivery.pk, delivery.status)
        return None
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
    if delivery is not None and delivery.status not in {
        delivery.Status.PLANNED, delivery.Status.QUEUED,
    }:
        logger.info("email enrichment held for delivery %s: %s", delivery.pk, delivery.status)
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

    # A repeated acceptance or restart must not purchase the same failed lookup
    # again. A later manually supplied address can still enqueue this delivery.
    if delivery is not None:
        previous = Task.objects.filter(
            task_type=Task.TaskType.ENRICH_EMAIL,
            payload__delivery_id=delivery.pk,
            payload__operator=operator,
        ).order_by("pk").first()
        if previous is not None:
            logger.info("email enrichment held for delivery %s: prior lookup Task %s", delivery.pk, previous.pk)
            return previous

    from linkedin.models import Campaign
    enrich_immediately = bool(
        delivery is not None
        and delivery.enrollment.deal.campaign.gmail_start_mode
        == Campaign.GmailStartMode.INVITATION_SENT
    )
    return Task.objects.create(
        task_type=Task.TaskType.ENRICH_EMAIL,
        scheduled_at=(
            timezone.now() if enrich_immediately else delivery.scheduled_at
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


@transaction.atomic
def _maybe_schedule_gmail_sequence(*, deal, operator: str):
    """Materialize the first Gmail step once, under the Lead ownership lock."""
    if not ENABLE_GMAIL_SEQUENCE:
        logger.info("gmail cadence skipped for lead %s: Gmail sequence disabled", deal.lead_id)
        return None
    from crm.models import Deal
    from drip.services.ownership import lock_lead_outbound_ownership
    from linkedin.enums import ProfileState
    from linkedin.models import Campaign, LinkedInProfile
    from linkedin.operators import resolve_operator

    lock_lead_outbound_ownership(deal.lead_id)
    deal = Deal.objects.select_for_update(of=("self",)).select_related(
        "lead", "campaign__active_message_version__program",
    ).get(pk=deal.pk)
    invitation_start = deal.campaign.gmail_start_mode == Campaign.GmailStartMode.INVITATION_SENT
    if invitation_start:
        owner_handle = LinkedInProfile.objects.filter(
            user_id=deal.campaign.user_id,
        ).values_list("linkedin_username", flat=True).first()
        if not owner_handle or resolve_operator(owner_handle) != operator:
            logger.info("gmail cadence skipped for Deal %s: invitation sender ownership mismatch", deal.pk)
            return None
        if (
            not deal.campaign.active_message_version_id
            or deal.invitation_sent_at is None
            or deal.invitation_sender != operator
            or deal.invitation_withdrawn_at is not None
            or deal.state not in {ProfileState.PENDING, ProfileState.CONNECTED, ProfileState.COMPLETED}
        ):
            logger.info("gmail cadence skipped for Deal %s: no qualifying confirmed invitation", deal.pk)
            return None
        reference_at = deal.invitation_sent_at
    else:
        if deal.state not in {ProfileState.CONNECTED, ProfileState.COMPLETED}:
            logger.info("gmail cadence skipped for Deal %s: waiting for acceptance", deal.pk)
            return None
        reference_at = deal.connected_at or timezone.now()
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
        delivery, created = get_or_create_delivery(
            enrollment=enrollment,
            channel=OutboundDelivery.Channel.GMAIL,
            step_key=first_step.step_key,
            step_index=first_step.step_index,
            reference_at=reference_at,
        )
        # Hooks start missing work; they are not a retry or sent-step recovery
        # command. Keep all terminal/interrupted identities and schedules intact.
        if delivery.status not in {delivery.Status.PLANNED, delivery.Status.QUEUED}:
            logger.info("gmail cadence held for delivery %s: %s", delivery.pk, delivery.status)
            return None
        if delivery.task_id is not None:
            return delivery.task
        if created and invitation_start:
            from linkedin.tasks.follow_up import _normalize_linkedin_due_at

            delivery.scheduled_at = _normalize_linkedin_due_at(
                delivery.scheduled_at, current_time=timezone.now(),
            )
            delivery.save(update_fields={"scheduled_at", "updated_at"})
        if delivery.frozen_media:
            from linkedin.message_delivery_runtime import mark_delivery_status

            mark_delivery_status(delivery, delivery.Status.FAILED)
            logger.error(
                "Gmail delivery %s held as failed: Gmail attachments are not supported",
                delivery.pk,
            )
            return None
        enqueue_kwargs = {
            "lead_id": deal.lead_id,
            "operator": operator,
            "deal_id": deal.pk,
            "step_index": delivery.step_index,
            "delivery_id": delivery.pk,
            "message_version_id": enrollment.message_version_id,
        }
        from gmail.addresses import usable_email
        if usable_email(deal.lead.email):
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


def recover_invitation_gmail_sequences(*, operator: str, campaign_ids, limit: int = 100) -> int:
    """One bounded startup pass for confirmed opt-in invitations missing work.

    This cannot backfill a legacy campaign or reset any existing Gmail Task.
    Repeat passes are safe: the canonical hook locks exact Lead/delivery identity.
    A scoped audit cursor rotates past temporarily held rows instead of letting
    a full page of missing-copy/stopped leads starve later eligible invitations.
    """
    from crm.models import Deal
    from django.db.models import CharField, Exists, OuterRef, Q
    from django.db.models.fields.json import KeyTextTransform
    from django.db.models.functions import Cast
    from linkedin.enums import ProfileState
    from linkedin.models import Campaign, OutboundDelivery, Task, WorkflowRun
    from linkedin.operators import CANONICAL_OPERATOR_HANDLES

    if not ENABLE_GMAIL_SEQUENCE:
        return 0
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise ValueError("invitation Gmail recovery limit must be between 1 and 1000")
    if operator not in CANONICAL_OPERATOR_HANDLES:
        raise ValueError("invitation Gmail recovery requires a canonical operator")
    campaign_ids = list(campaign_ids)
    if any(isinstance(pk, bool) or not isinstance(pk, int) or pk <= 0 for pk in campaign_ids):
        raise ValueError("invitation Gmail recovery requires positive campaign IDs")
    campaign_ids = sorted(set(campaign_ids))
    if not campaign_ids or not _operator_can_send_gmail(operator):
        return 0
    # Exclude handled work before applying the cap, so repeat restarts can reach
    # later missing hooks instead of repeatedly scanning the first sent batch.
    handled = OutboundDelivery.objects.filter(
        enrollment__deal_id=OuterRef("pk"), channel=OutboundDelivery.Channel.GMAIL,
    ).exclude(status=OutboundDelivery.Status.PLANNED, task__isnull=True)
    enrichment = Task.objects.annotate(
        _deal_key=KeyTextTransform("deal_id", "payload"),
    ).filter(
        task_type=Task.TaskType.ENRICH_EMAIL,
        _deal_key=Cast(OuterRef("pk"), output_field=CharField()),
        payload__operator=operator,
    )
    deals = Deal.objects.filter(
        campaign_id__in=campaign_ids,
        campaign__status=Campaign.Status.ACTIVE,
        campaign__gmail_start_mode=Campaign.GmailStartMode.INVITATION_SENT,
        campaign__active_message_version__isnull=False,
        state__in=(ProfileState.PENDING, ProfileState.CONNECTED, ProfileState.COMPLETED),
        lead__disqualified=False,
        invitation_sender=operator,
        invitation_sent_at__isnull=False,
        invitation_withdrawn_at__isnull=True,
    ).annotate(_handled=Exists(handled), _lookup=Exists(enrichment)).filter(
        Q(_lookup=False) | ~Q(lead__email=""), _handled=False,
    ).order_by("pk")
    audit_name = "invitation-gmail-recovery"
    previous = WorkflowRun.objects.filter(
        name=audit_name, operator=operator, counts__campaign_ids=campaign_ids,
    ).order_by("-completed_at", "-pk").first()
    cursor = previous.counts.get("last_deal_id", 0) if previous else 0
    if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
        raise ValueError("invalid invitation Gmail recovery cursor")
    batch = list(deals.filter(pk__gt=cursor)[:limit])
    if not batch and cursor:
        batch = list(deals[:limit])
    if not batch:
        return 0
    scheduled = 0
    for deal in batch:
        task = maybe_schedule_gmail_sequence(deal=deal, operator=operator)
        # A completed/failed lookup is deliberately returned by the canonical
        # hook to avoid buying it again. It is a hold, not scheduled work.
        scheduled += int(task is not None and task.status in _pending_or_running())
    WorkflowRun.objects.create(
        name=audit_name, operator=operator,
        summary=f"Scanned {len(batch)} invitations; {scheduled} pending/running work items",
        counts={
            "campaign_ids": campaign_ids, "last_deal_id": batch[-1].pk,
            "scanned": len(batch), "scheduled": scheduled,
        },
    )
    logger.info(
        "Invitation Gmail recovery for %s: scanned %d, %d work item(s), cursor %d",
        operator, len(batch), scheduled, batch[-1].pk,
    )
    return scheduled
