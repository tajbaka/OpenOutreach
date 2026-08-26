"""Small, fail-closed Trello REST client for the curated sales pipeline."""
from __future__ import annotations

import json
import re
import ssl
import time
from email.utils import parsedate_to_datetime
from typing import Any, Callable
from urllib import error, request
from urllib.parse import urlencode

import certifi

from linkedin.exceptions import (
    TrelloAuthenticationError,
    TrelloError,
    TrelloResponseError,
    TrelloTransientError,
)


_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())


class TrelloClient:
    """Synchronous Trello client with bounded, sanitized retries.

    Authentication is carried only in the Authorization header so API keys and
    tokens never appear in URLs, provider error strings, or scheduled logs.
    """

    def __init__(
        self,
        *,
        api_key: str,
        api_token: str,
        base_url: str = "https://api.trello.com/1",
        timeout: int = 30,
        max_retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
        opener: Callable[..., Any] = request.urlopen,
    ):
        self.api_key = str(api_key or "").strip()
        self.api_token = str(api_token or "").strip()
        self.base_url = str(base_url or "").strip().rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self._sleep = sleep
        self._opener = opener
        if not self.api_key or not self.api_token:
            raise TrelloError("Trello API key and token must both be configured.")
        if not self.base_url.startswith("https://"):
            raise TrelloError("Trello API base URL must use HTTPS.")
        if type(timeout) is not int or timeout <= 0:
            raise TrelloError("Trello HTTP timeout must be a positive integer.")
        if type(max_retries) is not int or max_retries < 0:
            raise TrelloError("Trello max_retries cannot be negative.")

    def get_board(self, board_id: str) -> dict[str, Any]:
        board_id = _validated_id(board_id, label="board")
        payload = self._request(
            "GET",
            f"/boards/{board_id}",
            params={"fields": "id,name,closed,url,dateLastActivity,limits"},
        )
        if not isinstance(payload, dict) or payload.get("id") != board_id:
            raise TrelloResponseError("Trello returned an unexpected board response.")
        return payload

    def list_open_lists(self, board_id: str) -> list[dict[str, Any]]:
        board_id = _validated_id(board_id, label="board")
        payload = self._request(
            "GET",
            f"/boards/{board_id}/lists",
            params={"filter": "open", "fields": "id,name,closed,pos"},
        )
        return _object_list(payload, context="board lists")

    def list_open_cards(self, board_id: str) -> list[dict[str, Any]]:
        board_id = _validated_id(board_id, label="board")
        payload = self._request(
            "GET",
            f"/boards/{board_id}/cards",
            params={
                "filter": "open",
                "fields": (
                    "id,name,desc,idList,closed,dateLastActivity,due,dueComplete,"
                    "idMembers,labels"
                ),
            },
        )
        return _object_list(payload, context="board cards")

    def create_list(self, board_id: str, *, name: str, position: str = "bottom") -> dict:
        board_id = _validated_id(board_id, label="board")
        clean_name = _bounded_text(name, label="list name", limit=128)
        payload = self._request(
            "POST",
            "/lists",
            data={"idBoard": board_id, "name": clean_name, "pos": position},
        )
        if not isinstance(payload, dict) or not _valid_id(payload.get("id")):
            raise TrelloResponseError("Trello returned an unexpected create-list response.")
        return payload

    def create_card(self, *, list_id: str, name: str, description: str) -> dict:
        list_id = _validated_id(list_id, label="list")
        payload = self._request(
            "POST",
            "/cards",
            data={
                "idList": list_id,
                "name": _bounded_text(name, label="card name", limit=512),
                "desc": _bounded_text(
                    description,
                    label="card description",
                    limit=16_000,
                    allow_blank=True,
                ),
                "pos": "bottom",
            },
        )
        if not isinstance(payload, dict) or not _valid_id(payload.get("id")):
            raise TrelloResponseError("Trello returned an unexpected create-card response.")
        return payload

    def update_card(self, card_id: str, **fields: Any) -> dict:
        card_id = _validated_id(card_id, label="card")
        allowed = {"idList", "name", "desc", "due", "dueComplete"}
        unknown = set(fields) - allowed
        if unknown:
            raise TrelloError("Unsupported Trello card update field.")
        if "idList" in fields:
            fields["idList"] = _validated_id(fields["idList"], label="list")
        if "name" in fields:
            fields["name"] = _bounded_text(fields["name"], label="card name", limit=512)
        if "desc" in fields:
            fields["desc"] = _bounded_text(
                fields["desc"],
                label="card description",
                limit=16_000,
                allow_blank=True,
            )
        payload = self._request("PUT", f"/cards/{card_id}", data=fields)
        if not isinstance(payload, dict) or payload.get("id") != card_id:
            raise TrelloResponseError("Trello returned an unexpected update-card response.")
        return payload

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> Any:
        query = urlencode({key: value for key, value in (params or {}).items()})
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{query}"
        encoded = None
        headers = {
            "Accept": "application/json",
            "Authorization": (
                'OAuth oauth_consumer_key="'
                f"{self.api_key}"
                '", oauth_token="'
                f"{self.api_token}"
                '"'
            ),
            "User-Agent": "OpenOutreach-Trello/1.0",
        }
        if data is not None:
            encoded = urlencode(_form_values(data)).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = request.Request(url, data=encoded, headers=headers, method=method)

        raw: bytes | None = None
        for attempt in range(self.max_retries + 1):
            try:
                with self._opener(
                    req,
                    timeout=self.timeout,
                    context=_SSL_CONTEXT,
                ) as response:
                    raw = response.read()
                break
            except error.HTTPError as exc:
                if exc.code in {401, 403}:
                    raise TrelloAuthenticationError(
                        "Trello rejected the configured credentials or board access."
                    ) from exc
                retryable = exc.code == 429 or 500 <= exc.code <= 599
                if not retryable:
                    raise TrelloError(
                        f"Trello rejected a pipeline request (HTTP {exc.code})."
                    ) from exc
                # A POST that reached Trello and then failed with a 5xx has an
                # ambiguous outcome.  Retrying could manufacture duplicate
                # cards/lists, so only an explicit rate-limit rejection is
                # safe to retry.  GET/PUT requests are idempotent.
                safe_to_retry = method in {"GET", "PUT"} or exc.code == 429
                if not safe_to_retry or attempt >= self.max_retries:
                    raise TrelloTransientError(
                        "Trello remained unavailable after bounded retries.",
                        status_code=exc.code,
                    ) from exc
                self._sleep(_retry_delay(exc, attempt))
            except (error.URLError, TimeoutError, OSError) as exc:
                # Transport failure after POST is also ambiguous.  A later
                # sync will discover any completed object by its footer ID.
                if method == "POST" or attempt >= self.max_retries:
                    raise TrelloTransientError(
                        "Trello transport failed after bounded retries."
                    ) from exc
                self._sleep(min(30.0, 1.0 * (2**attempt)))

        if raw is None:  # pragma: no cover - loop exits through response/exception
            raise TrelloTransientError("Trello request produced no response.")
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TrelloResponseError("Trello returned a non-JSON response.") from exc


def _object_list(payload: Any, *, context: str) -> list[dict[str, Any]]:
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        raise TrelloResponseError(f"Trello returned invalid {context}.")
    if any(not _valid_id(item.get("id")) for item in payload):
        raise TrelloResponseError(f"Trello returned {context} without stable IDs.")
    return payload


def _valid_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_ID_RE.fullmatch(value))


def _validated_id(value: Any, *, label: str) -> str:
    clean = str(value or "").strip()
    if not _valid_id(clean):
        raise TrelloError(f"Trello {label} ID is invalid.")
    return clean


def _bounded_text(
    value: Any,
    *,
    label: str,
    limit: int,
    allow_blank: bool = False,
) -> str:
    clean = str(value or "").strip()
    if (not clean and not allow_blank) or len(clean) > limit:
        raise TrelloError(f"Trello {label} is invalid.")
    return clean


def _form_values(values: dict[str, Any]) -> dict[str, str]:
    result = {}
    for key, value in values.items():
        if value is None:
            result[key] = ""
        elif isinstance(value, bool):
            result[key] = "true" if value else "false"
        else:
            result[key] = str(value)
    return result


def _retry_delay(exc: error.HTTPError, attempt: int) -> float:
    raw = exc.headers.get("Retry-After") if exc.headers else None
    if raw is not None:
        try:
            return min(30.0, max(0.0, float(raw)))
        except (TypeError, ValueError):
            try:
                retry_at = parsedate_to_datetime(str(raw))
            except (TypeError, ValueError, OverflowError):
                pass
            else:
                if retry_at.tzinfo is not None:
                    return min(30.0, max(0.0, retry_at.timestamp() - time.time()))
    return min(30.0, 1.0 * (2**attempt))
