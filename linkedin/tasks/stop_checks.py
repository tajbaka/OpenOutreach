"""Shared DB-local automation stop checks."""
from __future__ import annotations


def automation_stop_reason(deal) -> str:
    """Return a human-readable reason automation should stop, or "".

    Reads only local DB state so send paths do not depend on Sheets or live
    external systems.
    """
    from crm.models import Meeting, Message

    if deal.lead.disqualified:
        return "Lead disqualified; automation stopped"
    if Meeting.objects.filter(lead=deal.lead).exists():
        return "Meeting exists; automation stopped"
    if Message.objects.filter(
        lead=deal.lead,
        source__in=[Message.Source.LINKEDIN, Message.Source.GMAIL],
        direction=Message.Direction.INBOUND,
    ).exists():
        return "Lead replied; automation stopped"
    return ""
