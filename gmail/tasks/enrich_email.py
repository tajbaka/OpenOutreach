"""enrich_email task handler - BetterContact email lookup for Gmail cadence."""
from __future__ import annotations

import logging
from email.utils import parseaddr

from linkedin.enrichment.base import EnrichmentResult, EnrichmentStatus
from linkedin.enrichment.providers.bettercontact import BetterContactEmailProvider

logger = logging.getLogger(__name__)


def _normalize_email(value: str) -> str:
    _, addr = parseaddr(value or "")
    return addr.strip().lower()


def _stop_delivery(delivery) -> None:
    if delivery is None or delivery.status in {
        delivery.Status.SENT,
        delivery.Status.STOPPED,
        delivery.Status.UNCLEAR,
    }:
        return
    from linkedin.message_delivery_runtime import mark_delivery_status

    mark_delivery_status(delivery, delivery.Status.STOPPED)


def _fail_delivery(delivery) -> None:
    if delivery is None or delivery.status in {
        delivery.Status.SENT,
        delivery.Status.STOPPED,
        delivery.Status.UNCLEAR,
    }:
        return
    from linkedin.message_delivery_runtime import mark_delivery_status

    mark_delivery_status(delivery, delivery.Status.FAILED)


def handle_enrich_email(task) -> EnrichmentResult | None:
    """Find one lead's email address for the Gmail cadence lane."""
    from crm.models import Lead
    from gmail.handoff import (
        _bound_gmail_delivery,
        _current_deal_campaign_is_active,
        enqueue_gmail_follow_up,
    )
    from linkedin.tasks.stop_checks import lead_automation_stop_reason

    lead_id = task.payload.get("lead_id")
    operator = task.payload.get("operator") or ""
    lead = Lead.objects.filter(pk=lead_id).first()
    if lead is None:
        logger.warning("enrich_email: lead %s not found - skipping", lead_id)
        return None
    step_index = int(task.payload.get("step_index") or 0)
    delivery = _bound_gmail_delivery(
        deal_id=task.payload.get("deal_id"),
        lead_id=lead.id,
        operator=operator,
        step_index=step_index,
        delivery_id=task.payload.get("delivery_id"),
        message_version_id=task.payload.get("message_version_id"),
    )
    if delivery is not None and delivery.frozen_media:
        _fail_delivery(delivery)
        raise ValueError(
            "versioned Gmail delivery includes media, but Gmail attachments "
            "are not supported"
        )
    if delivery is not None and delivery.status in {
        delivery.Status.SENT,
        delivery.Status.STOPPED,
        delivery.Status.UNCLEAR,
    }:
        raise ValueError(
            f"versioned Gmail delivery {delivery.pk} status "
            f"{delivery.status!r} cannot execute enrichment"
        )
    if not _current_deal_campaign_is_active(task.payload.get("deal_id")):
        logger.info(
            "enrich_email: Deal %s campaign is not active - skipping",
            task.payload.get("deal_id"),
        )
        _stop_delivery(delivery)
        return None
    from drip.models import DripLane
    from drip.services.ownership import drip_owns_channel

    if drip_owns_channel(lead_id=lead.id, channel=DripLane.Channel.GMAIL):
        logger.info("enrich_email: lead %s skipped - drip owns Gmail", lead_id)
        _stop_delivery(delivery)
        return None
    stop_reason = lead_automation_stop_reason(lead)
    if stop_reason:
        logger.info("enrich_email: lead %s stopped - %s", lead_id, stop_reason)
        _stop_delivery(delivery)
        return None
    if lead.email:
        logger.info("enrich_email: lead %s already has email - enqueueing Gmail", lead_id)
        gmail_task = enqueue_gmail_follow_up(
            lead_id=lead.id,
            operator=operator,
            deal_id=task.payload.get("deal_id"),
            sequence_name=task.payload.get("sequence_name") or "gmail_fallback",
            step_index=step_index,
            delivery_id=delivery.pk if delivery is not None else None,
            message_version_id=(
                delivery.enrollment.message_version_id
                if delivery is not None
                else None
            ),
        )
        if gmail_task is None:
            _stop_delivery(delivery)
        return None

    deal_id = task.payload.get("deal_id")

    provider = BetterContactEmailProvider()
    tried = list(lead.email_providers_tried or [])
    if provider.name in tried:
        logger.info(
            "enrich_email: lead %s - provider %s already tried, skipping",
            lead_id, provider.name,
        )
        _fail_delivery(delivery)
        return None

    result = provider.enrich(lead, task)
    if result.status in (EnrichmentStatus.FOUND, EnrichmentStatus.NOT_FOUND):
        if result.provider not in tried:
            tried.append(result.provider)
        lead.email_providers_tried = tried
        update_fields = ["email_providers_tried"]

        if result.status == EnrichmentStatus.FOUND and result.email:
            email = _normalize_email(result.email)
            if email:
                lead.email = email
                update_fields.append("email")

        lead.save(update_fields=update_fields)

        if result.status == EnrichmentStatus.FOUND and lead.email:
            gmail_task = enqueue_gmail_follow_up(
                lead_id=lead.id,
                operator=operator,
                deal_id=deal_id,
                sequence_name=task.payload.get("sequence_name") or "gmail_fallback",
                step_index=step_index,
                delivery_id=delivery.pk if delivery is not None else None,
                message_version_id=(
                    delivery.enrollment.message_version_id
                    if delivery is not None
                    else None
                ),
            )
            if gmail_task is None:
                _stop_delivery(delivery)
        elif delivery is not None:
            _fail_delivery(delivery)
    else:
        logger.warning(
            "enrich_email: BetterContact failed for lead %s - not recording tried",
            lead_id,
        )

    return result
