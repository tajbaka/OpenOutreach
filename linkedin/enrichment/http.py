"""urllib JSON helper for the enrichment providers.

Mirrors linkedin/notifications/slack.py's stdlib-only HTTP approach — no new
dependency. Transport-level failures (network error, non-2xx, timeout,
non-JSON body) raise HttpError; providers catch it and convert to an
API_FAILURE EnrichmentResult, which drives waterfall failover.
"""
from __future__ import annotations

import json
from urllib import error, request


class HttpError(Exception):
    """A provider HTTP call failed at the transport layer (network error,
    non-2xx status, timeout, or a non-JSON response body)."""
    pass


def _request_json(url, *, method, headers=None, payload=None, timeout):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
    except error.HTTPError as exc:
        raise HttpError(f"{method} {url} -> HTTP {exc.code}") from exc
    except (error.URLError, TimeoutError, OSError) as exc:
        raise HttpError(f"{method} {url} -> {exc}") from exc
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise HttpError(f"{method} {url} -> non-JSON response body") from exc


def post_json(url, *, headers=None, payload=None, timeout):
    """POST a JSON body, return the parsed JSON response. Raises HttpError."""
    return _request_json(
        url, method="POST", headers=headers, payload=payload, timeout=timeout,
    )


def get_json(url, *, headers=None, timeout):
    """GET a URL, return the parsed JSON response. Raises HttpError."""
    return _request_json(url, method="GET", headers=headers, timeout=timeout)
