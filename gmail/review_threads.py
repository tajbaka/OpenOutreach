"""Paced full-thread retrieval for the read-only contextual CRM review."""

import json
from collections.abc import Iterable, Iterator
from time import monotonic, sleep

from googleapiclient.errors import HttpError


# Full threads cost 120 quota units each. Forty per minute uses 4,800 of
# Google's documented 6,000 per-user budget, leaving room for other reads.
_FULL_THREAD_INTERVAL = 1.5
_RATE_LIMIT_BACKOFF = (2, 4, 8, 16, 32, 60)


def verified_review_users(service, expected_mailbox: str):
    users = service.users()
    profile = users.getProfile(userId="me").execute()
    actual_mailbox = profile.get("emailAddress", "")
    if not expected_mailbox or actual_mailbox.casefold() != expected_mailbox.casefold():
        raise ValueError("Gmail profile does not match the expected review mailbox")
    return users


def _is_rate_limit(error: HttpError) -> bool:
    if error.resp.status == 429:
        return True
    if error.resp.status != 403:
        return False
    try:
        details = json.loads(error.content)["error"]["errors"]
    except (ValueError, KeyError, TypeError):
        return False
    return isinstance(details, list) and any(
        isinstance(detail, dict)
        and detail.get("reason") in ("rateLimitExceeded", "userRateLimitExceeded")
        for detail in details
    )


def iter_review_threads(
    service, thread_ids: Iterable[str], *, expected_mailbox: str,
) -> Iterator[dict]:
    """Yield each exact thread once so the review can save progress incrementally.

    A failed required thread raises after any rate-limit retries. A caller can
    resume with the remaining IDs from the same review after the quota recovers.
    """
    threads = verified_review_users(service, expected_mailbox).threads()
    next_request_at = monotonic()
    for thread_id in dict.fromkeys(thread_ids):
        for retry_delay in (*_RATE_LIMIT_BACKOFF, None):
            wait = next_request_at - monotonic()
            if wait > 0:
                sleep(wait)
            next_request_at = monotonic() + _FULL_THREAD_INTERVAL
            try:
                thread = threads.get(userId="me", id=thread_id, format="full").execute()
            except HttpError as error:
                if retry_delay is None or not _is_rate_limit(error):
                    raise
                next_request_at = monotonic() + retry_delay
                continue
            if thread.get("id") != thread_id:
                raise ValueError("Gmail returned a different thread than requested")
            messages = thread.get("messages")
            if not isinstance(messages, list) or not messages:
                raise ValueError("Gmail full thread response is missing its messages list")
            yield thread
            break
