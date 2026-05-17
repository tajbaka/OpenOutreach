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


def test_get_json_returns_parsed_body():
    with patch("linkedin.enrichment.http.request.urlopen",
               return_value=_fake_response({"status": "terminated"})):
        result = get_json("https://x.test/poll", timeout=5)
    assert result == {"status": "terminated"}


def test_http_error_status_raises_httperror():
    from urllib.error import HTTPError

    err = HTTPError("https://x.test", 500, "Server Error", {}, io.BytesIO(b""))
    with patch("linkedin.enrichment.http.request.urlopen", side_effect=err):
        with pytest.raises(HttpError):
            post_json("https://x.test/submit", payload={}, timeout=5)


def test_network_error_raises_httperror():
    from urllib.error import URLError

    with patch("linkedin.enrichment.http.request.urlopen",
               side_effect=URLError("connection refused")):
        with pytest.raises(HttpError):
            get_json("https://x.test/poll", timeout=5)


def test_non_json_body_raises_httperror():
    resp = MagicMock()
    resp.read.return_value = b"<html>not json</html>"
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    with patch("linkedin.enrichment.http.request.urlopen", return_value=resp):
        with pytest.raises(HttpError):
            get_json("https://x.test/poll", timeout=5)
