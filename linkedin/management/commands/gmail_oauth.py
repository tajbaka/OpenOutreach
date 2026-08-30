from __future__ import annotations

import json
import shutil
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from gmail.auth import (
    DEFAULT_CLIENT_SECRET_PATH,
    GMAIL_DATA_DIR,
    SCOPES,
    account_for_key,
    token_path,
)


class Command(BaseCommand):
    help = "Authorize Gmail and verify configured From/Reply-To identities."

    def add_arguments(self, parser):
        parser.add_argument(
            "--account",
            required=True,
            help="Gmail account key, e.g. arian_boundera or eddy_boundera.",
        )
        parser.add_argument(
            "--client-secret",
            default=str(DEFAULT_CLIENT_SECRET_PATH),
            help="OAuth desktop client JSON path.",
        )
        parser.add_argument(
            "--copy-client-secret",
            action="store_true",
            help="Copy --client-secret into data/gmail/oauth-client.json before auth.",
        )
        parser.add_argument(
            "--port",
            type=int,
            default=0,
            help="Local OAuth callback port. 0 lets the library choose.",
        )

    def handle(self, *args, **options):
        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from google_auth_oauthlib.flow import InstalledAppFlow
            from googleapiclient.discovery import build
        except ImportError as exc:
            raise CommandError(
                "Missing Gmail OAuth dependencies. Run "
                "`.venv/bin/pip install -r requirements/local.txt` first."
            ) from exc

        account_key = options["account"]
        account = account_for_key(account_key)
        client_secret = Path(options["client_secret"]).expanduser()
        if not client_secret.exists():
            raise CommandError(f"OAuth client JSON not found: {client_secret}")

        GMAIL_DATA_DIR.mkdir(parents=True, exist_ok=True)
        if options["copy_client_secret"]:
            shutil.copyfile(client_secret, DEFAULT_CLIENT_SECRET_PATH)
            client_secret = DEFAULT_CLIENT_SECRET_PATH
            self.stdout.write(f"Copied OAuth client JSON to {client_secret}")

        token_file = token_path(account.key)
        creds = None
        if token_file.exists():
            creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())

        if not creds or not creds.valid:
            flow = InstalledAppFlow.from_client_secrets_file(str(client_secret), SCOPES)
            creds = flow.run_local_server(port=options["port"])

        token_file.write_text(creds.to_json())
        self.stdout.write(f"Saved Gmail token: {token_file}")

        service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        response = service.users().settings().sendAs().list(userId="me").execute()
        aliases = response.get("sendAs", [])
        available = {
            (a.get("sendAsEmail") or "").strip().lower(): a
            for a in aliases
            if a.get("sendAsEmail")
        }

        self.stdout.write("Available send-as aliases:")
        for email in sorted(available):
            meta = available[email]
            status = meta.get("verificationStatus") or "unknown"
            default = " default" if meta.get("isDefault") else ""
            self.stdout.write(f"  - {email} ({status}{default})")

        missing = [
            alias for alias in account.delivery_aliases
            if alias.lower() not in available
        ]
        if missing:
            raise CommandError(
                "Missing configured Gmail delivery identities for "
                f"{account.key}: {', '.join(missing)}"
            )

        unverified = [
            alias for alias in account.delivery_aliases
            if not available[alias.lower()].get("isDefault")
            and (available[alias.lower()].get("verificationStatus") or "").lower()
            not in {"accepted", "verified"}
        ]
        if unverified:
            raise CommandError(
                "Configured Gmail delivery identities are present but not "
                "verified/accepted: "
                f"{', '.join(unverified)}"
            )

        manifest = {
            "account": account.key,
            "token_path": str(token_file),
            "send_as_aliases": account.send_as_aliases,
            "reply_to_aliases": account.reply_to_aliases,
        }
        self.stdout.write(json.dumps(manifest, indent=2))
        self.stdout.write(self.style.SUCCESS(f"Gmail OAuth ready for {account.key}"))
