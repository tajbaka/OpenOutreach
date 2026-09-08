"""Small Gmail API wrapper for the browserless Gmail sequence lane."""
from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import make_msgid
from typing import Callable, Iterable

from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from gmail.auth import (
    GMAIL_OPERATOR_MAPPING,
    SCOPES,
    token_path,
)
from gmail.delivery import GmailDeliveryPermit, consume_gmail_delivery_permit
from linkedin.exceptions import EnrichmentError


@dataclass(frozen=True)
class GmailSendResult:
    """Identifiers returned by one successful Gmail submission.

    Gmail message and thread IDs are mailbox-local provider identifiers.  The
    caller is responsible for pairing them with ``GmailClient.account_key``
    before persisting them to the cross-mailbox CRM store.
    """

    message_id: str
    thread_id: str
    rfc_message_id: str


def scoped_gmail_id(account_key: str, provider_id: str) -> str:
    """Namespace one mailbox-local Gmail identifier for ``crm.Message``."""
    account = (account_key or "").strip()
    raw_id = (provider_id or "").strip()
    if not account or not raw_id:
        raise ValueError("Gmail account key and provider ID must be non-empty")
    scoped = f"{account}:{raw_id}"
    if len(scoped) > 200:
        raise ValueError("Mailbox-scoped Gmail identifier exceeds crm.Message limit")
    return scoped


def _normalized_rfc_message_id(value: str, *, domain: str) -> str:
    message_id = (value or "").strip() or make_msgid(domain=domain)
    if "\r" in message_id or "\n" in message_id:
        raise ValueError("RFC Message-ID cannot contain a newline")
    if not (message_id.startswith("<") and message_id.endswith(">")):
        message_id = f"<{message_id.strip('<>')}>"
    return message_id


_PROVIDER_RFC_MESSAGE_ID = re.compile(r"\A<[^<>\s@]+@[^<>\s@]+>\Z")


def validated_provider_rfc_message_id(value: str) -> str:
    """Validate a provider-returned RFC Message-ID without repairing it."""
    message_id = str(value or "").strip()
    if not _PROVIDER_RFC_MESSAGE_ID.fullmatch(message_id):
        raise ValueError("Provider RFC Message-ID is malformed")
    return message_id


class GmailClient:
    def __init__(self, *, operator: str):
        mapping = GMAIL_OPERATOR_MAPPING.get(operator)
        if mapping is None:
            raise EnrichmentError(f"No Gmail mapping configured for operator {operator!r}")
        self.operator = operator
        self.account_key = mapping["gmail_account"]
        self.send_as = mapping["send_as"].lower()
        self.reply_to = mapping["reply_to"].lower()
        path = token_path(self.account_key)
        if not path.exists():
            raise EnrichmentError(
                f"Gmail token missing for {self.account_key}: run gmail_oauth"
            )
        self._creds = Credentials.from_authorized_user_file(str(path), SCOPES)
        if self._creds.expired and self._creds.refresh_token:
            try:
                self._creds.refresh(Request())
            except GoogleAuthError:
                raise EnrichmentError(
                    "Gmail authentication refresh is temporarily unavailable."
                ) from None
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
        for label, address in (("send-as", self.send_as), ("reply-to", self.reply_to)):
            meta = aliases.get(address)
            if not meta:
                raise EnrichmentError(
                    f"{address} is not configured as a {label} alias for {self.account_key}"
                )
            status = (meta.get("verificationStatus") or "").lower()
            if not meta.get("isDefault") and status not in {"accepted", "verified"}:
                raise EnrichmentError(
                    f"{address} is present but not verified for {self.account_key}"
                )

    def _provider_rfc_message_id(self, message_id: str, thread_id: str) -> str:
        """Read the canonical Message-ID header Gmail stored after sending.

        Gmail may replace a caller-supplied Message-ID during submission. A
        continuation must reference the header recipients actually received,
        not the pre-send value embedded in the MIME request.
        """
        try:
            sent = self._service.users().messages().get(
                userId="me",
                id=message_id,
                format="metadata",
                metadataHeaders=["Message-ID"],
            ).execute()
        except HttpError as exc:
            raise EnrichmentError(
                f"Gmail sent-message metadata lookup failed: {exc}"
            ) from exc
        if str(sent.get("id") or "") != message_id:
            raise EnrichmentError("Gmail sent-message metadata returned another message")
        if str(sent.get("threadId") or "") != thread_id:
            raise EnrichmentError("Gmail sent-message metadata returned another thread")
        headers = (sent.get("payload") or {}).get("headers", [])
        values = [
            str(header.get("value") or "").strip()
            for header in headers
            if str(header.get("name") or "").strip().lower() == "message-id"
        ]
        if len(values) != 1 or not values[0]:
            raise EnrichmentError(
                "Gmail sent message needs exactly one RFC Message-ID header"
            )
        try:
            return validated_provider_rfc_message_id(values[0])
        except ValueError as exc:
            raise EnrichmentError("Gmail returned an invalid RFC Message-ID header") from exc

    def send_message(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        delivery_permit: GmailDeliveryPermit | None = None,
        thread_id: str = "",
        in_reply_to: str = "",
        references: Iterable[str] = (),
        rfc_message_id: str = "",
        on_submit_attempt: Callable[[], None] | None = None,
    ) -> GmailSendResult:
        """Submit one exact, Task-authorized Gmail automation message."""
        reply_thread_id = (thread_id or "").strip()
        reply_to_id = (in_reply_to or "").strip()
        reference_ids = tuple(
            value.strip() for value in references if (value or "").strip()
        )
        consume_gmail_delivery_permit(
            delivery_permit,
            operator=self.operator,
            account_key=self.account_key,
            to=to,
            subject=subject,
            body=body,
            thread_id=reply_thread_id,
            in_reply_to=reply_to_id,
            references=reference_ids,
            rfc_message_id=rfc_message_id,
        )
        self.validate_send_as()
        if bool(reply_thread_id) != bool(reply_to_id):
            raise ValueError(
                "Gmail thread continuation requires both thread_id and in_reply_to"
            )

        domain = self.send_as.rsplit("@", 1)[-1]
        message_id_header = _normalized_rfc_message_id(
            rfc_message_id,
            domain=domain,
        )
        msg = EmailMessage()
        msg["To"] = to
        msg["From"] = self.send_as
        msg["Reply-To"] = self.reply_to
        msg["Subject"] = subject
        msg["Message-ID"] = message_id_header
        if reply_thread_id:
            msg["In-Reply-To"] = reply_to_id
            msg["References"] = " ".join(reference_ids or (reply_to_id,))
        msg.set_content(body)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
        request_body = {"raw": raw}
        if reply_thread_id:
            request_body["threadId"] = reply_thread_id
        try:
            request = self._service.users().messages().send(
                userId="me",
                body=request_body,
            )
            if on_submit_attempt is not None:
                on_submit_attempt()
            sent = request.execute()
        except HttpError as exc:
            raise EnrichmentError(f"Gmail send failed: {exc}") from exc
        msg_id = sent.get("id")
        sent_thread_id = sent.get("threadId")
        if not msg_id or not sent_thread_id:
            raise EnrichmentError(f"Gmail send returned incomplete identifiers: {sent}")
        if reply_thread_id and str(sent_thread_id) != reply_thread_id:
            raise EnrichmentError(
                "Gmail continued message returned an unexpected thread ID"
            )
        provider_rfc_message_id = self._provider_rfc_message_id(
            str(msg_id),
            str(sent_thread_id),
        )
        return GmailSendResult(
            message_id=str(msg_id),
            thread_id=str(sent_thread_id),
            rfc_message_id=provider_rfc_message_id,
        )
