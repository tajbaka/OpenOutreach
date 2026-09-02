import base64
from email import policy
from email.parser import BytesParser

import pytest

from gmail.auth import GMAIL_OPERATOR_MAPPING
from gmail.client import GmailClient, GmailSendResult, scoped_gmail_id
from gmail.delivery import consume_gmail_delivery_permit
from gmail.exceptions import GmailDeliveryAuthorizationError
from linkedin.exceptions import EnrichmentError


@pytest.fixture(autouse=True)
def _allow_client_transport_unit_tests(monkeypatch):
    """Permit tests live separately; these tests exercise MIME/provider behavior."""
    monkeypatch.setattr(
        "gmail.client.consume_gmail_delivery_permit",
        lambda *args, **kwargs: None,
    )


class _Request:
    def __init__(self, response, events, event="execute"):
        self._response = response
        self._events = events
        self._event = event

    def execute(self):
        self._events.append(self._event)
        return self._response


class _Messages:
    def __init__(self, response, metadata_response, events):
        self._response = response
        self._metadata_response = metadata_response
        self._events = events
        self.calls = []
        self.get_calls = []

    def send(self, **kwargs):
        self.calls.append(kwargs)
        self._events.append("request")
        return _Request(self._response, self._events)

    def get(self, **kwargs):
        self.get_calls.append(kwargs)
        self._events.append("get_request")
        return _Request(self._metadata_response, self._events, "get_execute")


class _Users:
    def __init__(self, messages):
        self._messages = messages

    def messages(self):
        return self._messages


class _Service:
    def __init__(self, response, metadata_response):
        self.events = []
        self.messages = _Messages(response, metadata_response, self.events)

    def users(self):
        return _Users(self.messages)


def _client(
    response,
    *,
    provider_rfc_message_id="<provider-message@gmail.com>",
    metadata_response=None,
):
    client = object.__new__(GmailClient)
    client.operator = "Arian"
    client.account_key = "arian_boundera"
    client.send_as = "ariant@getboundera.com"
    client.reply_to = "ariant@boundera.io"
    if metadata_response is None:
        metadata_response = {
            "id": response.get("id", ""),
            "threadId": response.get("threadId", ""),
            "payload": {
                "headers": [
                    {"name": "Message-ID", "value": provider_rfc_message_id},
                ],
            },
        }
    client._service = _Service(response, metadata_response)
    client.validate_send_as = lambda: None
    return client


def _mime_message(client):
    raw = client._service.messages.calls[-1]["body"]["raw"]
    return BytesParser(policy=policy.default).parsebytes(
        base64.urlsafe_b64decode(raw),
    )


def test_send_message_rejects_direct_delivery_before_provider_access(monkeypatch):
    client = _client({"id": "raw-message-1", "threadId": "raw-thread-1"})
    monkeypatch.setattr(
        "gmail.client.consume_gmail_delivery_permit",
        consume_gmail_delivery_permit,
    )

    with pytest.raises(GmailDeliveryAuthorizationError, match="Direct Gmail sends"):
        client.send_message(to="ada@example.com", subject="Subject", body="Body")

    assert client._service.events == []
    assert client._service.messages.calls == []


def test_send_message_returns_distinct_raw_provider_identifiers():
    client = _client(
        {"id": "raw-message-1", "threadId": "raw-thread-1"},
        provider_rfc_message_id="<provider-message-1@gmail.com>",
    )

    result = client.send_message(
        to="ada@example.com",
        subject="A subject",
        body="Hello",
        rfc_message_id="<automation-1@getboundera.com>",
    )

    assert result == GmailSendResult(
        message_id="raw-message-1",
        thread_id="raw-thread-1",
        rfc_message_id="<provider-message-1@gmail.com>",
    )
    request_body = client._service.messages.calls[-1]["body"]
    assert "threadId" not in request_body
    message = _mime_message(client)
    assert message["Message-ID"] == "<automation-1@getboundera.com>"
    assert message["Reply-To"] == "ariant@boundera.io"
    assert message["In-Reply-To"] is None
    assert client._service.messages.get_calls == [{
        "userId": "me",
        "id": "raw-message-1",
        "format": "metadata",
        "metadataHeaders": ["Message-ID"],
    }]


def test_send_message_continues_exact_thread_with_reply_headers():
    client = _client(
        {"id": "raw-message-2", "threadId": "raw-thread-1"},
        provider_rfc_message_id="<provider-message-2@gmail.com>",
    )

    result = client.send_message(
        to="ada@example.com",
        subject="Original subject",
        body="Following up",
        thread_id="raw-thread-1",
        in_reply_to="<automation-1@getboundera.com>",
        references=(
            "<automation-0@getboundera.com>",
            "<automation-1@getboundera.com>",
        ),
        rfc_message_id="automation-2@getboundera.com",
    )

    assert result.thread_id == "raw-thread-1"
    assert result.rfc_message_id == "<provider-message-2@gmail.com>"
    request_body = client._service.messages.calls[-1]["body"]
    assert request_body["threadId"] == "raw-thread-1"
    message = _mime_message(client)
    assert message["Subject"] == "Original subject"
    assert message["In-Reply-To"] == "<automation-1@getboundera.com>"
    assert str(message["References"]) == (
        "<automation-0@getboundera.com> <automation-1@getboundera.com>"
    )
    assert message["Reply-To"] == "ariant@boundera.io"


def test_send_message_runs_callback_at_submission_boundary():
    client = _client({"id": "raw-message-1", "threadId": "raw-thread-1"})

    client.send_message(
        to="ada@example.com",
        subject="Subject",
        body="Body",
        on_submit_attempt=lambda: client._service.events.append("callback"),
    )

    assert client._service.events == [
        "request",
        "callback",
        "execute",
        "get_request",
        "get_execute",
    ]


def test_send_message_callback_can_abort_before_provider_submission():
    client = _client({"id": "raw-message-1", "threadId": "raw-thread-1"})

    def abort():
        client._service.events.append("callback")
        raise ValueError("automation stopped")

    with pytest.raises(ValueError, match="automation stopped"):
        client.send_message(
            to="ada@example.com",
            subject="Subject",
            body="Body",
            on_submit_attempt=abort,
        )

    assert client._service.events == ["request", "callback"]


def test_send_message_requires_complete_continuation_binding():
    client = _client({"id": "raw-message-2", "threadId": "raw-thread-1"})

    with pytest.raises(ValueError, match="requires both"):
        client.send_message(
            to="ada@example.com",
            subject="Subject",
            body="Body",
            thread_id="raw-thread-1",
        )

    assert client._service.messages.calls == []


@pytest.mark.parametrize(
    "response",
    [
        {"threadId": "raw-thread-1"},
        {"id": "raw-message-1"},
    ],
)
def test_send_message_fails_closed_on_incomplete_provider_result(response):
    client = _client(response)

    with pytest.raises(EnrichmentError, match="incomplete identifiers"):
        client.send_message(
            to="ada@example.com",
            subject="Subject",
            body="Body",
        )


def test_send_message_rejects_provider_thread_switch():
    client = _client({"id": "raw-message-2", "threadId": "wrong-thread"})

    with pytest.raises(EnrichmentError, match="unexpected thread ID"):
        client.send_message(
            to="ada@example.com",
            subject="Subject",
            body="Body",
            thread_id="raw-thread-1",
            in_reply_to="<automation-1@getboundera.com>",
        )


def test_send_message_fails_closed_without_provider_rfc_message_id():
    client = _client(
        {"id": "raw-message-1", "threadId": "raw-thread-1"},
        provider_rfc_message_id="",
    )

    with pytest.raises(EnrichmentError, match="exactly one RFC Message-ID"):
        client.send_message(
            to="ada@example.com",
            subject="Subject",
            body="Body",
        )


@pytest.mark.parametrize(
    "provider_rfc_message_id",
    [
        "message@gmail.com",
        "<missing-at>",
        "<bad id@gmail.com>",
        "<<x@gmail.com>>",
        "<x@gmail.com>\r\nInjected",
    ],
)
def test_send_message_rejects_malformed_provider_rfc_message_id(
    provider_rfc_message_id,
):
    client = _client(
        {"id": "raw-message-1", "threadId": "raw-thread-1"},
        provider_rfc_message_id=provider_rfc_message_id,
    )

    with pytest.raises(EnrichmentError, match="invalid RFC Message-ID"):
        client.send_message(to="ada@example.com", subject="Subject", body="Body")


def test_send_message_rejects_duplicate_provider_rfc_message_id_headers():
    client = _client(
        {"id": "raw-message-1", "threadId": "raw-thread-1"},
        metadata_response={
            "id": "raw-message-1",
            "threadId": "raw-thread-1",
            "payload": {"headers": [
                {"name": "Message-ID", "value": "<one@gmail.com>"},
                {"name": "message-id", "value": "<two@gmail.com>"},
            ]},
        },
    )

    with pytest.raises(EnrichmentError, match="exactly one RFC Message-ID"):
        client.send_message(to="ada@example.com", subject="Subject", body="Body")


@pytest.mark.parametrize(
    "metadata_response, error",
    [
        (
            {
                "id": "another-message",
                "threadId": "raw-thread-1",
                "payload": {"headers": [
                    {"name": "Message-ID", "value": "<one@gmail.com>"},
                ]},
            },
            "another message",
        ),
        (
            {
                "id": "raw-message-1",
                "threadId": "another-thread",
                "payload": {"headers": [
                    {"name": "Message-ID", "value": "<one@gmail.com>"},
                ]},
            },
            "another thread",
        ),
    ],
)
def test_send_message_rejects_mismatched_metadata_binding(metadata_response, error):
    client = _client(
        {"id": "raw-message-1", "threadId": "raw-thread-1"},
        metadata_response=metadata_response,
    )

    with pytest.raises(EnrichmentError, match=error):
        client.send_message(to="ada@example.com", subject="Subject", body="Body")


@pytest.mark.parametrize(
    "operator, account_key, send_as, reply_to",
    [
        ("Arian", "arian_boundera", "ariant@getboundera.com", "ariant@boundera.io"),
        ("Leili", "arian_boundera", "leili@getboundera.com", "leili@boundera.io"),
        ("Athena", "eddy_boundera", "athena@getboundera.com", "athena@boundera.io"),
        ("Chuka", "eddy_boundera", "eddy@getboundera.com", "eddy@boundera.io"),
        ("Eddy", "eddy_boundera", "eddy@getboundera.com", "eddy@boundera.io"),
    ],
)
def test_operator_delivery_identity_mapping(operator, account_key, send_as, reply_to):
    assert GMAIL_OPERATOR_MAPPING[operator] == {
        **({"display_name": "Eddy"} if operator == "Chuka" else {}),
        "gmail_account": account_key,
        "send_as": send_as,
        "reply_to": reply_to,
    }


def test_validate_send_as_requires_verified_from_and_reply_to_aliases():
    client = object.__new__(GmailClient)
    client.account_key = "arian_boundera"
    client.send_as = "leili@getboundera.com"
    client.reply_to = "leili@boundera.io"
    aliases = {
        client.send_as: {"verificationStatus": "accepted"},
        client.reply_to: {"verificationStatus": "accepted"},
    }
    client.send_as_aliases = lambda: aliases

    client.validate_send_as()
    client.reply_to = "wrong@boundera.io"
    with pytest.raises(EnrichmentError, match="not configured as a reply-to alias"):
        client.validate_send_as()


def test_scoped_gmail_id_namespaces_mailbox_local_ids():
    assert scoped_gmail_id("arian_boundera", "same") == "arian_boundera:same"
    assert scoped_gmail_id("eddy_boundera", "same") == "eddy_boundera:same"
