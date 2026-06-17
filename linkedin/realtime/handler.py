"""Realtime event handler: parse → resolve Lead → persist → Slack.

Called by the listener once per decoded SSE event. Every call is wrapped
in try/except — a bad event logs, Slack-error-notifies (deduped), and is
dropped; it never unwinds the daemon loop. Realtime is an enhancement,
never a hard dependency.

Outbound echoes (our own sends mirrored back on the stream) are still
persisted — that is harmless and idempotent with backfill_messages — but
direction is derived by `persist_thread` from the sender, and only INBOUND
messages trigger a Slack notification.
"""
from __future__ import annotations

import logging

from django.utils import timezone

from linkedin.conf import ENABLE_AUTO_PHONE_ENRICHMENT
from linkedin.notifications.slack import notify_error, notify_message_received
from linkedin.realtime.lead_lookup import resolve_lead_for_realtime
from linkedin.realtime.parser import parse_realtime_event

logger = logging.getLogger(__name__)


def handle_realtime_event(event: dict, *, operator: str = "") -> None:
    """Process one decoded realtime SSE event. Never raises."""
    try:
        _handle(event, operator=operator)
    except Exception as exc:
        logger.exception("handle_realtime_event failed — event dropped")
        notify_error("daemon:realtime_listener", exc, context={"operator": operator})


def _maybe_enqueue_enrichment(lead) -> None:
    """Enqueue a waterfall phone-enrichment task for a freshly-replied lead.

    Gated by ENABLE_AUTO_PHONE_ENRICHMENT (default off — operators normally
    trigger enrichment from the Slack select menu). Skipped when the lead is
    disqualified, every provider has already been tried, or a PENDING/RUNNING
    waterfall enrich_phone task already exists — the last guard prevents
    duplicate provider billing when a lead sends several messages before the
    EnrichmentWorker runs.
    """
    if not ENABLE_AUTO_PHONE_ENRICHMENT:
        return
    if lead.disqualified:
        return

    from linkedin.enrichment.waterfall import PROVIDER_CHAIN
    from linkedin.models import Task

    tried = lead.phone_providers_tried or []
    if all(p.name in tried for p in PROVIDER_CHAIN):
        logger.debug("All phone providers already tried for %s — skipping", lead)
        return

    already = Task.objects.filter(
        task_type=Task.TaskType.ENRICH_PHONE,
        status__in=[Task.Status.PENDING, Task.Status.RUNNING],
        payload__lead_id=lead.id,
        payload__provider="waterfall",
    ).exists()
    if already:
        logger.debug("Phone enrichment already queued for %s — skipping", lead)
        return

    Task.objects.create(
        task_type=Task.TaskType.ENRICH_PHONE,
        scheduled_at=timezone.now(),
        payload={
            "lead_id": lead.id,
            "bettercontact_request_id": "",
            "provider": "waterfall",
        },
    )
    logger.info("Enqueued phone enrichment for %s", lead)


def _handle(event: dict, *, operator: str) -> None:
    from crm.models import Message
    from linkedin.db.messages import persist_thread

    parsed = parse_realtime_event(event)
    if parsed is None:
        return  # presence / typing / read-receipt / unparseable

    if not parsed.entity_urn:
        logger.debug("realtime: message event with no URN — skipped")
        return

    lead = resolve_lead_for_realtime(
        conversation_urn=parsed.conversation_urn,
        sender_member_urn=parsed.sender_member_urn,
    )
    if lead is None:
        logger.warning(
            "Realtime message from %r (conv=%s) — no matching Lead, skipped",
            parsed.sender_name, parsed.conversation_urn,
        )
        return

    created = persist_thread(
        lead=lead,
        parsed=[{
            "entity_urn": parsed.entity_urn,
            "sender": parsed.sender_name,
            "text": parsed.text,
            "timestamp": parsed.timestamp,
        }],
        thread_external_id=parsed.conversation_urn,
        outbound_senders={operator} if operator else set(),
    )
    if not created:
        logger.debug("Realtime event already persisted (%s)", parsed.entity_urn)
        return

    msg = Message.objects.get(
        source=Message.Source.LINKEDIN, external_id=parsed.entity_urn,
    )
    if msg.direction == Message.Direction.INBOUND:
        logger.info("Realtime inbound message persisted for %s", lead)
        notify_message_received(
            lead=lead,
            text=parsed.text,
            operator=operator,
            thread_external_id=msg.thread_external_id or parsed.conversation_urn,
        )
        _maybe_enqueue_enrichment(lead)
    else:
        logger.debug("Realtime outbound echo persisted for %s — no notify", lead)
