import base64
from email import policy
from email.parser import BytesParser

import pytest

from gmail.client import GmailClient, GmailSendResult, scoped_gmail_id
from linkedin.exceptions import EnrichmentError


class _Request:
    def __init__(self, response, events):
        self._response = response
        self._events = events

    def execute(self):
        self._events.append("execute")
        return self._response


class _Messages:
    def __init__(self, response, events):
        self._response = response
        self._events = events
        self.calls = []

    def send(self, **kwargs):
        self.calls.append(kwargs)
        self._events.append("request")
        return _Request(self._response, self._events)


class _Users:
    def __init__(self, messages):
        self._messages = messages

    def messages(self):
        return self._messages


class _Service:
    def __init__(self, response):
        self.events = []
        self.messages = _Messages(response, self.events)

    def users(self):
        return _Users(self.messages)


def _client(response):
    client = object.__new__(GmailClient)
    client.operator = "Arian"
    client.account_key = "arian_boundera"
    client.send_as = "ariant@getboundera.com"
    client._service = _Service(response)
    client.validate_send_as = lambda: None
    return client


def _mime_message(client):
    raw = client._service.messages.calls[-1]["body"]["raw"]
    return BytesParser(policy=policy.default).parsebytes(
        base64.urlsafe_b64decode(raw),
    )


def test_send_message_returns_distinct_raw_provider_identifiers():
    client = _client({"id": "raw-message-1", "threadId": "raw-thread-1"})

    result = client.send_message(
        to="ada@example.com",
        subject="A subject",
        body="Hello",
        rfc_message_id="<automation-1@getboundera.com>",
    )

    assert result == GmailSendResult(
        message_id="raw-message-1",
        thread_id="raw-thread-1",
        rfc_message_id="<automation-1@getboundera.com>",
    )
    request_body = client._service.messages.calls[-1]["body"]
    assert "threadId" not in request_body
    message = _mime_message(client)
    assert message["Message-ID"] == "<automation-1@getboundera.com>"
    assert message["In-Reply-To"] is None


def test_send_message_continues_exact_thread_with_reply_headers():
    client = _client({"id": "raw-message-2", "threadId": "raw-thread-1"})

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
    assert result.rfc_message_id == "<automation-2@getboundera.com>"
    request_body = client._service.messages.calls[-1]["body"]
    assert request_body["threadId"] == "raw-thread-1"
    message = _mime_message(client)
    assert message["Subject"] == "Original subject"
    assert message["In-Reply-To"] == "<automation-1@getboundera.com>"
    assert str(message["References"]) == (
        "<automation-0@getboundera.com> <automation-1@getboundera.com>"
    )


def test_send_message_runs_callback_at_submission_boundary():
    client = _client({"id": "raw-message-1", "threadId": "raw-thread-1"})

    client.send_message(
        to="ada@example.com",
        subject="Subject",
        body="Body",
        on_submit_attempt=lambda: client._service.events.append("callback"),
    )

    assert client._service.events == ["request", "callback", "execute"]


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


def test_scoped_gmail_id_namespaces_mailbox_local_ids():
    assert scoped_gmail_id("arian_boundera", "same") == "arian_boundera:same"
    assert scoped_gmail_id("eddy_boundera", "same") == "eddy_boundera:same"
