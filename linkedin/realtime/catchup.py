"""Catch up on LinkedIn replies the realtime listener missed.

The daemon stops the realtime listener outside its listener hours and
restarts it after a crash, so replies can land while nothing is listening.
Each time the supervisor starts a new listener process, the daemon reads the
old listener's last heartbeat and, through its own logged-in browser, pages
the Messaging inbox (newest first) back to that moment. Every message in that
window goes through the listener's own path: find the Lead, save the message,
stamp the reply and post one Slack notice. A reply another path saved
silently (for example the connection sweep) still gets its notice, because
notices are claimed per message (`linkedin.realtime.reply_notice`), not per
save, so nothing is ever announced twice.

Bounded: the window is capped at CATCHUP_MAX_HOURS and the run at
CATCHUP_MAX_CONVERSATIONS threads, with short pauses between reads. A capped,
truncated or partly failed run posts one ops alert naming the manual
command, `manage.py catch_up_replies`, which runs this same catch-up for a
chosen window or thread.

Like the listener, this is an enhancement: a failed run is reported and
never unwinds the daemon loop.
"""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone as dt_timezone

from django.utils import timezone
from playwright.sync_api import Error as PlaywrightError

from linkedin.actions.conversations import parse_messages
from linkedin.api.client import PlaywrightLinkedinAPI
from linkedin.api.messaging.conversations import fetch_messages
from linkedin.api.messaging.utils import get_self_urn
from linkedin.management.commands.discover_inbox_leads import iter_inbox_conversations
from linkedin.notifications.slack import notify_degraded, notify_error
from linkedin.realtime.heartbeat import read_heartbeat
from linkedin.realtime.lead_lookup import resolve_lead_for_realtime

logger = logging.getLogger(__name__)

# The window starts this far before the last heartbeat (written every
# LISTENER_PUMP_SLICE_SECONDS) so a reply in the listener's last moments is
# not lost between beats.
CATCHUP_OVERLAP_MINUTES = 2
# A longer outage is checked only this far back, with an ops alert.
CATCHUP_MAX_HOURS = 72
CATCHUP_MAX_CONVERSATIONS = 40
CATCHUP_MAX_PAGES = 5
CATCHUP_PAGE_SIZE = 20
# Random pause between thread reads.
CATCHUP_PAUSE_SECONDS = (2.0, 5.0)
# A listener that keeps crashing must not make the daemon re-read the inbox
# on every respawn; a throttled window is kept and checked once this passes.
CATCHUP_MIN_INTERVAL_SECONDS = 600
# A run that fails outright (say the browser is offline) is retried, after
# the interval, up to this many times in all.
CATCHUP_MAX_ATTEMPTS = 3

MANUAL_COMMAND = "manage.py catch_up_replies"


@dataclass(frozen=True)
class CatchupWindow:
    since: datetime
    capped: bool


@dataclass(frozen=True)
class PendingNotice:
    """A reply a dry run would announce."""
    lead_id: int
    name: str
    sent_at: str
    text: str
    stored: bool


@dataclass
class CatchupResult:
    since: datetime
    conversations: int = 0
    messages_in_window: int = 0
    saved: int = 0
    notified: int = 0
    already_notified: int = 0
    would_notify: int = 0
    failed_notices: int = 0
    unmatched: int = 0
    errors: list[str] = field(default_factory=list)
    truncated: bool = False
    pending: list[PendingNotice] = field(default_factory=list)

    def summary(self) -> str:
        parts = [
            f"{self.conversations} conversations",
            f"{self.messages_in_window} messages in window",
            f"{self.saved} saved",
            f"{self.notified} notices posted",
            f"{self.already_notified} already posted",
        ]
        if self.would_notify:
            parts.append(f"{self.would_notify} would be posted")
        if self.failed_notices:
            parts.append(f"{self.failed_notices} notices failed")
        if self.unmatched:
            parts.append(f"{self.unmatched} with no matching lead")
        if self.errors:
            parts.append(f"{len(self.errors)} threads failed to load")
        if self.truncated:
            parts.append(f"stopped at {CATCHUP_MAX_CONVERSATIONS} conversations")
        return ", ".join(parts)


def catchup_window(username: str, *, now: datetime | None = None) -> CatchupWindow | None:
    """The window the last listener could have missed, or None if unknown."""
    last = read_heartbeat(username)
    if last is None:
        return None
    now = now or timezone.now()
    since = last - timedelta(minutes=CATCHUP_OVERLAP_MINUTES)
    floor = now - timedelta(hours=CATCHUP_MAX_HOURS)
    if since < floor:
        return CatchupWindow(since=floor, capped=True)
    return CatchupWindow(since=since, capped=False)


def _sent_at(message: dict) -> datetime | None:
    """`parse_messages` timestamps are minute-precision UTC strings."""
    try:
        naive = datetime.strptime(message.get("timestamp") or "", "%Y-%m-%d %H:%M")
    except ValueError:
        return None
    return naive.replace(tzinfo=dt_timezone.utc)


def _pause(pause_seconds: tuple[float, float]) -> None:
    low, high = pause_seconds
    if high > 0:
        time.sleep(random.uniform(low, high))


def _lead_name(lead) -> str:
    return f"{lead.first_name or ''} {lead.last_name or ''}".strip() or lead.public_identifier or ""


def _settle_notices(result: CatchupResult, *, lead, parsed: list[dict], operator: str,
                    apply: bool) -> None:
    """Stamp and announce each inbound message in the window exactly once."""
    from crm.models import Message
    from linkedin.db.deals import stamp_inbound_linkedin_reply
    from linkedin.db.messages import _looks_like_lead_sender
    from linkedin.realtime import reply_notice
    from linkedin.realtime.handler import _maybe_enqueue_enrichment

    for item in parsed:
        msg = (
            Message.objects.select_related("lead")
            .filter(source=Message.Source.LINKEDIN, external_id=item["entity_urn"])
            .first()
        )
        if msg is None:
            # Only a dry run leaves a window message unsaved; estimate its
            # direction the way persist_thread would.
            if _looks_like_lead_sender((item.get("sender") or "").strip().lower(), lead):
                result.would_notify += 1
                result.pending.append(PendingNotice(
                    lead_id=lead.pk, name=_lead_name(lead), sent_at=item.get("timestamp", ""),
                    text=item.get("text", ""), stored=False,
                ))
            continue
        if msg.direction != Message.Direction.INBOUND:
            continue
        if not apply:
            if reply_notice.reply_notice_sent(msg):
                result.already_notified += 1
            else:
                result.would_notify += 1
                result.pending.append(PendingNotice(
                    lead_id=msg.lead_id, name=_lead_name(msg.lead),
                    sent_at=msg.sent_at.strftime("%Y-%m-%d %H:%M"), text=msg.body, stored=True,
                ))
            continue

        stamp_inbound_linkedin_reply(lead=msg.lead, sent_at=msg.sent_at)
        outcome = reply_notice.deliver_reply_notice(msg, lead=msg.lead, operator=operator)
        if outcome in (reply_notice.POSTED, reply_notice.OUTBOX):
            result.notified += 1
            _maybe_enqueue_enrichment(msg.lead)
        elif outcome == reply_notice.ALREADY_SENT:
            result.already_notified += 1
        else:
            result.failed_notices += 1


def catch_up_missed_replies(
    session,
    *,
    operator: str,
    since: datetime,
    conversation_urns: list[str] | None = None,
    apply: bool = True,
    max_conversations: int = CATCHUP_MAX_CONVERSATIONS,
    pause_seconds: tuple[float, float] = CATCHUP_PAUSE_SECONDS,
    self_urn: str = "",
) -> CatchupResult:
    """Save and announce every LinkedIn message since `since` the listener missed.

    `session` is any object with the logged-in `.page`/`.context`
    (`PlaywrightLinkedinAPI`'s contract). Without `conversation_urns` it pages
    the inbox back to `since`; with them it reads only those threads.
    `apply=False` reads LinkedIn but writes nothing and posts nothing.
    """
    from linkedin.db.messages import persist_thread

    api = PlaywrightLinkedinAPI(session=session)
    self_urn = self_urn or get_self_urn(api)
    window_start = since.replace(second=0, microsecond=0)
    result = CatchupResult(since=since)

    if conversation_urns is not None:
        conversations = ((urn, "") for urn in conversation_urns)
    else:
        conversations = (
            (conv.conversation_urn, conv.participant.member_urn)
            for conv in iter_inbox_conversations(
                api,
                self_urn=self_urn,
                self_name="",
                cutoff=since,
                max_pages=CATCHUP_MAX_PAGES,
                page_size=CATCHUP_PAGE_SIZE,
                page_delay_seconds=pause_seconds[0],
                on_unidentifiable=lambda urn: result.errors.append(
                    f"{urn}: participant identity unavailable"
                ),
            )
        )

    for index, (conversation_urn, member_urn) in enumerate(conversations):
        if index >= max_conversations:
            result.truncated = True
            logger.warning("Missed-reply catch-up stopped at %d conversations", max_conversations)
            break
        if index:
            _pause(pause_seconds)
        result.conversations += 1

        try:
            raw = fetch_messages(api, conversation_urn)
        except (IOError, PlaywrightError) as exc:
            result.errors.append(f"{conversation_urn}: {exc.__class__.__name__}")
            logger.warning("Missed-reply catch-up could not read %s: %s",
                           conversation_urn, exc.__class__.__name__)
            continue

        in_window = [m for m in parse_messages(raw)
                     if (sent := _sent_at(m)) is not None and sent >= window_start]
        if not in_window:
            continue
        result.messages_in_window += len(in_window)

        lead = resolve_lead_for_realtime(
            conversation_urn=conversation_urn, sender_member_urn=member_urn,
        )
        if lead is None:
            result.unmatched += 1
            logger.warning("Missed-reply catch-up: no matching Lead for %s — skipped",
                           conversation_urn)
            continue

        if apply:
            result.saved += persist_thread(
                lead=lead,
                parsed=in_window,
                thread_external_id=conversation_urn,
                outbound_senders={operator} if operator else set(),
            )
        _settle_notices(result, lead=lead, parsed=in_window, operator=operator, apply=apply)

    return result


class ListenerCatchup:
    """Daemon-side scheduler: one catch-up per new listener process.

    The supervisor calls `window()` just before it spawns a listener (so the
    old heartbeat is read before the new child can refresh it), `request()`
    with that window after a successful spawn, and `run_pending()` on every
    sync. Runs on the daemon's own thread, which owns its Playwright session.
    """

    def __init__(self, *, session, username: str, operator: str, clock=time.monotonic):
        self.session = session
        self.username = username
        self.operator = operator
        self._clock = clock
        self._pending: CatchupWindow | None = None
        self._last_run_at: float | None = None
        self._failures = 0

    def window(self) -> CatchupWindow | None:
        return catchup_window(self.username)

    def request(self, window: CatchupWindow | None) -> None:
        if window is None:
            logger.warning(
                "Realtime listener has no heartbeat yet — no missed-reply window to check"
            )
            return
        if self._pending is None or window.since < self._pending.since:
            self._pending = CatchupWindow(
                since=window.since,
                capped=window.capped or bool(self._pending and self._pending.capped),
            )

    def run_pending(self) -> None:
        if self._pending is None:
            return
        now = self._clock()
        if self._last_run_at is not None and now - self._last_run_at < CATCHUP_MIN_INTERVAL_SECONDS:
            return
        window, self._pending = self._pending, None
        self._last_run_at = now
        try:
            result = catch_up_missed_replies(self.session, operator=self.operator, since=window.since)
        except Exception as exc:
            # For example the browser is offline right as the listener
            # restarts: keep the window for a later sync, a few times.
            self._failures += 1
            logger.exception("Missed-reply catch-up failed (%d/%d)",
                             self._failures, CATCHUP_MAX_ATTEMPTS)
            notify_error("daemon:listener_catchup", exc, context={
                "operator": self.operator, "since": window.since.isoformat(),
            })
            if self._failures < CATCHUP_MAX_ATTEMPTS:
                self.request(window)
            else:
                self._failures = 0
            return
        self._failures = 0
        logger.info("Missed-reply catch-up since %s: %s",
                    window.since.isoformat(timespec="minutes"), result.summary())
        if window.capped or result.truncated or result.errors:
            self._alert_incomplete(window, result)

    def _alert_incomplete(self, window: CatchupWindow, result: CatchupResult) -> None:
        reasons = []
        if window.capped:
            reasons.append(f"the listener was off longer than {CATCHUP_MAX_HOURS}h, "
                           "so older replies were not checked")
        if result.truncated:
            reasons.append(f"it stopped after {CATCHUP_MAX_CONVERSATIONS} conversations")
        if result.errors:
            reasons.append(f"{len(result.errors)} threads could not be fully checked")
        notify_degraded(
            sender=self.operator,
            title="Missed-reply catch-up was incomplete",
            detail=(
                f"Checked LinkedIn replies since {window.since:%Y-%m-%d %H:%M} UTC, but "
                + "; ".join(reasons)
                + f". Result: {result.summary()}. To check by hand on this machine, run "
                f"`{MANUAL_COMMAND} --since <time>` to preview, then add `--apply`."
            ),
        )
