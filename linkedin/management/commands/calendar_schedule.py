from __future__ import annotations

import json
from hashlib import sha256

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import validate_email
from google.auth.exceptions import GoogleAuthError
from googleapiclient.errors import HttpError
from httplib2 import HttpLib2Error

from gmail.auth import GMAIL_ACCOUNTS
from linkedin.management.calendar_api import calendar_events, parse_calendar_time


class Command(BaseCommand):
    help = "Preview or schedule a meeting on an authorized account's primary Google Calendar."

    def add_arguments(self, parser):
        parser.add_argument("--account", required=True, choices=sorted(GMAIL_ACCOUNTS))
        parser.add_argument("--title", required=True)
        parser.add_argument("--start", required=True, help="ISO 8601 date and time with a UTC offset.")
        parser.add_argument("--end", required=True, help="ISO 8601 date and time with a UTC offset.")
        parser.add_argument("--attendee", action="append", default=[], help="Guest email. Repeat for each guest.")
        parser.add_argument("--description", default="")
        parser.add_argument("--location", default="")
        parser.add_argument("--apply", action="store_true", help="Create the event and invite the listed guests.")

    def handle(self, *args, **options):
        account = options["account"]
        title = options["title"].strip()
        if not title:
            raise CommandError("The meeting title is required.")
        start = parse_calendar_time(options["start"])
        end = parse_calendar_time(options["end"])
        if end <= start:
            raise CommandError("The end time must be after the start time.")

        attendees = sorted({email.strip().lower() for email in options["attendee"]})
        for email in attendees:
            try:
                validate_email(email)
            except ValidationError:
                raise CommandError(f"Invalid attendee email: {email}") from None

        event = {
            "summary": title,
            "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": end.isoformat()},
            "attendees": [{"email": email} for email in attendees],
        }
        for field in ("description", "location"):
            if options[field]:
                event[field] = options[field]

        if not options["apply"]:
            self.stdout.write(json.dumps({"account": account, "event": event}, indent=2))
            self.stdout.write("Preview only. Add --apply to schedule the meeting.")
            return

        # A retry after a lost response must target the same Calendar event.
        event_id = sha256(json.dumps([account, event], sort_keys=True).encode()).hexdigest()
        event["id"] = event_id
        try:
            events = calendar_events(account)
            try:
                result = events.insert(calendarId="primary", body=event, sendUpdates="all").execute(
                    num_retries=0
                )
            except HttpError as exc:
                if exc.resp.status != 409:
                    raise
                result = events.get(calendarId="primary", eventId=event_id).execute()
                if result.get("status") == "cancelled":
                    raise CommandError("The matching calendar event was cancelled.")
                self.stdout.write("This meeting was already scheduled.")
            else:
                self.stdout.write(self.style.SUCCESS(f"Scheduled on the {account} calendar."))
        except HttpError as exc:
            raise CommandError(f"Google Calendar returned HTTP {exc.resp.status}. Event ID: {event_id}.") from None
        except GoogleAuthError:
            raise CommandError(f"Calendar authorization failed. Run calendar_oauth --account {account}.") from None
        except (OSError, HttpLib2Error):
            raise CommandError(f"Calendar request interrupted. Retry with the same details. Event ID: {event_id}.") from None

        self.stdout.write(result.get("htmlLink") or result["id"])
