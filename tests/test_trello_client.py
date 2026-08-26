import io
import json
from urllib import error

import pytest

from linkedin.exceptions import TrelloAuthenticationError, TrelloTransientError
from linkedin.trello import TrelloClient


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def _http_error(code, *, headers=None):
    return error.HTTPError(
        "https://api.trello.com/1/boards/board1",
        code,
        "provider message that must not escape",
        headers or {},
        io.BytesIO(b"provider body that must not escape"),
    )


def test_get_retries_429_without_putting_credentials_in_url():
    outcomes = [
        _http_error(429, headers={"Retry-After": "0"}),
        FakeResponse({"id": "board1", "name": "Pipeline"}),
    ]
    requests = []
    sleeps = []

    def opener(req, **kwargs):
        requests.append(req)
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    client = TrelloClient(
        api_key="super-secret-key",
        api_token="super-secret-token",
        opener=opener,
        sleep=sleeps.append,
    )

    board = client.get_board("board1")

    assert board["id"] == "board1"
    assert len(requests) == 2
    assert sleeps == [0.0]
    assert all("super-secret" not in req.full_url for req in requests)
    assert "oauth_consumer_key" in requests[0].headers["Authorization"]


def test_exhausted_429_is_bounded_and_sanitized():
    calls = 0

    def opener(req, **kwargs):
        nonlocal calls
        calls += 1
        raise _http_error(429, headers={"Retry-After": "0"})

    client = TrelloClient(
        api_key="secret-key",
        api_token="secret-token",
        opener=opener,
        sleep=lambda _seconds: None,
        max_retries=2,
    )

    with pytest.raises(TrelloTransientError) as caught:
        client.get_board("board1")

    assert calls == 3
    assert caught.value.status_code == 429
    assert "secret" not in str(caught.value)
    assert "provider" not in str(caught.value)


def test_auth_failure_does_not_retry_or_expose_provider_body():
    calls = 0

    def opener(req, **kwargs):
        nonlocal calls
        calls += 1
        raise _http_error(401)

    client = TrelloClient(
        api_key="secret-key",
        api_token="secret-token",
        opener=opener,
        sleep=lambda _seconds: None,
    )

    with pytest.raises(TrelloAuthenticationError) as caught:
        client.get_board("board1")

    assert calls == 1
    assert "secret" not in str(caught.value)
    assert "provider" not in str(caught.value)


def test_ambiguous_post_transport_failure_is_never_retried():
    calls = 0

    def opener(req, **kwargs):
        nonlocal calls
        calls += 1
        raise error.URLError("connection reset after send")

    client = TrelloClient(
        api_key="key",
        api_token="token",
        opener=opener,
        sleep=lambda _seconds: None,
        max_retries=3,
    )

    with pytest.raises(TrelloTransientError):
        client.create_card(list_id="list1", name="Ramp", description="safe")

    assert calls == 1


def test_ambiguous_post_server_failure_is_never_retried():
    calls = 0

    def opener(req, **kwargs):
        nonlocal calls
        calls += 1
        raise _http_error(500)

    client = TrelloClient(
        api_key="key",
        api_token="token",
        opener=opener,
        sleep=lambda _seconds: None,
        max_retries=3,
    )

    with pytest.raises(TrelloTransientError) as caught:
        client.create_card(list_id="list1", name="Ramp", description="safe")

    assert calls == 1
    assert caught.value.status_code == 500
