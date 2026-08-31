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

Crash-safety: any DB error here is logged-and-swallowed for established
text-send callers. Strict media callers re-read their exact synthetic Message
after this helper returns and fail closed without advancing when it is absent.
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
    external_id_kind: str = "daemon-send",
    raw: dict | None = None,
):
    """Persist an outbound LinkedIn message to `crm.Message`. Never raises."""
    try:
        from crm.models import Lead, Message, SalesOwner
        from linkedin.operators import resolve_sales_owner_handle

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
        send_operator = operator or sender
        owner_handle = resolve_sales_owner_handle(send_operator)
        message_owner = (
            SalesOwner.objects.filter(handle=owner_handle).first()
            if owner_handle
            else None
        )
        if external_id_kind == "manual-reply":
            external_id = f"manual-reply:{send_operator}:{lead.pk}:{int(now.timestamp())}"
        elif deal_id is not None and sequence_name and step_index is not None:
            external_id = (
                f"daemon-send:{send_operator}:{deal_id}:"
                f"{sequence_name}:step-{step_index}:{int(now.timestamp())}"
            )
        else:
            external_id = f"daemon-send:{lead.pk}:{int(now.timestamp())}"

        defaults = {
            "lead": lead,
            "operator": message_owner,
            "direction": Message.Direction.OUTBOUND,
            "sender": sender,
            "body": content,
            "sent_at": now,
        }
        if raw is not None:
            defaults["raw"] = dict(raw)

        Message.objects.get_or_create(
            source=Message.Source.LINKEDIN,
            external_id=external_id,
            defaults=defaults,
        )
        logger.debug("Saved outbound LinkedIn message for %s", public_identifier)
    except Exception as e:
        # Preserve the established non-raising contract. Strict media callers
        # verify their exact evidence after this returns; other senders rely on
        # the next backfill_messages pass to restore provider history.
        logger.warning(
            "save_chat_message: persist failed for %s (send already succeeded): %s",
            public_identifier, e,
        )
