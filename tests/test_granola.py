from __future__ import annotations

import io
import json
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

import pytest

from linkedin.exceptions import (
    GranolaAuthenticationError,
    GranolaError,
    GranolaNotFoundError,
    GranolaPayloadTooLargeError,
    GranolaRequestError,
    GranolaResponseError,
    GranolaTransientError,
)
from linkedin.granola import GranolaClient, build_granola_client


def _response(payload: dict):
    response = MagicMock()
    response.read.return_value = json.dumps(payload).encode("utf-8")
    response.__enter__.return_value = response
    response.__exit__.return_value = False
    return response


def _client() -> GranolaClient:
    return GranolaClient(
        api_key="grn_test",
        base_url="https://public-api.granola.ai/v1",
        timeout=10,
    )


def test_safe_client_factory_returns_configuration_error_without_raising():
    setup = build_granola_client(
        api_key="grn_test",
        base_url="https://public-api.granola.ai/v1",
        timeout=0,
    )

    assert setup.client is None
    assert isinstance(setup.error, GranolaError)
    assert "positive" in str(setup.error)


def test_list_notes_sends_bearer_auth_and_filters():
    payload = {"notes": [], "hasMore": False, "cursor": None}
    with patch("linkedin.granola.request.urlopen", return_value=_response(payload)) as open_url:
        result = _client().list_notes_page(created_after="2026-08-01", page_size=20)

    assert result == payload
    sent_request = open_url.call_args.args[0]
    assert sent_request.get_header("Authorization") == "Bearer grn_test"
    assert "created_after=2026-08-01" in sent_request.full_url
    assert "page_size=20" in sent_request.full_url


def test_iter_notes_follows_cursor_pages():
    first = {
        "notes": [{"id": "not_aaaaaaaaaaaaaa"}],
        "hasMore": True,
        "cursor": "next-page",
    }
    second = {
        "notes": [{"id": "not_bbbbbbbbbbbbbb"}],
        "hasMore": False,
        "cursor": None,
    }
    with patch(
        "linkedin.granola.request.urlopen",
        side_effect=[_response(first), _response(second)],
    ) as open_url:
        notes = list(_client().iter_notes())

    assert [note["id"] for note in notes] == ["not_aaaaaaaaaaaaaa", "not_bbbbbbbbbbbbbb"]
    assert "cursor=next-page" in open_url.call_args_list[1].args[0].full_url


def test_get_transcript_combines_all_pages():
    first = {
        "transcript": [{"text": "first"}],
        "hasMore": True,
        "cursor": "more",
    }
    second = {
        "transcript": [{"text": "second"}],
        "hasMore": False,
        "cursor": None,
    }
    with patch(
        "linkedin.granola.request.urlopen",
        side_effect=[_response(first), _response(second)],
    ):
        transcript = _client().get_transcript("not_aaaaaaaaaaaaaa")

    assert [item["text"] for item in transcript] == ["first", "second"]


def test_auth_error_is_safe_and_does_not_include_key():
    http_error = HTTPError(
        "https://public-api.granola.ai/v1/notes",
        401,
        "Unauthorized",
        {},
        io.BytesIO(b'{"message":"invalid token"}'),
    )
    with patch("linkedin.granola.request.urlopen", side_effect=http_error):
        with pytest.raises(GranolaAuthenticationError) as raised:
            _client().list_notes_page()

    assert "rejected the API key" in str(raised.value)
    assert "grn_test" not in str(raised.value)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (404, GranolaNotFoundError),
        (413, GranolaPayloadTooLargeError),
        (422, GranolaRequestError),
    ],
)
def test_terminal_http_errors_are_typed_and_not_retried(status, expected):
    http_error = HTTPError(
        "https://public-api.granola.ai/v1/notes/not_aaaaaaaaaaaaaa",
        status,
        "terminal",
        {},
        io.BytesIO(b"{}"),
    )
    client = GranolaClient(
        api_key="grn_test",
        base_url="https://public-api.granola.ai/v1",
        max_retries=3,
        min_request_interval=0,
    )
    with patch(
        "linkedin.granola.request.urlopen",
        side_effect=http_error,
    ) as open_url:
        with pytest.raises(expected):
            client.get_note("not_aaaaaaaaaaaaaa")

    assert open_url.call_count == 1


def test_invalid_list_shape_fails_closed():
    with patch(
        "linkedin.granola.request.urlopen",
        return_value=_response({"notes": {}, "hasMore": False, "cursor": None}),
    ):
        with pytest.raises(GranolaResponseError, match=r"notes\[\]"):
            _client().list_notes_page()


def test_rate_limit_retries_are_bounded_and_honor_retry_after():
    retry = HTTPError(
        "https://public-api.granola.ai/v1/notes",
        429,
        "Too Many Requests",
        {"Retry-After": "2"},
        io.BytesIO(b'{"message":"slow down"}'),
    )
    payload = {"notes": [], "hasMore": False, "cursor": None}
    sleeps: list[float] = []
    client = GranolaClient(
        api_key="grn_test",
        base_url="https://public-api.granola.ai/v1",
        max_retries=1,
        min_request_interval=0,
        sleep=sleeps.append,
    )

    with patch(
        "linkedin.granola.request.urlopen",
        side_effect=[retry, _response(payload)],
    ) as open_url:
        assert client.list_notes_page() == payload

    assert open_url.call_count == 2
    assert sleeps == [2.0]


def test_persistent_server_failure_raises_typed_transient_error():
    def server_error():
        return HTTPError(
            "https://public-api.granola.ai/v1/notes",
            503,
            "Unavailable",
            {},
            io.BytesIO(b"{}"),
        )

    sleeps: list[float] = []
    client = GranolaClient(
        api_key="grn_test",
        base_url="https://public-api.granola.ai/v1",
        max_retries=2,
        min_request_interval=0,
        sleep=sleeps.append,
    )
    with patch(
        "linkedin.granola.request.urlopen",
        side_effect=[server_error(), server_error(), server_error()],
    ) as open_url:
        with pytest.raises(GranolaTransientError) as raised:
            client.list_notes_page()

    assert raised.value.status_code == 503
    assert open_url.call_count == 3
    assert sleeps == [0.5, 1.0]


def test_client_paces_consecutive_requests():
    clock = [0.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock[0] += seconds

    client = GranolaClient(
        api_key="grn_test",
        base_url="https://public-api.granola.ai/v1",
        max_retries=0,
        min_request_interval=0.2,
        sleep=sleep,
        monotonic=lambda: clock[0],
    )
    payload = {"notes": [], "hasMore": False, "cursor": None}
    with patch(
        "linkedin.granola.request.urlopen",
        side_effect=[_response(payload), _response(payload)],
    ):
        client.list_notes_page()
        client.list_notes_page()

    assert sleeps == [0.2]
