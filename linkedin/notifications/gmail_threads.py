"""Gmail thread merge for the followup workflow.

The followup classifier needs to answer "whose move is it?" — and that
question is wrong if we only look at LinkedIn DMs. A lead may have gone
silent on LinkedIn and replied via email; from a ball-on-court lens
they're "ball on us" but the LinkedIn-only view classifies them "cold
thread" or worse. This module fixes that by persisting Gmail messages
into `crm.Message` (source=gmail) and exposing helpers that operate on
the union of all sources for a lead.

Why persist instead of merging in memory each run? `crm.Message` already
has `(source, external_id)` uniqueness, so re-running is a free no-op
upsert — same idempotency contract as `linkedin.db.messages.persist_thread`
for LinkedIn. Subsequent runs read from DB without paying the MCP-call
cost again, and downstream features (synthesis, sheet status derivation)
get email context for free.

Direction inference for Gmail: a message is OUTBOUND if its From header
matches any address in the `self_emails` set the caller passes in;
otherwise INBOUND. Caller resolves `self_emails` at run start from the
connected Gmail account itself (primary mailbox + Send-As aliases via
Gmail Profile API), rather than from a static env-var list — see
`docs/data-sync-workflow.md` Phase 0 for the resolution snippet. The
lead's `Lead.email` is what we matched the thread on, so anything from
that email is by definition the lead replying.

This module does NOT call the Gmail MCP itself — MCP tools are only
callable from the Claude Code harness. The orchestrator (interactive
session, cron task, or wrapper script) fetches via MCP and hands the raw
thread payloads to `persist_gmail_threads`. That keeps the Python module
testable without a Gmail mock and decouples credentials/tokens.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr, parsedate_to_datetime
from typing import Iterable

from django.db import transaction
from django.utils import timezone as dj_tz

from crm.models import Message

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def persist_gmail_threads(
    *,
    lead,
    threads: list[dict],
    self_emails: Iterable[str],
    operator: str = "",
) -> int:
    """Upsert all messages in the supplied Gmail thread payloads.

    `threads` is the raw response shape produced by Gmail MCP search_threads
    or get_thread — a list of thread dicts, each with a `messages`
    collection. We're tolerant of missing fields because both the MCP and
    Gmail's own API surface vary across endpoints.

    `self_emails` is the set of email addresses that count as "us" for
    direction inference (primary + any Send-As aliases). Caller is
    expected to resolve this from the connected Gmail account at run
    start — typically via Gmail's `users.getProfile` + `users.settings.sendAs.list`
    — rather than from a static env-var list. That removes the brittle
    "remember every alias" config maintenance.

    Returns the number of NEW Message rows created. Re-running with the
    same payload is a no-op (idempotent on `(source, external_id)`).
    """
    self_set = {(e or "").strip().lower() for e in self_emails}
    self_set.discard("")
    if not self_set:
        # Without any address marked as "us", direction inference is
        # meaningless — caller has misconfigured. Crash early per project's
        # error-handling rule.
        from linkedin.exceptions import SheetsError
        raise SheetsError(
            "gmail_threads: `self_emails` must be a non-empty set of "
            "addresses we treat as outbound senders (primary + Send-As "
            "aliases of the connected account)."
        )

    from crm.models import SalesOwner
    from linkedin.operators import resolve_sales_owner_handle

    owner_handle = resolve_sales_owner_handle(operator)
    message_owner = (
        SalesOwner.objects.filter(handle=owner_handle).first()
        if owner_handle
        else None
    )
    created = 0
    with transaction.atomic():
        for thread in threads or []:
            thread_id = (thread.get("id") or thread.get("threadId") or "").strip()
            for raw_msg in _iter_messages(thread):
                external_id = (raw_msg.get("id") or "").strip()
                if not external_id:
                    continue

                from_addr = _extract_from(raw_msg)
                direction = (
                    Message.Direction.OUTBOUND
                    if from_addr in self_set
                    else Message.Direction.INBOUND
                )
                sent_at = _extract_timestamp(raw_msg)
                body = _extract_body(raw_msg)
                sender = from_addr or _extract_sender_display(raw_msg)

                stored, was_created = Message.objects.get_or_create(
                    source=Message.Source.GMAIL,
                    external_id=external_id,
                    defaults={
                        "lead": lead,
                        "operator": (
                            message_owner
                            if direction == Message.Direction.OUTBOUND
                            else None
                        ),
                        "direction": direction,
                        "sender": sender,
                        "body": body,
                        "sent_at": sent_at,
                        "thread_external_id": thread_id,
                        "raw": _stripped_raw(raw_msg),
                    },
                )
                if (
                    not was_created
                    and direction == Message.Direction.OUTBOUND
                    and stored.operator_id is None
                    and message_owner is not None
                ):
                    stored.operator = message_owner
                    stored.save(update_fields={"operator"})
                if was_created:
                    created += 1
    return created


# ---------------------------------------------------------------------------
# Read-side helpers
# ---------------------------------------------------------------------------


def merged_timeline(lead, since_days: int | None = None) -> list[Message]:
    """Return all Messages for a lead across sources, sorted by sent_at asc.

    `since_days` clamps to the trailing window so very old threads don't
    bias ball-on-court classification. None = no clamp.
    """
    qs = Message.objects.filter(lead=lead).order_by("sent_at")
    if since_days is not None:
        cutoff = dj_tz.now() - timedelta(days=since_days)
        qs = qs.filter(sent_at__gte=cutoff)
    return list(qs)


def classify_ball_on_court(
    lead, *, nudge_after_days: int = 5, since_days: int | None = None,
) -> tuple[str, Message | None, list[Message]]:
    """Ball-on-court classifier on the union of LinkedIn + Gmail messages.

    Returns one of: 'ball_on_us', 'cold_thread', 'active_in_flight',
    'no_reply_yet', 'no_messages' — same vocabulary as the inline
    classifier in docs/followup-generation-workflow.md Phase 1, but
    operating on the merged timeline so an email reply correctly flips a
    LinkedIn-silent lead from cold_thread to ball_on_us.
    """
    msgs = merged_timeline(lead, since_days=since_days)
    if not msgs:
        return ("no_messages", None, [])
    if not any(m.direction == Message.Direction.INBOUND for m in msgs):
        return ("no_reply_yet", None, msgs)

    latest = msgs[-1]
    if latest.direction == Message.Direction.INBOUND:
        return ("ball_on_us", latest, msgs)

    cutoff = dj_tz.now() - timedelta(days=nudge_after_days)
    if latest.sent_at < cutoff:
        return ("cold_thread", latest, msgs)
    return ("active_in_flight", latest, msgs)


# ---------------------------------------------------------------------------
# Payload-shape adapters — tolerant of MCP / Gmail-API variation.
# ---------------------------------------------------------------------------


def _iter_messages(thread: dict) -> Iterable[dict]:
    """Yield each message dict in a thread regardless of envelope key."""
    for key in ("messages", "relatedMessages", "messageList"):
        msgs = thread.get(key)
        if isinstance(msgs, list):
            yield from msgs
            return


def _extract_from(msg: dict) -> str:
    """Return the lowercase plain email address of the From header."""
    raw = (
        _header_value(msg, "From")
        or msg.get("from")
        or msg.get("sender")
        or ""
    )
    if isinstance(raw, dict):
        # Gmail-API shape: {"name": "...", "address": "..."} or similar
        raw = raw.get("address") or raw.get("email") or raw.get("value") or ""
    _, addr = parseaddr(str(raw))
    return (addr or "").strip().lower()


def _extract_sender_display(msg: dict) -> str:
    raw = _header_value(msg, "From") or msg.get("from") or ""
    if isinstance(raw, dict):
        return raw.get("name") or raw.get("address") or ""
    name, _ = parseaddr(str(raw))
    return name or str(raw)


def _extract_timestamp(msg: dict):
    """Resolve to an aware datetime. Accepts header Date, internalDate
    (Gmail epoch ms), or `sentAt` ISO string. Falls back to now() — the
    classifier needs an ordering, never crash on a bad row."""
    # Gmail API: internalDate is epoch milliseconds as a string
    internal = msg.get("internalDate")
    if internal:
        try:
            return datetime.fromtimestamp(int(internal) / 1000, tz=timezone.utc)
        except (TypeError, ValueError):
            pass

    iso = msg.get("sentAt") or msg.get("date") or msg.get("sent_at")
    if iso:
        try:
            s = str(iso).replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass

    header_date = _header_value(msg, "Date")
    if header_date:
        try:
            dt = parsedate_to_datetime(header_date)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            pass

    logger.debug("gmail_threads: no parseable timestamp on msg %s", msg.get("id"))
    return dj_tz.now()


_BODY_KEYS = ("snippet", "body", "bodyText", "textPlain", "plainText")


def _extract_body(msg: dict) -> str:
    """Best-effort plain-text body. Search MCP-flavored keys first, then
    fall back to Gmail-API payload.parts walk."""
    for k in _BODY_KEYS:
        v = msg.get(k)
        if isinstance(v, str) and v.strip():
            return _clean_body(v)
        if isinstance(v, dict):
            inner = v.get("data") or v.get("text") or v.get("plain") or ""
            if isinstance(inner, str) and inner.strip():
                return _clean_body(inner)

    # Gmail API: payload.parts[].body.data (base64url) — we don't decode
    # base64 here because the MCP usually returns snippet/text already.
    # If we hit this branch, we degrade gracefully to the empty string.
    return ""


_WS_RE = re.compile(r"\s+")


def _clean_body(s: str) -> str:
    """Collapse whitespace and trim. Gmail snippets often have stray
    \\r\\n and HTML residue; this keeps the persisted body searchable."""
    return _WS_RE.sub(" ", s).strip()


def _header_value(msg: dict, name: str) -> str:
    """Look up an RFC2822 header by case-insensitive name. Tolerant of
    both Gmail-API ([{name, value}]) and flat-dict (`headers.From`)
    shapes."""
    headers = msg.get("headers") or msg.get("payload", {}).get("headers")
    if isinstance(headers, list):
        lower = name.lower()
        for h in headers:
            if (h.get("name") or "").lower() == lower:
                return str(h.get("value") or "")
    elif isinstance(headers, dict):
        for k, v in headers.items():
            if k.lower() == name.lower():
                return str(v or "")
    return ""


def _stripped_raw(msg: dict) -> dict:
    """Trim raw payload before persisting — full Gmail responses include
    base64 attachments and MIME parts that bloat the JSONField. Keep just
    the fields we need for forensic replay."""
    keep = {}
    for k in (
        "id", "threadId", "snippet", "internalDate", "labelIds",
        "from", "sender", "sentAt",
    ):
        if k in msg:
            keep[k] = msg[k]
    headers = msg.get("headers") or msg.get("payload", {}).get("headers")
    if headers:
        keep["headers"] = headers
    return keep
