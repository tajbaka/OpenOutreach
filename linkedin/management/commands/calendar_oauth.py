from __future__ import annotations

from pathlib import Path
from tempfile import NamedTemporaryFile

from django.core.management.base import BaseCommand, CommandError
from google.auth.transport.requests import Request
from google.oauth2 import id_token
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from gmail.auth import (
    CALENDAR_EVENTS_SCOPE,
    DEFAULT_CLIENT_SECRET_PATH,
    GMAIL_ACCOUNTS,
    SCOPES,
    calendar_token_path,
    token_path,
)


class Command(BaseCommand):
    help = "Authorize Calendar event access for an existing OpenOutreach mailbox."

    def add_arguments(self, parser):
        parser.add_argument("--account", required=True, choices=sorted(GMAIL_ACCOUNTS))
        parser.add_argument("--port", type=int, default=0, help="Local OAuth callback port.")

    def handle(self, *args, **options):
        account = options["account"]
        request = Request()
        try:
            gmail_creds = Credentials.from_authorized_user_file(str(token_path(account)), SCOPES)
            if not gmail_creds.valid:
                gmail_creds.refresh(request)
            gmail = build("gmail", "v1", credentials=gmail_creds, cache_discovery=False)
            profile = gmail.users().getProfile(userId="me", fields="emailAddress").execute()
            expected_email = profile["emailAddress"]
        except Exception as exc:
            raise CommandError(
                f"Could not verify the existing Gmail mailbox ({type(exc).__name__})."
            ) from None

        scopes = (CALENDAR_EVENTS_SCOPE, "openid", "https://www.googleapis.com/auth/userinfo.email")
        try:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(DEFAULT_CLIENT_SECRET_PATH), scopes, autogenerate_code_verifier=True
            )
            creds = flow.run_local_server(
                host="127.0.0.1",
                port=options["port"],
                open_browser=False,
                timeout_seconds=240,
                login_hint=expected_email,
                prompt="consent",
            )
            identity = id_token.verify_oauth2_token(creds.id_token, request, creds.client_id)
        except Exception as exc:
            raise CommandError(f"Calendar authorization did not complete ({type(exc).__name__}).") from None

        authorized_email = identity.get("email", "")
        if not identity.get("email_verified") or authorized_email.casefold() != expected_email.casefold():
            raise CommandError(f"Authorize Calendar with the existing mailbox account, {expected_email}.")
        if not creds.refresh_token or CALENDAR_EVENTS_SCOPE not in (creds.granted_scopes or ()):
            raise CommandError("Google did not grant persistent Calendar event access.")

        try:
            calendar = build("calendar", "v3", credentials=creds, cache_discovery=False)
            calendar.events().list(calendarId="primary", maxResults=1, fields="kind").execute()
        except Exception as exc:
            raise CommandError(f"Calendar access could not be verified ({type(exc).__name__}).") from None

        destination = calendar_token_path(account)
        handle = NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination.parent, delete=False)
        temporary = Path(handle.name)
        try:
            with handle:
                handle.write(creds.to_json())
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
        self.stdout.write(self.style.SUCCESS(f"Verified Calendar access for {account}. Saved {destination}."))
