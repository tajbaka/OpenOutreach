from datetime import datetime, timezone

from django.core.management.base import CommandError
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from gmail.auth import CALENDAR_EVENTS_SCOPE, calendar_token_path


def parse_calendar_time(value: str) -> datetime:
    try:
        time = datetime.fromisoformat(value)
        if time.utcoffset() is None:
            raise ValueError
        return time.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise CommandError("Use an ISO 8601 date and time with a UTC offset.") from None


def calendar_events(account: str):
    token = calendar_token_path(account)
    if not token.is_file():
        raise CommandError(f"Calendar token missing. Run calendar_oauth --account {account}.")
    try:
        credentials = Credentials.from_authorized_user_file(str(token))
    except (OSError, ValueError):
        raise CommandError(
            f"Calendar token unreadable. Run calendar_oauth --account {account}."
        ) from None
    if not credentials.has_scopes([CALENDAR_EVENTS_SCOPE]):
        raise CommandError(f"Calendar scope missing. Run calendar_oauth --account {account}.")
    return build("calendar", "v3", credentials=credentials, cache_discovery=False).events()
