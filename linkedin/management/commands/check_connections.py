"""Standalone connection-check: detects accepted invites + posts to Slack.

Visits LinkedIn's connections page using the saved outreach session,
scrapes recent connections, cross-references against PENDING deals, and
for each match: updates state to CONNECTED and (if SLACK_WEBHOOK_URL is
set) posts a notification.

Decoupled from the daemon. Run on demand or via cron — does NOT require
the daemon to be running, and won't fire any follow-up DMs even if
ENABLE_FOLLOW_UP=true.

Usage:
    python manage.py check_connections

WARNING: don't run while the daemon is actively performing browser
actions on the same LinkedIn account — two concurrent Playwright
sessions on one account can be flagged. The two CAN technically run at
once (different processes, same cookie file), but pause the daemon for
the cleanest experience.
"""
from __future__ import annotations

import sys

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Detect accepted LinkedIn invites and notify Slack."

    def handle(self, *args, **options):
        from datetime import datetime

        from django.utils import timezone

        from linkedin.actions.connections import scrape_connections
        from linkedin.actions.conversations import get_conversation
        from linkedin.browser.registry import get_or_create_session
        from linkedin.conf import SLACK_WEBHOOK_URL, get_first_active_profile_handle
        from linkedin.db.deals import set_profile_state
        from linkedin.db.urls import url_to_public_id
        from linkedin.enums import ProfileState
        from linkedin.notifications.slack import (
            latest_reply_from_lead,
            notify_connection_accepted,
        )

        from crm.models import Deal

        handle = get_first_active_profile_handle()
        if not handle:
            self.stderr.write("No active LinkedInProfile found.")
            sys.exit(1)

        session = get_or_create_session(handle=handle)
        # Set a default campaign so set_profile_state's campaign-scoped query
        # has a starting value; the loop below resets it per match.
        session.campaign = session.campaigns.first()
        if session.campaign is None:
            self.stderr.write("No campaigns found for this profile.")
            sys.exit(1)
        session.ensure_browser()

        # All PENDING Deals across this account's campaigns — one query.
        pending = list(
            Deal.objects.filter(
                state=ProfileState.PENDING,
                campaign__in=session.campaigns,
            )
            .select_related("lead", "campaign")
        )
        if not pending:
            self.stdout.write("No PENDING deals — nothing to check.")
            return

        oldest = min(d.update_date for d in pending)
        stop_before = oldest.date()

        self.stdout.write(
            f"Checking {len(pending)} PENDING deals against LinkedIn connections "
            f"(stop_before={stop_before})"
        )

        entries = scrape_connections(session, stop_before=stop_before)
        accepted = {e.public_id: e for e in entries}

        if not SLACK_WEBHOOK_URL:
            self.stdout.write(
                "(SLACK_WEBHOOK_URL is not set — state will update but no Slack post)"
            )

        matched = 0
        for deal in pending:
            public_id = (
                url_to_public_id(deal.lead.linkedin_url)
                if deal.lead.linkedin_url else None
            )
            if not public_id or public_id not in accepted:
                continue

            lead = deal.lead
            full_name = (
                f"{lead.first_name or ''} {lead.last_name or ''}".strip()
                or public_id
            )

            session.campaign = deal.campaign
            set_profile_state(session, public_id, ProfileState.CONNECTED.value)

            # One extra LinkedIn API call per match to pull conversation
            # history; cheap because matches are rare and this is async-cron'd.
            try:
                messages = get_conversation(session, public_id)
            except Exception as e:
                self.stderr.write(f"  ! could not fetch conversation for {full_name}: {e}")
                messages = None
            reply = latest_reply_from_lead(messages, full_name)
            reply_text = reply.get("text") if reply else None

            # Persist the last-reply timestamp on the Deal for urgency math.
            # `parse_messages` formats timestamps as "YYYY-MM-DD HH:MM" already
            # in local time — parse back into an aware datetime.
            if reply:
                ts_str = (reply.get("timestamp") or "").strip()
                if ts_str:
                    try:
                        naive = datetime.strptime(ts_str, "%Y-%m-%d %H:%M")
                        deal.last_reply_at = timezone.make_aware(
                            naive, timezone.get_current_timezone()
                        )
                        deal.save(update_fields=["last_reply_at"])
                    except ValueError:
                        # Unparseable timestamp shouldn't block the rest of the flow.
                        pass

            notify_connection_accepted(
                full_name=full_name,
                title="",  # Lead doesn't carry a title field; positions live elsewhere
                company=lead.company_name or "",
                profile_url=lead.linkedin_url
                or f"https://www.linkedin.com/in/{public_id}/",
                campaign_name=deal.campaign.name,
                reply_text=reply_text,
            )
            badge = "✓ replied" if reply_text else "✓ accepted"
            self.stdout.write(f"  {badge} {full_name} ({lead.company_name}) → CONNECTED")
            matched += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Done — {matched} new connections detected of {len(pending)} pending"
            )
        )
