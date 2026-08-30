"""Shared DB-local automation stop checks and inbound stop notifications."""
from __future__ import annotations


def lead_automation_stop_reason(lead) -> str:
    """Return a human-readable reason automation should stop, or "".

    Reads only local DB state so send paths do not depend on Sheets or live
    external systems.  Reloading the Lead here keeps the send-boundary check
    authoritative when a long-running browser action outlives an earlier ORM
    snapshot.
    """
    from django.db.models import Q

    from crm.models import Lead, Meeting, Message
    from linkedin.suppression import lead_suppression_match

    lead = Lead.objects.get(pk=lead.pk)
    if lead.disqualified:
        return "Lead disqualified; automation stopped"

    suppression = lead_suppression_match(lead)
    if suppression is not None:
        return f"Suppression: {suppression.value}"

    if Meeting.objects.filter(Q(lead=lead) | Q(participants=lead)).distinct().exists():
        return "Meeting exists; automation stopped"
    if Message.objects.filter(
        lead=lead,
        source__in=[Message.Source.LINKEDIN, Message.Source.GMAIL],
        direction=Message.Direction.INBOUND,
    ).exists():
        return "Lead replied; automation stopped"
    return ""


def automation_stop_reason(deal) -> str:
    """Deal-compatible wrapper retained for current outbound callers."""
    return lead_automation_stop_reason(deal.lead)


def retire_pending_linkedin_follow_ups(lead, *, reason: str) -> int:
    """Retire resolvable pending current LinkedIn follow-ups for one Lead."""
    from django.db.models import Q
    from django.utils import timezone

    from linkedin.db.urls import url_to_public_id
    from linkedin.models import Task

    public_ids = {
        value
        for value in (
            (lead.public_identifier or "").strip(),
            url_to_public_id(lead.linkedin_url or ""),
        )
        if value
    }
    if not public_ids:
        return 0

    identity_q = Q()
    for public_id in public_ids:
        identity_q |= Q(payload__public_id=public_id)
    return Task.objects.filter(
        identity_q,
        task_type=Task.TaskType.FOLLOW_UP,
        status=Task.Status.PENDING,
    ).update(
        status=Task.Status.COMPLETED,
        completed_at=timezone.now(),
        error=reason,
    )


def retire_pending_current_gmail_work(lead, *, reason: str) -> int:
    """Retire pending current Gmail sends and automatic email lookup."""
    from django.utils import timezone

    from linkedin.models import Task

    return Task.objects.filter(
        task_type__in=(
            Task.TaskType.GMAIL_FOLLOW_UP,
            Task.TaskType.ENRICH_EMAIL,
        ),
        status=Task.Status.PENDING,
        payload__lead_id=lead.pk,
    ).update(
        status=Task.Status.COMPLETED,
        completed_at=timezone.now(),
        error=reason,
    )


def _handle_inbound_messages_persisted(
    message_ids: tuple[int, ...],
    *,
    source: str,
) -> None:
    from crm.models import Message
    from drip.services.stops import stop_for_inbound_message

    messages = Message.objects.filter(
        pk__in=message_ids,
        source=source,
        direction=Message.Direction.INBOUND,
    ).select_related("lead")
    for message in messages:
        reason = "Lead replied; automation stopped"
        retire_pending_linkedin_follow_ups(message.lead, reason=reason)
        retire_pending_current_gmail_work(message.lead, reason=reason)
        stop_for_inbound_message(message.pk)


def handle_inbound_linkedin_messages_persisted(message_ids: tuple[int, ...]) -> None:
    """Apply current and drip stop hooks after inbound Messages commit.

    ``persist_thread`` invokes this through ``transaction.on_commit`` so no
    downstream lifecycle write can make the Message transaction roll back.
    The drip service owns drip state; this module owns only the shared current
    LinkedIn follow-up cleanup.
    """
    from crm.models import Message

    _handle_inbound_messages_persisted(
        message_ids,
        source=Message.Source.LINKEDIN,
    )


def handle_inbound_gmail_messages_persisted(message_ids: tuple[int, ...]) -> None:
    """Apply current and drip stops after Gmail context ingestion commits."""
    from crm.models import Message

    _handle_inbound_messages_persisted(
        message_ids,
        source=Message.Source.GMAIL,
    )
