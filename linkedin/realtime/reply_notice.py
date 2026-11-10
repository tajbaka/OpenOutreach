"""One Slack reply notice per inbound LinkedIn message.

The realtime listener, the listener's missed-reply catch-up and
`backfill_messages` can each meet the same inbound message. Each claims the
message's notice on its own row (`Message.raw[NOTICE_KEY]`) before posting,
so a reply is announced once, whichever path reaches it first, and a reply
another path saved silently (for example the connection sweep) can still be
announced later. Legacy-linked leads keep their durable outbox
(`linkedin.legacy_rb2b`), which has its own sent marker.

A failed post releases the claim so a later path can retry. Slack webhooks
offer no idempotency key, so a lost acknowledgement can still duplicate a
notice; it never re-sends anything on LinkedIn.
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

NOTICE_KEY = "slack_reply_notice_at"

POSTED = "posted"
ALREADY_SENT = "already_sent"
OUTBOX = "outbox"
FAILED = "failed"


def reply_notice_sent(message) -> bool:
    """Whether this message's Slack reply notice has been claimed."""
    return bool((message.raw or {}).get(NOTICE_KEY))


def _set_claim(message_id: int, *, claimed: bool) -> bool:
    """Atomically claim or release the notice. Returns False if unchanged."""
    from crm.models import Message

    with transaction.atomic():
        row = Message.objects.select_for_update().get(pk=message_id)
        raw = dict(row.raw or {})
        if claimed == bool(raw.get(NOTICE_KEY)):
            return False
        if claimed:
            raw[NOTICE_KEY] = timezone.now().isoformat()
        else:
            raw.pop(NOTICE_KEY, None)
        Message.objects.filter(pk=message_id).update(raw=raw)
    return True


def deliver_reply_notice(message, *, lead, operator: str, notify=None) -> str:
    """Post this inbound message's Slack notice unless it was already posted.

    `notify` defaults to `slack.notify_message_received`, looked up at call
    time. Returns POSTED, ALREADY_SENT, OUTBOX (handed to the legacy outbox)
    or FAILED (claim released for a later retry).
    """
    from linkedin.legacy_rb2b import flush_notifications, owns_reply_notice

    if owns_reply_notice(message):
        flush_notifications()
        return OUTBOX
    if not _set_claim(message.pk, claimed=True):
        return ALREADY_SENT
    if notify is None:
        from linkedin.notifications import slack
        notify = slack.notify_message_received
    ok = notify(
        lead=lead,
        text=message.body,
        operator=operator,
        thread_external_id=message.thread_external_id,
    )
    if not ok:
        _set_claim(message.pk, claimed=False)
        return FAILED
    return POSTED
