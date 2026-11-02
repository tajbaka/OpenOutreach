"""Queued personal LinkedIn posts: strict validation, ledger transitions and one worker run.

Arian or Eddy (Chuka) posting an approved post from their own profile, not a
repost (user decision, 2026-11-02). Boundera Social Studio owns approval and
scheduling: when the user schedules such a post, its dashboard writes one
queued ``LinkedInPost`` row with ``scheduled_for`` and later reads the result
back; the worker claims a row only once that time has come. Media comes from
the row's ``media_url`` (an exact Sanity file copy) and is used only when its
SHA-256 and size match. One attempt per row, and anything after the final
click is attempted is ``uncertain``, never retried. Module import stays
stdlib-only so the supervisor can read constants before Django setup. Mirrors
``linkedin.reposts``; see docs/linkedin-posts.md.
"""
from __future__ import annotations

import hashlib
import logging
import math
import os
import re
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path

from linkedin.reposts import (
    COMMENTARY_TYPE_MAX_DELAY_MS,
    POST_URL_RE,
    PROCESS_GRACE_SECONDS,
    REPOST_OPERATORS,
    RepostTargetReceipt,
    RepostWatchdog,
)

logger = logging.getLogger(__name__)

# The same two people as reposts; Eddy is Chuka's other name.
POST_OPERATORS = REPOST_OPERATORS
_OPERATOR_INPUTS = frozenset({"arian", "chuka", "eddy"})

MAX_COMMENTARY_CHARS = 3000
MAX_DOCUMENT_TITLE_CHARS = 200
# retry_linkedin_post after a fix: at most two more tries per row (three in all).
MAX_POST_RETRIES = 2
MAX_EXTERNAL_REF_CHARS = 100
MAX_MEDIA_BYTES = 100 * 1024 * 1024
MEDIA_KINDS = ("", "image", "document")
_MEDIA_EXTENSIONS = {"image": (".png", ".jpg", ".jpeg", ".gif"), "document": (".pdf",)}
# Only Sanity's file CDN, which serves the exact bytes it stores.
MEDIA_URL_RE = re.compile(r"https://cdn\.sanity\.io/files/[a-z0-9]+/[a-z0-9_-]+/[0-9a-f]{40}\.[a-z0-9]{2,5}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_MEDIA_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}")
_EXTERNAL_REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,99}")
_ERROR_TYPE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,79}")

# Fixed browser work (feed, composer, upload and document processing) plus
# the slowest possible typing bounds every row.
BASE_BUDGET_SECONDS = 360
MEDIA_DOWNLOAD_TIMEOUT_SECONDS = 60
SUPERVISOR_TIMEOUT_SECONDS = (
    BASE_BUDGET_SECONDS
    + math.ceil(MAX_COMMENTARY_CHARS * COMMENTARY_TYPE_MAX_DELAY_MS / 1000)
    + 3 * PROCESS_GRACE_SECONDS
)


def posts_enabled() -> bool:
    return os.getenv("ENABLE_LINKEDIN_POSTS", "false").strip().lower() in {"1", "true", "yes", "on"}


def budget_seconds(commentary: str) -> int:
    """Wall-clock bound for one row, measured from its claim."""
    typing = math.ceil(len(commentary or "") * COMMENTARY_TYPE_MAX_DELAY_MS / 1000)
    return BASE_BUDGET_SECONDS + typing


def safe_error_type(value: str | None) -> str:
    """Class-name-shaped identifier only; never a raw message or page text."""
    value = (value or "").strip()
    return value if _ERROR_TYPE_RE.fullmatch(value) else "PostError"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _invalid(message: str):
    from linkedin.exceptions import LinkedInPostValidationError

    return LinkedInPostValidationError(message)


def validate_operator(value: str | None) -> str:
    """Accept only Arian, Chuka or Eddy (case-insensitive); return the canonical handle."""
    from linkedin.operators import resolve_operator

    key = (value or "").strip().lower()
    if key not in _OPERATOR_INPUTS:
        raise _invalid("--operator must be Arian, Chuka or Eddy.")
    operator = resolve_operator(key)
    if operator not in POST_OPERATORS:
        raise _invalid("--operator must be Arian, Chuka or Eddy.")
    return operator


def validate_external_ref(value: str | None) -> str:
    ref = value or ""
    if not _EXTERNAL_REF_RE.fullmatch(ref):
        raise _invalid(
            f"--external-ref must be 1-{MAX_EXTERNAL_REF_CHARS} characters of "
            "letters, digits, '.', '_', ':' or '-', starting with a letter or digit."
        )
    return ref


def _plain_text(text: str, label: str, limit: int, *, line_breaks: bool) -> str:
    if len(text) > limit:
        raise _invalid(f"{label} must be at most {limit} characters.")
    if text != text.strip():
        raise _invalid(f"{label} must not start or end with whitespace.")
    if "﻿" in text:
        raise _invalid(f"{label} must not contain a byte-order mark.")
    # Only \n is typed as a line break (Shift+Enter); other control characters
    # would move focus or mean something else to the editor.
    allowed = "\n" if line_breaks else ""
    if any(char not in allowed and unicodedata.category(char) == "Cc" for char in text):
        raise _invalid(f"{label} may not contain control characters" + (" other than line breaks." if line_breaks else "."))
    return text


def validate_commentary(value: str) -> str:
    """Exact post text to type; nothing is normalized."""
    return _plain_text(value or "", "Post text", MAX_COMMENTARY_CHARS, line_breaks=True)


MAX_TEXT_FILE_BYTES = 16_384


def read_text_file(path: str | Path) -> str:
    path = Path(path)
    try:
        if path.stat().st_size > MAX_TEXT_FILE_BYTES:
            raise _invalid("Post text file is too large.")
        raw = path.read_bytes()
    except OSError:
        raise _invalid("Post text file could not be read.") from None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise _invalid("Post text file must be UTF-8.") from None
    return validate_commentary(text)


def validate_media(*, kind: str, url: str, sha256: str, size: int | None, name: str, title: str) -> dict:
    """Media fields as a unit: none at all, or a complete image or document."""
    kind = kind or ""
    if kind not in MEDIA_KINDS:
        raise _invalid("Media kind must be '', 'image' or 'document'.")
    if not kind:
        if url or sha256 or size is not None or name or title:
            raise _invalid("A post without media must not carry media fields or a document title.")
        return {"kind": "", "url": "", "sha256": "", "size": None, "name": "", "title": ""}
    if not MEDIA_URL_RE.fullmatch(url or ""):
        raise _invalid("Media must be an https://cdn.sanity.io/files/ address.")
    if not _SHA256_RE.fullmatch(sha256 or ""):
        raise _invalid("Media SHA-256 must be 64 lowercase hex characters.")
    if size is not None and not (isinstance(size, int) and 0 < size <= MAX_MEDIA_BYTES):
        raise _invalid(f"Media size must be between 1 and {MAX_MEDIA_BYTES} bytes.")
    if not _MEDIA_NAME_RE.fullmatch(name or "") or not (name or "").lower().endswith(_MEDIA_EXTENSIONS[kind]):
        raise _invalid(f"An {kind} needs a plain file name ending in {', '.join(_MEDIA_EXTENSIONS[kind])}.")
    if kind == "document":
        if not title:
            raise _invalid("A document needs its title.")
        _plain_text(title, "Document title", MAX_DOCUMENT_TITLE_CHARS, line_breaks=False)
    elif title:
        raise _invalid("Only a document has a title.")
    return {"kind": kind, "url": url, "sha256": sha256, "size": size, "name": name, "title": title}


def validate_row(row) -> dict:
    """Re-check a row written by Social Studio before anything is opened."""
    validate_operator(row.operator)
    commentary = validate_commentary(row.commentary)
    media = validate_media(
        kind=row.media_kind, url=row.media_url, sha256=row.media_sha256, size=row.media_bytes,
        name=row.media_name, title=row.document_title,
    )
    if not commentary and not media["kind"]:
        raise _invalid("A post needs text, media or both.")
    return media


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


def _now():
    from django.utils import timezone

    return timezone.now()


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _due(now: datetime):
    """Queued rows whose time has come; a row without a time is due at once."""
    from django.db.models import Q

    return Q(scheduled_for__isnull=True) | Q(scheduled_for__lte=now)


def validate_scheduled_for(value: datetime | None) -> datetime | None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise _invalid("--post-at must include a time zone, for example 2026-11-02T13:15:00Z.")
    return value


def enqueue_post(*, operator: str, external_ref: str, commentary: str = "", media_kind: str = "",
                 media_url: str = "", media_sha256: str = "", media_bytes: int | None = None,
                 media_name: str = "", document_title: str = "", scheduled_for: datetime | None = None):
    """Create one queued row, or return the identical existing row.

    Returns ``(row, created)``. A different payload under the same
    external_ref raises ``LinkedInPostConflictError`` and changes nothing.
    Social Studio normally inserts rows itself; this is for manual use and tests.
    """
    from django.db import IntegrityError, transaction

    from linkedin.exceptions import LinkedInPostConflictError
    from linkedin.models import LinkedInPost

    media = validate_media(kind=media_kind, url=media_url, sha256=media_sha256, size=media_bytes,
                           name=media_name, title=document_title)
    payload = {
        "operator": validate_operator(operator),
        "commentary": validate_commentary(commentary),
        "media_kind": media["kind"], "media_url": media["url"], "media_sha256": media["sha256"],
        "media_bytes": media["size"], "media_name": media["name"], "document_title": media["title"],
    }
    if not payload["commentary"] and not payload["media_kind"]:
        raise _invalid("A post needs text, media or both.")
    external_ref = validate_external_ref(external_ref)
    scheduled_for = validate_scheduled_for(scheduled_for)

    def existing():
        row = LinkedInPost.objects.get(external_ref=external_ref)
        if any(getattr(row, name) != value for name, value in payload.items()):
            raise LinkedInPostConflictError("external_ref already exists with a different payload; nothing changed.")
        return row, False

    if LinkedInPost.objects.filter(external_ref=external_ref).exists():
        return existing()
    try:
        with transaction.atomic():
            return LinkedInPost.objects.create(external_ref=external_ref, scheduled_for=scheduled_for, **payload), True
    except IntegrityError:
        # A concurrent enqueue of the same ref won; compare against it.
        return existing()


def _row(external_ref: str):
    from linkedin.exceptions import LinkedInPostStateError
    from linkedin.models import LinkedInPost

    try:
        return LinkedInPost.objects.get(external_ref=validate_external_ref(external_ref))
    except LinkedInPost.DoesNotExist:
        raise LinkedInPostStateError("No LinkedIn post has that external_ref.") from None


def is_stale_running(row, now: datetime | None = None) -> bool:
    """A running row past its child's bound can no longer be in progress."""
    if row.status != "running":
        return False
    if row.started_at is None:
        return True
    limit = timedelta(seconds=budget_seconds(row.commentary) + PROCESS_GRACE_SECONDS)
    return (now or _now()) - row.started_at > limit


def post_status(external_ref: str, now: datetime | None = None) -> dict:
    """Read-only status. A running row older than its bound reads as uncertain."""
    row = _row(external_ref)
    status = "uncertain" if is_stale_running(row, now) else row.status
    return {
        "external_ref": row.external_ref,
        "status": status,
        "scheduled_for": _iso(row.scheduled_for),
        "result_url": row.result_url or None,
        "error_type": row.error_type or None,
        "click_attempted_at": _iso(row.click_attempted_at),
        "completed_at": _iso(row.completed_at),
        "retry_count": row.retry_count,
    }


def cancel_post(external_ref: str) -> dict:
    """Only a queued row can become skipped; any other state is refused."""
    from linkedin.exceptions import LinkedInPostStateError
    from linkedin.models import LinkedInPost

    row = _row(external_ref)
    now = _now()
    updated = LinkedInPost.objects.filter(pk=row.pk, status=LinkedInPost.Status.QUEUED).update(
        status=LinkedInPost.Status.SKIPPED, completed_at=now, updated_at=now,
    )
    if updated != 1:
        row.refresh_from_db()
        raise LinkedInPostStateError(f"Only queued posts can be cancelled; status is {row.status}.")
    return {"external_ref": row.external_ref, "status": "skipped", "cancelled": True}


def retry_post(external_ref: str) -> dict:
    """Queue a failed row again, as the same row, after its cause was fixed.

    Only a ``failed`` row whose final click was never attempted (so nothing was
    posted) qualifies, at most MAX_POST_RETRIES times. Nothing retries on its
    own. The row keeps its reference, so Social Studio's dashboard follows it.
    """
    from django.db.models import F

    from linkedin.exceptions import LinkedInPostStateError
    from linkedin.models import LinkedInPost

    row = _row(external_ref)
    now = _now()
    updated = LinkedInPost.objects.filter(
        pk=row.pk, status=LinkedInPost.Status.FAILED, click_attempted_at__isnull=True,
        retry_count__lt=MAX_POST_RETRIES,
    ).update(
        status=LinkedInPost.Status.QUEUED, started_at=None, completed_at=None, error_type="",
        retry_count=F("retry_count") + 1, updated_at=now,
    )
    row.refresh_from_db()
    if updated != 1:
        if row.status == LinkedInPost.Status.FAILED and row.click_attempted_at is None:
            raise LinkedInPostStateError(
                f"This post was already retried {row.retry_count} times, the limit; it is not retried again."
            )
        raise LinkedInPostStateError(f"Only a failed post that never clicked can be retried; status is {row.status}.")
    return {
        "external_ref": row.external_ref, "status": row.status, "retried": True,
        "retry_count": row.retry_count, "retries_left": MAX_POST_RETRIES - row.retry_count,
    }


def has_queued_post(operator: str, now: datetime | None = None) -> bool:
    """Does this operator have a queued post whose time has come?"""
    from linkedin.models import LinkedInPost

    return LinkedInPost.objects.filter(_due(now or _now()), operator=operator, status=LinkedInPost.Status.QUEUED).exists()


def sweep_stale_running(operator: str, now: datetime | None = None) -> list[str]:
    """Mark this operator's interrupted rows uncertain. They are never retried."""
    from django.db import transaction

    from linkedin.models import LinkedInPost

    now = now or _now()
    swept = []
    with transaction.atomic():
        rows = list(
            LinkedInPost.objects.select_for_update()
            .filter(operator=operator, status=LinkedInPost.Status.RUNNING)
            .order_by("pk")
        )
        for row in rows:
            if row.click_attempted_at is None and not is_stale_running(row, now):
                continue
            updated = LinkedInPost.objects.filter(pk=row.pk, status=LinkedInPost.Status.RUNNING).update(
                status=LinkedInPost.Status.UNCERTAIN, error_type="PostInterruptedError",
                completed_at=now, updated_at=now,
            )
            if updated:
                swept.append(row.external_ref)
    for ref in swept:
        logger.warning("LinkedIn post %s was interrupted; marked uncertain and never retried", ref)
    return swept


def _repost_running(operator: str) -> bool:
    """A repost for this person is driving the same browser right now."""
    from linkedin.models import LinkedInRepost

    return LinkedInRepost.objects.filter(operator=operator, status=LinkedInRepost.Status.RUNNING).exists()


def claim_next(operator: str, now: datetime | None = None):
    """Atomically move this operator's earliest due queued row to running.

    Rows scheduled for later stay queued. Returns None while another of this
    operator's posts, or one of their reposts, is still running.
    """
    from django.db import transaction
    from django.db.models.functions import Coalesce

    from linkedin.models import LinkedInPost

    now = now or _now()
    with transaction.atomic():
        if LinkedInPost.objects.filter(operator=operator, status=LinkedInPost.Status.RUNNING).exists():
            return None
        if _repost_running(operator):
            return None
        row = (
            LinkedInPost.objects.select_for_update(skip_locked=True)
            .filter(_due(now), operator=operator, status=LinkedInPost.Status.QUEUED)
            .order_by(Coalesce("scheduled_for", "created_at"), "pk")
            .first()
        )
        if row is None:
            return None
        updated = LinkedInPost.objects.filter(_due(now), pk=row.pk, status=LinkedInPost.Status.QUEUED).update(
            status=LinkedInPost.Status.RUNNING, started_at=now, updated_at=now,
        )
        if updated != 1:
            return None
    row.refresh_from_db()
    return row


def mark_click_attempted(pk: int, now: datetime | None = None) -> None:
    """Persist the attempt immediately before the final click, or refuse to click."""
    from linkedin.exceptions import PostStateChangedError
    from linkedin.models import LinkedInPost

    now = now or _now()
    updated = LinkedInPost.objects.filter(
        pk=pk, status=LinkedInPost.Status.RUNNING, click_attempted_at__isnull=True,
    ).update(click_attempted_at=now, updated_at=now)
    if updated != 1:
        raise PostStateChangedError("Post row changed before the final click")


def record_sent(pk: int, result_url: str, now: datetime | None = None) -> str:
    """Only a verified permalink makes a row sent; anything else is uncertain."""
    from linkedin.models import LinkedInPost

    if not POST_URL_RE.fullmatch(result_url or ""):
        return record_failure(pk, error_type="PostUnverifiedError", uncertain=True, now=now)
    now = now or _now()
    LinkedInPost.objects.filter(pk=pk, status=LinkedInPost.Status.RUNNING).update(
        status=LinkedInPost.Status.SENT, result_url=result_url, error_type="", completed_at=now, updated_at=now,
    )
    return LinkedInPost.objects.values_list("status", flat=True).get(pk=pk)


def record_failure(pk: int, *, error_type: str, uncertain: bool, now: datetime | None = None) -> str:
    """Failed only when no final click was ever persisted; otherwise uncertain."""
    from django.db.models import Case, Value, When

    from linkedin.models import LinkedInPost

    now = now or _now()
    status = Case(
        When(click_attempted_at__isnull=False, then=Value(LinkedInPost.Status.UNCERTAIN)),
        default=Value(LinkedInPost.Status.UNCERTAIN if uncertain else LinkedInPost.Status.FAILED),
    )
    LinkedInPost.objects.filter(pk=pk, status=LinkedInPost.Status.RUNNING).update(
        status=status, error_type=safe_error_type(error_type), completed_at=now, updated_at=now,
    )
    return LinkedInPost.objects.values_list("status", flat=True).get(pk=pk)


# ---------------------------------------------------------------------------
# Worker identity, media, owned targets
# ---------------------------------------------------------------------------


def worker_operator(linkedin_username: str) -> str:
    """Resolve this checkout's exact sender; refuse stopped, inactive or ambiguous ones."""
    from linkedin.exceptions import LinkedInPostWorkerRefused
    from linkedin.models import LinkedInProfile
    from linkedin.operators import resolve_operator

    username = (linkedin_username or "").strip()
    if not username:
        raise LinkedInPostWorkerRefused("LINKEDIN_USERNAME is not configured.")
    operator = resolve_operator(username)
    if operator not in POST_OPERATORS:
        raise LinkedInPostWorkerRefused("This sender is not enabled for personal posts.")
    profiles = list(
        LinkedInProfile.objects.filter(linkedin_username__iexact=username)
        .order_by("pk").values_list("active", "stop_requested")[:2]
    )
    if len(profiles) != 1:
        raise LinkedInPostWorkerRefused(f"Local LinkedIn username matches {'multiple' if profiles else 'no'} profiles.")
    active, stop_requested = profiles[0]
    if stop_requested or not active:
        raise LinkedInPostWorkerRefused("Sender is stopped or inactive.")
    return operator


def post_directory(root: Path, linkedin_username: str) -> Path:
    username = (linkedin_username or "").strip()
    if not username:
        raise ValueError("Posts require an exact configured LinkedIn account")
    key = hashlib.sha256(username.lower().encode()).hexdigest()[:24]
    return Path(root) / "data" / "linkedin-posts" / key


class PostTargetReceipt(RepostTargetReceipt):
    """The post worker's own CDP target receipt, separate from the repost one."""

    def __init__(self, root: Path, linkedin_username: str, port: int):
        super().__init__(root, linkedin_username, port)
        self.path = post_directory(root, linkedin_username) / "targets.json"


def fetch_media(media: dict, directory: Path, *, opener=None) -> Path:
    """Download the approved file to a private path; keep it only if it matches exactly."""
    import urllib.request

    from linkedin.exceptions import PostMediaMismatchError, PostMediaUnavailableError

    if not MEDIA_URL_RE.fullmatch(media["url"]):
        raise PostMediaUnavailableError("Media is not a Sanity file address")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = directory / f"{media['sha256']}{Path(media['name']).suffix.lower()}"
    opener = opener or (lambda url: urllib.request.urlopen(url, timeout=MEDIA_DOWNLOAD_TIMEOUT_SECONDS))
    digest, size = hashlib.sha256(), 0
    try:
        with opener(media["url"]) as response, open(target, "wb") as out:
            os.chmod(target, 0o600)
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_MEDIA_BYTES:
                    raise PostMediaMismatchError("Media is larger than allowed")
                digest.update(chunk)
                out.write(chunk)
    except PostMediaMismatchError:
        target.unlink(missing_ok=True)
        raise
    except OSError as exc:
        target.unlink(missing_ok=True)
        raise PostMediaUnavailableError("Media download failed", error_type=type(exc).__name__) from None
    if digest.hexdigest() != media["sha256"] or (media["size"] is not None and size != media["size"]):
        target.unlink(missing_ok=True)
        raise PostMediaMismatchError("Downloaded media differs from the approved file")
    return target


def announce(row) -> None:
    """One Slack message (feed channel) for a finished row. Never changes its outcome."""
    try:
        from linkedin.notifications.slack import notify_post_outcome

        notify_post_outcome(
            operator=row.operator, status=row.status, result_url=row.result_url or None,
            error_type=row.error_type or None,
        )
    except Exception as exc:
        logger.warning("LinkedIn post %s Slack message failed: %s", row.external_ref, type(exc).__name__)


def run_once(*, linkedin_username: str, root: Path, port: int, attach=None, perform=None, page_opener=None,
             notify=None, media_fetcher=None) -> dict:
    """Run at most one queued post for this checkout's sender.

    The row is claimed only after the daemon browser is attached, so a browser
    that is not up yet leaves the row queued. Every outcome after the claim is
    terminal: sent (verified permalink), failed (no final click) or uncertain.
    Each terminal outcome, including interrupted rows swept to uncertain, gets
    one Slack message through ``notify`` (``announce`` by default).
    """
    from linkedin.exceptions import LinkedInPostValidationError, PostInvalidRowError, PostSendError, PostUncertainError
    from linkedin.models import LinkedInPost

    notify = notify or announce
    media_fetcher = media_fetcher or fetch_media
    if perform is None or attach is None or page_opener is None:
        from linkedin.actions import personal_post as actions

        attach = attach or actions.attached_browser
        perform = perform or actions.perform_post
        page_opener = page_opener or actions.owned_page

    operator = worker_operator(linkedin_username)
    receipt = PostTargetReceipt(root, linkedin_username, port)
    # Tabs left by a killed child belong to this feature only.
    receipt.close_all()
    summary = {"operator": operator, "swept": sweep_stale_running(operator), "claimed": None}
    for ref in summary["swept"]:
        notify(LinkedInPost.objects.get(external_ref=ref))
    if not has_queued_post(operator):
        return summary

    media_dir = post_directory(root, linkedin_username) / "media"
    with attach(port) as browser:
        row = claim_next(operator)
        if row is None:
            return summary
        summary["claimed"] = row.external_ref
        logger.warning("LinkedIn post %s claimed for %s", row.external_ref, operator)
        watchdog = RepostWatchdog(receipt, budget_seconds(row.commentary))
        watchdog.start()
        media_path = None
        try:
            try:
                media = validate_row(row)
            except LinkedInPostValidationError:
                raise PostInvalidRowError("Queued row failed validation") from None
            if media["kind"]:
                media_path = media_fetcher(media, media_dir)
            result_url = perform(
                lambda url: page_opener(
                    browser, url, on_target=watchdog.target_created, on_closed=watchdog.target_closed,
                ),
                commentary=row.commentary,
                media=({**media, "path": media_path} if media["kind"] else None),
                on_click_attempt=lambda: mark_click_attempted(row.pk),
            )
        except PostSendError as exc:
            error_type = safe_error_type(exc.error_type)
            status = record_failure(row.pk, error_type=error_type, uncertain=isinstance(exc, PostUncertainError))
            summary.update(status=status, error_type=error_type, result_url=None)
        except Exception as exc:
            # Unexpected: record conservatively, then crash. A persisted click
            # attempt always makes the row uncertain.
            record_failure(row.pk, error_type=type(exc).__name__, uncertain=False)
            notify(LinkedInPost.objects.get(pk=row.pk))
            raise
        else:
            status = record_sent(row.pk, result_url)
            sent = status == "sent"
            summary.update(
                status=status, error_type=None if sent else "PostUnverifiedError", result_url=result_url if sent else None,
            )
        finally:
            watchdog.stop()
            if media_path is not None:
                Path(media_path).unlink(missing_ok=True)
    logger.warning("LinkedIn post %s finished: %s %s", summary["claimed"], summary["status"], summary["error_type"] or "")
    notify(LinkedInPost.objects.get(pk=row.pk))
    return summary
