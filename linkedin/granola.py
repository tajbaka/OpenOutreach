"""Read-only client for Granola meeting notes and transcripts."""
from __future__ import annotations

import json
import re
import ssl
import time
from collections.abc import Iterator
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any
from urllib import error, request
from urllib.parse import urlencode

import certifi

from linkedin.exceptions import (
    GranolaAuthenticationError,
    GranolaError,
    GranolaNotFoundError,
    GranolaPayloadTooLargeError,
    GranolaRequestError,
    GranolaResponseError,
    GranolaTransientError,
)


NOTE_ID_PATTERN = re.compile(r"^not_[a-zA-Z0-9]{14}$")
_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())


class GranolaClient:
    """Small synchronous client for Granola's public read API."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        timeout: int = 30,
        max_retries: int = 2,
        min_request_interval: float = 0.2,
        max_retry_delay: float = 10.0,
        sleep=time.sleep,
        monotonic=time.monotonic,
    ):
        if not isinstance(api_key, str):
            raise GranolaError("GRANOLA_API_KEY must be a string.")
        if not isinstance(base_url, str):
            raise GranolaError("GRANOLA_API_BASE must be a string.")
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.min_request_interval = min_request_interval
        self.max_retry_delay = max_retry_delay
        self._sleep = sleep
        self._monotonic = monotonic
        self._last_request_at: float | None = None
        if not self.api_key:
            raise GranolaError("GRANOLA_API_KEY is not configured.")
        if not self.base_url:
            raise GranolaError("GRANOLA_API_BASE is not configured.")
        if type(self.timeout) is not int or self.timeout <= 0:
            raise GranolaError("Granola HTTP timeout must be positive.")
        if type(self.max_retries) is not int or self.max_retries < 0:
            raise GranolaError("Granola max_retries cannot be negative.")
        if (
            isinstance(self.min_request_interval, bool)
            or not isinstance(self.min_request_interval, (int, float))
            or self.min_request_interval < 0
        ):
            raise GranolaError("Granola min_request_interval cannot be negative.")
        if (
            isinstance(self.max_retry_delay, bool)
            or not isinstance(self.max_retry_delay, (int, float))
            or self.max_retry_delay < 0
        ):
            raise GranolaError("Granola max_retry_delay cannot be negative.")
        if not callable(self._sleep) or not callable(self._monotonic):
            raise GranolaError("Granola clock and sleep hooks must be callable.")

    def list_notes_page(
        self,
        *,
        created_before: str | None = None,
        created_after: str | None = None,
        updated_after: str | None = None,
        folder_id: str | None = None,
        cursor: str | None = None,
        page_size: int = 30,
    ) -> dict[str, Any]:
        if not 1 <= page_size <= 30:
            raise GranolaError("Granola note page_size must be between 1 and 30.")
        payload = self._get(
            "/notes",
            params={
                "created_before": created_before,
                "created_after": created_after,
                "updated_after": updated_after,
                "folder_id": folder_id,
                "cursor": cursor,
                "page_size": page_size,
            },
        )
        notes = payload.get("notes")
        has_more = payload.get("hasMore")
        next_cursor = payload.get("cursor")
        if not isinstance(notes, list) or not all(isinstance(note, dict) for note in notes):
            raise GranolaResponseError("Granola list-notes response is missing notes[].")
        if not isinstance(has_more, bool):
            raise GranolaResponseError("Granola list-notes response is missing hasMore.")
        if next_cursor is not None and not isinstance(next_cursor, str):
            raise GranolaResponseError("Granola list-notes cursor must be a string or null.")
        if has_more and not next_cursor:
            raise GranolaResponseError(
                "Granola reported more notes without returning a cursor."
            )
        return payload

    def iter_notes(
        self,
        *,
        created_before: str | None = None,
        created_after: str | None = None,
        updated_after: str | None = None,
        folder_id: str | None = None,
        max_notes: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        if max_notes is not None and (
            type(max_notes) is not int or max_notes <= 0
        ):
            raise GranolaError("Granola max_notes must be a positive integer.")
        cursor = None
        seen_cursors: set[str] = set()
        yielded = 0
        while True:
            page = self.list_notes_page(
                created_before=created_before,
                created_after=created_after,
                updated_after=updated_after,
                folder_id=folder_id,
                cursor=cursor,
                page_size=30,
            )
            for note in page["notes"]:
                yield note
                yielded += 1
                if max_notes is not None and yielded >= max_notes:
                    return
            if not page["hasMore"]:
                return
            next_cursor = page["cursor"]
            if next_cursor in seen_cursors:
                raise GranolaResponseError(
                    "Granola list-notes pagination repeated a cursor."
                )
            seen_cursors.add(next_cursor)
            cursor = next_cursor

    def get_note(self, note_id: str) -> dict[str, Any]:
        self._validate_note_id(note_id)
        payload = self._get(f"/notes/{note_id}")
        if payload.get("id") != note_id or payload.get("object") != "note":
            raise GranolaResponseError("Granola get-note response has an unexpected shape.")
        return payload

    def get_transcript(self, note_id: str) -> list[dict[str, Any]]:
        self._validate_note_id(note_id)
        cursor = None
        seen_cursors: set[str] = set()
        transcript: list[dict[str, Any]] = []
        while True:
            payload = self._get(
                f"/notes/{note_id}/transcript",
                params={"cursor": cursor, "page_size": 100},
            )
            items = payload.get("transcript")
            has_more = payload.get("hasMore")
            next_cursor = payload.get("cursor")
            if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
                raise GranolaResponseError(
                    "Granola transcript response is missing transcript[]."
                )
            if not isinstance(has_more, bool):
                raise GranolaResponseError(
                    "Granola transcript response is missing hasMore."
                )
            transcript.extend(items)
            if not has_more:
                return transcript
            if not isinstance(next_cursor, str) or not next_cursor:
                raise GranolaResponseError(
                    "Granola reported more transcript items without a cursor."
                )
            if next_cursor in seen_cursors:
                raise GranolaResponseError(
                    "Granola transcript pagination repeated a cursor."
                )
            seen_cursors.add(next_cursor)
            cursor = next_cursor

    def _validate_note_id(self, note_id: str) -> None:
        if not NOTE_ID_PATTERN.fullmatch(note_id):
            raise GranolaError(
                "Granola note IDs must match not_ followed by 14 letters or digits."
            )

    def _get(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        query = urlencode(
            {key: value for key, value in (params or {}).items() if value is not None}
        )
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{query}"
        req = request.Request(
            url,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "User-Agent": "OpenOutreach-Granola/1.0",
            },
        )
        raw: bytes | None = None
        for attempt in range(self.max_retries + 1):
            self._pace()
            try:
                with request.urlopen(
                    req,
                    timeout=self.timeout,
                    context=_SSL_CONTEXT,
                ) as response:
                    raw = response.read()
                break
            except error.HTTPError as exc:
                detail = _http_error_detail(exc)
                if exc.code in {401, 403}:
                    raise GranolaAuthenticationError(
                        f"Granola rejected the API key or its note scopes "
                        f"(HTTP {exc.code}){detail}."
                    ) from exc
                if exc.code == 404:
                    raise GranolaNotFoundError(
                        f"Granola resource was not found or is no longer accessible{detail}."
                    ) from exc
                if exc.code == 413:
                    raise GranolaPayloadTooLargeError(
                        "Granola could not return the requested payload inline; "
                        "use the paginated transcript endpoint."
                    ) from exc
                retry_after = _retry_after_seconds(exc)
                retryable = exc.code == 429 or 500 <= exc.code <= 599
                if not retryable:
                    raise GranolaRequestError(
                        f"Granola API rejected the request (HTTP {exc.code}){detail}.",
                        status_code=exc.code,
                    ) from exc
                if attempt >= self.max_retries:
                    message = (
                        "Granola API rate limit remained exceeded after bounded retries."
                        if exc.code == 429
                        else f"Granola API remained unavailable after bounded retries "
                        f"(HTTP {exc.code}){detail}."
                    )
                    raise GranolaTransientError(
                        message,
                        status_code=exc.code,
                        retry_after_seconds=retry_after,
                    ) from exc
                self._sleep(self._retry_delay(attempt, retry_after))
            except (error.URLError, TimeoutError, OSError) as exc:
                if attempt >= self.max_retries:
                    raise GranolaTransientError(
                        f"Granola API request failed after bounded retries: {exc}"
                    ) from exc
                self._sleep(self._retry_delay(attempt, None))

        if raw is None:  # pragma: no cover - loop exits only by break or exception
            raise GranolaTransientError("Granola API request produced no response.")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GranolaResponseError("Granola API returned a non-JSON response.") from exc
        if not isinstance(payload, dict):
            raise GranolaResponseError("Granola API response root must be an object.")
        return payload

    def _pace(self) -> None:
        now = self._monotonic()
        if self._last_request_at is not None:
            remaining = self.min_request_interval - (now - self._last_request_at)
            if remaining > 0:
                self._sleep(remaining)
                now = self._monotonic()
        self._last_request_at = now

    def _retry_delay(self, attempt: int, retry_after: float | None) -> float:
        requested = retry_after if retry_after is not None else 0.5 * (2**attempt)
        return min(self.max_retry_delay, max(0.0, requested))


@dataclass(frozen=True)
class GranolaClientSetup:
    """Safe constructor result for workflows that must fall back to Gemini."""

    client: GranolaClient | None
    error: GranolaError | None


def build_granola_client(**options: Any) -> GranolaClientSetup:
    """Build a client without letting configuration errors abort CRM refresh."""
    try:
        client = GranolaClient(**options)
    except GranolaError as exc:
        return GranolaClientSetup(client=None, error=exc)
    return GranolaClientSetup(client=client, error=None)


def _http_error_detail(exc: error.HTTPError) -> str:
    try:
        raw = exc.read()
    except OSError:
        return ""
    if not raw:
        return ""
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    message = payload.get("message") or payload.get("error")
    return f": {message}" if isinstance(message, str) and message else ""


def _retry_after_seconds(exc: error.HTTPError) -> float | None:
    raw = exc.headers.get("Retry-After") if exc.headers else None
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        try:
            retry_at = parsedate_to_datetime(str(raw))
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            return None
        return max(0.0, retry_at.timestamp() - time.time())
