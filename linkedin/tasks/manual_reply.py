"""Slack-triggered manual LinkedIn replies."""
from __future__ import annotations

import logging

from crm.models import Lead
from linkedin.db.leads import lead_to_profile_dict
from linkedin.notifications.slack import notify_manual_reply_failed, notify_manual_reply_sent
from linkedin.operators import resolve_operator

logger = logging.getLogger(__name__)


def _manual_reply_already_sent(*, lead: Lead, operator: str, message: str) -> bool:
    """Return True if this exact manual reply was already persisted.

    Covers the crash window where LinkedIn accepted the send but the daemon
    died before marking the Task completed. The persisted crm.Message is the
    durable idempotency ledger.
    """
    from crm.models import Message

    prior_senders = (
        Message.objects.filter(
            lead=lead,
            source=Message.Source.LINKEDIN,
            direction=Message.Direction.OUTBOUND,
            external_id__startswith="manual-reply:",
            body=message,
        )
        .exclude(sender="")
        .values_list("sender", flat=True)
    )
    return operator in {resolve_operator(sender) for sender in prior_senders}


def handle_manual_reply(task, session, qualifiers):
    """Send a Slack-composed reply from the daemon's logged-in account.

    This lane intentionally does not change Deal state, count against daemon
    quotas, or advance follow-up sequences. The outbound message is still
    persisted to crm.Message by send_raw_message.
    """
    from linkedin.actions.message import send_raw_message

    payload = task.payload or {}
    operator = (payload.get("operator") or "").strip()
    message = (payload.get("message") or "").strip()
    our_operator = resolve_operator(session.linkedin_profile.linkedin_username)

    try:
        if operator != our_operator:
            raise ValueError(
                f"Manual reply for {operator or 'unknown operator'} cannot be sent by {our_operator}"
            )
        if not message:
            raise ValueError("Manual reply message is empty")

        lead = Lead.objects.filter(pk=payload.get("lead_id")).first()
        if lead is None:
            raise ValueError(f"Lead {payload.get('lead_id')} not found")
        if lead.disqualified:
            raise ValueError(f"Lead {lead.pk} is disqualified")

        profile = lead_to_profile_dict(lead)
        if not profile:
            raise ValueError(f"Lead {lead.pk} has no LinkedIn public identifier")

        if _manual_reply_already_sent(lead=lead, operator=our_operator, message=message):
            logger.info(
                "manual_reply already sent to %s by %s — skipping duplicate task",
                profile.get("public_identifier"),
                our_operator,
            )
            return

        sent = send_raw_message(
            session,
            profile,
            message,
            operator=our_operator,
            external_id_kind="manual-reply",
            prefer_direct=True,
            allow_api_fallback=False,
            raise_on_failure=True,
        )
        if not sent:
            raise RuntimeError(f"LinkedIn send failed for lead {lead.pk}")

        lead_name = " ".join(part for part in [lead.first_name, lead.last_name] if part)
        notify_manual_reply_sent(payload, lead_name=lead_name)
        logger.info(
            "manual_reply sent to %s by %s",
            profile.get("public_identifier"),
            our_operator,
        )
    except Exception as exc:
        notify_manual_reply_failed(payload, str(exc))
        raise
