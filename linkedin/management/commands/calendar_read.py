from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError
from google.auth.exceptions import GoogleAuthError
from googleapiclient.errors import HttpError
from httplib2 import HttpLib2Error

from gmail.auth import GMAIL_ACCOUNTS
from linkedin.management.calendar_api import calendar_events, parse_calendar_time


class Command(BaseCommand):
    help = "List or search an authorized account's primary Google Calendar in a time window."

    def add_arguments(self, parser):
        parser.add_argument("--account", required=True, choices=sorted(GMAIL_ACCOUNTS))
        parser.add_argument("--start", required=True, help="ISO 8601 date and time with a UTC offset.")
        parser.add_argument("--end", required=True, help="ISO 8601 date and time with a UTC offset.")
        parser.add_argument("--query", help="Search event text and attendees.")
        parser.add_argument("--limit", type=int, default=50, choices=range(1, 251), metavar="1–250")

    def handle(self, *args, **options):
        account = options["account"]
        start = parse_calendar_time(options["start"])
        end = parse_calendar_time(options["end"])
        if end <= start:
            raise CommandError("The end time must be after the start time.")

        params = {
            "calendarId": "primary",
            "timeMin": start.isoformat(),
            "timeMax": end.isoformat(),
            "singleEvents": True,
            "orderBy": "startTime",
            "showDeleted": False,
            "fields": (
                "timeZone,nextPageToken,items(id,status,summary,start,end,organizer,"
                "attendees,description,location,hangoutLink,htmlLink,transparency)"
            ),
        }
        if options["query"]:
            params["q"] = options["query"]

        limit = options["limit"]
        items = []
        next_page = None
        try:
            events = calendar_events(account)
            while len(items) < limit:
                page = events.list(maxResults=limit - len(items), pageToken=next_page, **params).execute()
                items.extend(page.get("items", []))
                next_page = page.get("nextPageToken")
                if not next_page:
                    break
        except HttpError as exc:
            raise CommandError(f"Google Calendar returned HTTP {exc.resp.status}.") from None
        except GoogleAuthError:
            raise CommandError(f"Calendar authorization failed. Run calendar_oauth --account {account}.") from None
        except (OSError, HttpLib2Error):
            raise CommandError("Calendar read was interrupted. Retry the read.") from None

        self.stdout.write(json.dumps({
            "account": account,
            "time_zone": page.get("timeZone"),
            "has_more": bool(next_page),
            "events": items,
        }, indent=2))
