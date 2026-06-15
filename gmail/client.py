"""Small Gmail API wrapper for the browserless Gmail sequence lane."""
from __future__ import annotations

import base64
from email.message import EmailMessage

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from linkedin.exceptions import EnrichmentError
from gmail.auth import (
    GMAIL_OPERATOR_MAPPING,
    SCOPES,
    token_path,
)


class GmailClient:
    def __init__(self, *, operator: str):
        mapping = GMAIL_OPERATOR_MAPPING.get(operator)
        if mapping is None:
            raise EnrichmentError(f"No Gmail mapping configured for operator {operator!r}")
        self.operator = operator
        self.account_key = mapping["gmail_account"]
        self.send_as = mapping["send_as"].lower()
        path = token_path(self.account_key)
        if not path.exists():
            raise EnrichmentError(
                f"Gmail token missing for {self.account_key}: run gmail_oauth"
            )
        self._creds = Credentials.from_authorized_user_file(str(path), SCOPES)
        if self._creds.expired and self._creds.refresh_token:
            self._creds.refresh(Request())
            path.write_text(self._creds.to_json())
        if not self._creds.valid:
            raise EnrichmentError(f"Gmail token invalid for {self.account_key}")
        self._service = build("gmail", "v1", credentials=self._creds, cache_discovery=False)

    def send_as_aliases(self) -> dict[str, dict]:
        resp = self._service.users().settings().sendAs().list(userId="me").execute()
        return {
            (item.get("sendAsEmail") or "").strip().lower(): item
            for item in resp.get("sendAs", [])
            if item.get("sendAsEmail")
        }

    def validate_send_as(self) -> None:
        aliases = self.send_as_aliases()
        meta = aliases.get(self.send_as)
        if not meta:
            raise EnrichmentError(
                f"{self.send_as} is not configured as a send-as alias for {self.account_key}"
            )
        status = (meta.get("verificationStatus") or "").lower()
        if not meta.get("isDefault") and status not in {"accepted", "verified"}:
            raise EnrichmentError(
                f"{self.send_as} is present but not verified for {self.account_key}"
            )

    def send_message(self, *, to: str, subject: str, body: str) -> str:
        self.validate_send_as()
        msg = EmailMessage()
        msg["To"] = to
        msg["From"] = self.send_as
        msg["Subject"] = subject
        msg.set_content(body)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
        try:
            sent = self._service.users().messages().send(
                userId="me",
                body={"raw": raw},
            ).execute()
        except HttpError as exc:
            raise EnrichmentError(f"Gmail send failed: {exc}") from exc
        msg_id = sent.get("id")
        if not msg_id:
            raise EnrichmentError(f"Gmail send returned no id: {sent}")
        return str(msg_id)

    def search_threads_for_email(self, email: str, *, newer_than_days: int = 90) -> list[dict]:
        q = f"{email} newer_than:{int(newer_than_days)}d"
        found = self._service.users().messages().list(userId="me", q=q).execute()
        messages = found.get("messages", [])
        thread_ids = sorted({m["threadId"] for m in messages if m.get("threadId")})
        threads: list[dict] = []
        for thread_id in thread_ids:
            threads.append(
                self._service.users().threads().get(
                    userId="me",
                    id=thread_id,
                    format="full",
                ).execute()
            )
        return threads
