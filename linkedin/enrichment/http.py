"""urllib JSON helper for the enrichment providers.

Mirrors linkedin/notifications/slack.py's stdlib-only HTTP approach — no new
dependency. Transport-level failures (network error, non-2xx, timeout,
non-JSON body) raise HttpError; providers usually catch it and convert to an
API_FAILURE EnrichmentResult, which drives waterfall failover. Some APIs
signal a clean outcome via a non-2xx status (e.g. Prospeo reports 'no match'
as a 400 with an error_code) — HttpError carries `status` and the parsed
`body` so a provider can distinguish those cases.
"""
from __future__ import annotations

import json
import ssl
from urllib import error, request

import certifi


class HttpError(Exception):
    """A provider HTTP call failed at the transport layer (network error,
    non-2xx status, timeout, or a non-JSON response body).

    `status` is the HTTP status code, or None for a network/timeout error.
    `body` is the parsed JSON response body when the server returned one on
    a non-2xx, else None.
    """

    def __init__(self, message, *, status=None, body=None):
        super().__init__(message)
        self.status = status
        self.body = body


def _request_json(url, *, method, headers=None, payload=None, timeout):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Content-Type": "application/json",
            # Some provider APIs (e.g. BetterContact) sit behind Cloudflare,
            # which 403-bans urllib's default "Python-urllib/x" User-Agent
            # (Cloudflare error 1010). Send a plain app UA instead.
            "User-Agent": "OpenOutreach/1.0",
            **(headers or {}),
        },
    )
    try:
        context = ssl.create_default_context(cafile=certifi.where())
        with request.urlopen(req, timeout=timeout, context=context) as resp:
            body = resp.read().decode("utf-8")
    except error.HTTPError as exc:
        raw = b""
        try:
            raw = exc.read()
        except Exception:
            pass
        parsed = None
        if raw:
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                parsed = None
        raise HttpError(
            f"{method} {url} -> HTTP {exc.code}", status=exc.code, body=parsed,
        ) from exc
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
