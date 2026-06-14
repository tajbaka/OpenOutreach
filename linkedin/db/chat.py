"""Persist an outbound LinkedIn DM into `crm.Message`.

Used by `linkedin.actions.message.send_raw_message` after a successful
send. Writes the message to the canonical `crm.Message` thread store
(same place `get_conversation` / `backfill_messages` write) so the
followup classifier and ball-on-court logic see it as an outbound.

The legacy `chat.ChatMessage` model lost its `is_outgoing` field at some
point but this helper kept passing it — every successful send crashed
on the persist step, preventing the Deal from being marked Completed
(Paul Baltzell incident, 2026-05-12). Rewritten to use `crm.Message`
with a synthetic `external_id` since the send paths
(`_send_msg_pop_up` / `_send_message` / Voyager API) don't return the
LinkedIn URN. The real URN gets captured later by `backfill_messages`
or the next `get_conversation` call; `crm.Message`'s
`(source, external_id)` uniqueness keeps the synthetic + URN versions
distinct without duplicating the body.

Crash-safety: any DB error here is logged-and-swallowed. A persist
failure must NOT unwind the send — the message already went out, the
Deal needs to advance to Completed regardless.
"""
import logging
from datetime import datetime, timezone

from linkedin.db.urls import public_id_to_url

logger = logging.getLogger(__name__)


def save_chat_message(
    session: "AccountSession",
    public_identifier: str,
    content: str,
    *,
    deal_id: int | None = None,
    sequence_name: str = "",
    step_index: int | None = None,
    operator: str = "",
):
    """Persist an outbound LinkedIn message to `crm.Message`. Never raises."""
    try:
        from crm.models import Lead, Message

        clean_url = public_id_to_url(public_identifier)
        lead = Lead.objects.filter(linkedin_url=clean_url).first()
        if not lead:
            logger.warning("save_chat_message: no Lead for %s", public_identifier)
            return

        now = datetime.now(timezone.utc)
        sender = (
            getattr(session.linkedin_profile, "linkedin_username", "")
            or getattr(session, "handle", "")
        )
        # Synthetic external_id — daemon's send paths don't return the
        # LinkedIn URN. Keyed on a sortable timestamp so two sends to the
        # same lead get distinct rows. backfill_messages will later add
        # the URN-keyed row alongside (different external_id, no collision).
        #
        # Sequence-aware callers provide the extra fields. Older callers keep
        # the historical shape so existing ad-hoc sends do not change format.
        if deal_id is not None and sequence_name and step_index is not None:
            send_operator = operator or sender
            external_id = (
                f"daemon-send:{send_operator}:{deal_id}:"
                f"{sequence_name}:step-{step_index}:{int(now.timestamp())}"
            )
        else:
            external_id = f"daemon-send:{lead.pk}:{int(now.timestamp())}"

        Message.objects.get_or_create(
            source=Message.Source.LINKEDIN,
            external_id=external_id,
            defaults={
                "lead": lead,
                "direction": Message.Direction.OUTBOUND,
                "sender": sender,
                "body": content,
                "sent_at": now,
            },
        )
        logger.debug("Saved outbound LinkedIn message for %s", public_identifier)
    except Exception as e:
        # Never let a persist failure undo a successful send. The message
        # already went out; the worst we can do is fail to record it, and
        # the next backfill_messages pass will pick it up from LinkedIn.
        logger.warning(
            "save_chat_message: persist failed for %s (send already succeeded): %s",
            public_identifier, e,
        )
