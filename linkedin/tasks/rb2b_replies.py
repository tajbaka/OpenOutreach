# linkedin/tasks/rb2b_replies.py
"""rb2b_replies — hourly catch-up for replies the realtime listener missed.

RB2B leads have no Deal, so the campaign backfill never covers them. The scan
pages back through Voyager's inbox (no navigation, no compose page) until it
passes the start of the last complete scan, matches 1:1 threads to stored RB2B
member URNs, and persists threads with new activity through the normal path.
New inbound messages then reach the replies channel once, sharing the
realtime listener's message identity and notification ledger. The window only
advances after a complete pass, so an interrupted or capped scan is retried
from the same point. Each run also resolves stranded send reservations.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import timedelta

from django.db.models import Min
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from linkedin.operators import resolve_operator

logger = logging.getLogger(__name__)

SCAN_OVERLAP = timedelta(minutes=10)
MAX_PAGES = 50
PAGE_SIZE = 20
PAGE_DELAY_SECONDS = 1.0


@dataclass
class InboxWindow:
    conversations: list[dict]  # newer than the cutoff, newest first
    complete: bool             # proven: passed the cutoff or reached the end of the inbox


def _box(raw, *keys) -> dict:
    """The validated response box; an error body or missing list raises."""
    from linkedin.exceptions import LinkedInInboxResponseError

    data = raw.get("data") if isinstance(raw, dict) and not raw.get("errors") else None
    box = next((data[key] for key in keys if isinstance(data, dict) and isinstance(data.get(key), dict)), None)
    if box is None or not isinstance(box.get("elements"), list):
        raise LinkedInInboxResponseError(f"Voyager response has no {keys[0]} list")
    return box


def inbox_since(api, cutoff_ms: int) -> InboxWindow:
    """Page the inbox newest-first until conversations older than ``cutoff_ms``.

    Same queries as the LinkedIn Messaging UI and discover_inbox_leads: the
    recent page, then older pages by ``nextCursor``, ``lastUpdatedBefore`` or,
    failing both, the base query's ``nextSyncToken``. Completion must be
    proven: a conversation older than the cutoff, or an empty page after at
    least one full one. A page that only repeats seen conversations still
    follows its continuation; a repeated request, a dead end or MAX_PAGES
    leaves the window incomplete, and invalid responses raise.
    """
    from linkedin.api.messaging import fetch_conversations, fetch_conversations_by_category

    found, seen, requests = [], set(), set()
    raw = fetch_conversations(api, count=PAGE_SIZE)
    for page in range(MAX_PAGES):
        box = _box(raw, "messengerConversationsBySyncToken", "messengerConversationsByCategoryQuery")
        elements = box["elements"]
        if not elements:
            return InboxWindow(found, page > 0)  # an empty first page proves nothing
        # A conversation without an activity time is kept, never taken as old.
        activity = [c.get("lastActivityAt") for c in elements]
        found += [c for c, at in zip(elements, activity)
                  if c.get("entityUrn") not in seen and (at is None or int(at) >= cutoff_ms)]
        seen.update(c.get("entityUrn") for c in elements)
        known = [int(at) for at in activity if at is not None]
        if known and min(known) < cutoff_ms:
            return InboxWindow(found, True)
        meta = box.get("metadata") or {}
        cursor, token = meta.get("nextCursor"), box.get("nextSyncToken") or meta.get("nextSyncToken")
        request = (("cursor", cursor) if cursor else ("before", min(known)) if known
                   else ("token", token) if token else None)
        if request is None or request in requests or page + 1 == MAX_PAGES:
            break
        requests.add(request)
        time.sleep(PAGE_DELAY_SECONDS)
        kind, value = request
        if kind == "cursor":
            raw = fetch_conversations_by_category(api, next_cursor=value, count=PAGE_SIZE)
        elif kind == "before":
            raw = fetch_conversations_by_category(api, last_updated_before=value, count=PAGE_SIZE)
        else:
            raw = fetch_conversations(api, sync_token=value, count=PAGE_SIZE)
    return InboxWindow(found, False)


def handle_rb2b_replies(task, session, qualifiers):
    from crm.models import Message
    from linkedin import rb2b
    from linkedin.actions.conversations import parse_messages
    from linkedin.api.client import PlaywrightLinkedinAPI
    from linkedin.api.messaging import fetch_messages
    from linkedin.api.messaging.utils import get_self_urn
    from linkedin.db.messages import persist_thread
    from linkedin.models import RB2BVisitor

    profile = session.linkedin_profile
    if resolve_operator(profile.linkedin_username) != rb2b.SENDER or not rb2b.connect_ready():
        return
    started = timezone.now()
    contacted_rows = RB2BVisitor.objects.filter(
        linkedin_profile=profile, status__in=["sent", "uncertain"], lead__isnull=False,
    ).exclude(member_urn="")
    # No reply can predate our invitation, so a first scan starts there.
    since = (parse_datetime(task.payload.get("since") or "")
             or contacted_rows.aggregate(at=Min("attempted_at"))["at"])
    # Schedule the next run first, carrying the same window, so a failed read
    # never ends the catch-up or skips the gap.
    rb2b.enqueue_reply_scan(at=started + timedelta(minutes=rb2b.REPLY_SCAN_MINUTES),
                            since=since.isoformat() if since else None)
    rb2b.resolve_interrupted_attempts(profile)

    contacted = {row.member_urn: row for row in contacted_rows.select_related("lead")}
    if contacted and since:
        cutoff = since - SCAN_OVERLAP
        session.ensure_browser()
        api = PlaywrightLinkedinAPI(session=session)
        self_urn = get_self_urn(api)
        window = inbox_since(api, int(cutoff.timestamp() * 1000))
        for conversation in window.conversations:
            others = [p.get("hostIdentityUrn") for p in conversation.get("conversationParticipants") or []
                      if p.get("hostIdentityUrn") != self_urn]
            row = contacted.get(others[0]) if len(others) == 1 else None  # 1:1 threads only
            thread = conversation.get("entityUrn") or ""
            if row is None or not thread:
                continue
            # Never re-attribute a thread already stored for another Lead.
            if Message.objects.filter(source="linkedin", thread_external_id=thread).exclude(lead=row.lead).exists():
                logger.warning("rb2b_replies: thread for %s belongs to another lead — skipped", row.public_identifier)
                continue
            raw_thread = fetch_messages(api, thread)
            _box(raw_thread, "messengerMessagesBySyncToken")  # an error body must not read as "no reply"
            parsed = parse_messages(raw_thread)
            if parsed:
                persist_thread(lead=row.lead, parsed=parsed, thread_external_id=thread,
                               outbound_senders={rb2b.SENDER})
        # Advance only after a proven-complete pass with every matched thread
        # read; any exception above leaves the queued run on the old window.
        if window.complete:
            rb2b.advance_reply_scan(since=started)
        else:
            logger.warning("rb2b_replies: inbox paging did not prove it reached %s; the next run retries "
                           "this window", cutoff)
    rb2b.flush_notifications()
