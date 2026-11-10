"""`manage.py catch_up_replies` — find LinkedIn replies Slack never saw.

Runs the realtime listener's missed-reply catch-up by hand
(`linkedin.realtime.catchup`). Run it on the machine where this sender's
daemon is running: it attaches to the daemon's browser over CDP (like the
listener and feed collector), opens one tab of its own, reads LinkedIn
through the daemon's logged-in session, and closes only that tab. It never
logs in, sends on LinkedIn, or touches the daemon's tab.

Preview by default: reads LinkedIn, writes nothing and posts nothing. With
--apply it saves missed messages, stamps the replies, and posts one Slack
notice for each reply that has not had one (never a second).

    manage.py catch_up_replies --hours 12
    manage.py catch_up_replies --since 2026-09-10T00:00 --lead 10547
    manage.py catch_up_replies --since 2026-09-10T00:00 --lead 10547 --apply

Without --lead/--thread it pages the inbox back to the start of the window.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone
from types import SimpleNamespace

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone


def parse_since(*, since: str | None, hours: float | None, now: datetime | None = None) -> datetime:
    """The window start: an ISO time (UTC unless it says otherwise) or N hours ago."""
    if bool(since) == (hours is not None):
        raise CommandError("Pass exactly one of --since or --hours.")
    if hours is not None:
        if hours <= 0:
            raise CommandError("--hours must be positive.")
        return (now or timezone.now()) - timedelta(hours=hours)
    try:
        parsed = datetime.fromisoformat(since)
    except ValueError as exc:
        raise CommandError(f"--since must be an ISO time, e.g. 2026-09-10T00:00: {exc}")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_timezone.utc)
    return parsed


def lead_threads(lead_ids: list[int], *, self_urn: str) -> list[str]:
    """This mailbox's stored LinkedIn thread URNs for the given Leads."""
    from crm.models import Message

    return sorted(set(
        Message.objects.filter(
            lead_id__in=lead_ids,
            source=Message.Source.LINKEDIN,
            thread_external_id__contains=self_urn,
        ).exclude(thread_external_id="").values_list("thread_external_id", flat=True)
    ))


class Command(BaseCommand):
    help = "Find and announce LinkedIn replies the realtime listener missed (preview unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument("--since", help="Window start, ISO time (UTC unless an offset is given).")
        parser.add_argument("--hours", type=float, help="Window start, this many hours ago.")
        parser.add_argument("--lead", type=int, action="append", default=[],
                            help="Only this Lead's threads (repeatable).")
        parser.add_argument("--thread", action="append", default=[],
                            help="Only this conversation URN (repeatable).")
        parser.add_argument("--apply", action="store_true",
                            help="Save missed messages and post their Slack notices.")
        parser.add_argument("--cdp-port", type=int, default=None,
                            help="The daemon browser's debugging port (default LISTENER_CDP_PORT).")
        parser.add_argument("--max-conversations", type=int, default=None)

    def handle(self, *args, **opts):
        from playwright.sync_api import sync_playwright

        from linkedin.api.client import PlaywrightLinkedinAPI
        from linkedin.api.messaging.utils import get_self_urn
        from linkedin.conf import LISTENER_CDP_PORT, get_daemon_handle
        from linkedin.feed_collection import _feed_page
        from linkedin.models import LinkedInProfile
        from linkedin.operators import resolve_operator
        from linkedin.realtime.catchup import CATCHUP_MAX_CONVERSATIONS, catch_up_missed_replies

        since = parse_since(since=opts["since"], hours=opts["hours"])
        handle = get_daemon_handle()
        if not handle:
            raise CommandError("No daemon LinkedIn account configured — set LINKEDIN_USERNAME in .env.")
        profile = LinkedInProfile.objects.filter(user__username=handle).first()
        if profile is None:
            raise CommandError(f"No LinkedInProfile for handle {handle!r}.")
        operator = resolve_operator(profile.linkedin_username)
        port = opts["cdp_port"] or LISTENER_CDP_PORT
        mode = "APPLY" if opts["apply"] else "PREVIEW (nothing written or posted)"
        self.stdout.write(f"{mode}: replies to {operator} since {since.isoformat(timespec='minutes')}")

        with sync_playwright() as pw:
            try:
                browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=15_000)
            except Exception as exc:
                raise CommandError(
                    f"Could not attach to the daemon browser on port {port} ({exc.__class__.__name__}). "
                    "Run this on the machine where this sender's daemon is running."
                )
            if not browser.contexts:
                raise CommandError("The daemon browser has no shared context.")
            with _feed_page(browser) as page:
                session = SimpleNamespace(page=page, context=browser.contexts[0])
                self_urn = get_self_urn(PlaywrightLinkedinAPI(session=session))
                threads = list(opts["thread"])
                if opts["lead"]:
                    found = lead_threads(opts["lead"], self_urn=self_urn)
                    if not found:
                        raise CommandError(f"No stored LinkedIn thread in this mailbox for lead(s) {opts['lead']}.")
                    threads.extend(found)
                result = catch_up_missed_replies(
                    session,
                    operator=operator,
                    since=since,
                    conversation_urns=threads or None,
                    apply=opts["apply"],
                    max_conversations=opts["max_conversations"] or CATCHUP_MAX_CONVERSATIONS,
                    self_urn=self_urn,
                )

        self.stdout.write(result.summary())
        for pending in result.pending:
            stored = "saved earlier, never announced" if pending.stored else "not saved yet"
            self.stdout.write(
                f"  would post: lead {pending.lead_id} {pending.name} at {pending.sent_at} UTC "
                f"({stored}): {pending.text[:120]!r}"
            )
        for error in result.errors:
            self.stdout.write(f"  could not read {error}")
