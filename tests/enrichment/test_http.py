"""Tests for the enrichment urllib JSON helper."""
import io
import json
from unittest.mock import MagicMock, patch

import pytest

from linkedin.enrichment.http import HttpError, get_json, post_json


def _fake_response(payload: dict):
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode("utf-8")
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


def test_post_json_returns_parsed_body():
    with patch("linkedin.enrichment.http.request.urlopen",
               return_value=_fake_response({"id": "abc"})):
        result = post_json("https://x.test/submit", payload={"q": 1}, timeout=5)
    assert result == {"id": "abc"}


def test_request_sends_non_urllib_user_agent():
    """http.py must send a real User-Agent. Some provider APIs sit behind
    Cloudflare, which 403-bans urllib's default 'Python-urllib' UA
    (Cloudflare error 1010) — that would silently break BetterContact."""
    with patch("linkedin.enrichment.http.request.urlopen",
               return_value=_fake_response({"ok": True})) as mock_open:
        post_json("https://x.test/submit", payload={}, timeout=5)
    sent = mock_open.call_args[0][0]
    ua = sent.get_header("User-agent")
    assert ua and "urllib" not in ua.lower()


def test_get_json_returns_parsed_body():
    with patch("linkedin.enrichment.http.request.urlopen",
               return_value=_fake_response({"status": "terminated"})):
        result = get_json("https://x.test/poll", timeout=5)
    assert result == {"status": "terminated"}


def test_http_error_status_raises_httperror():
    from urllib.error import HTTPError

    err = HTTPError("https://x.test", 500, "Server Error", {}, io.BytesIO(b""))
    with patch("linkedin.enrichment.http.request.urlopen", side_effect=err):
        with pytest.raises(HttpError) as ei:
            post_json("https://x.test/submit", payload={}, timeout=5)
    assert ei.value.status == 500
    assert ei.value.body is None


def test_http_error_carries_parsed_json_body():
    """A non-2xx with a JSON body exposes it on HttpError.body — providers
    rely on this to distinguish a structured outcome (e.g. Prospeo's
    400/NO_MATCH) from a real transport failure."""
    from urllib.error import HTTPError

    err = HTTPError("https://x.test", 400, "Bad Request", {},
                    io.BytesIO(b'{"error_code": "NO_MATCH"}'))
    with patch("linkedin.enrichment.http.request.urlopen", side_effect=err):
        with pytest.raises(HttpError) as ei:
            post_json("https://x.test/submit", payload={}, timeout=5)
    assert ei.value.status == 400
    assert ei.value.body == {"error_code": "NO_MATCH"}


def test_network_error_raises_httperror():
    from urllib.error import URLError

    with patch("linkedin.enrichment.http.request.urlopen",
               side_effect=URLError("connection refused")):
        with pytest.raises(HttpError) as ei:
            get_json("https://x.test/poll", timeout=5)
    assert ei.value.status is None


def test_non_json_body_raises_httperror():
    resp = MagicMock()
    resp.read.return_value = b"<html>not json</html>"
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    with patch("linkedin.enrichment.http.request.urlopen", return_value=resp):
        with pytest.raises(HttpError):
            get_json("https://x.test/poll", timeout=5)
