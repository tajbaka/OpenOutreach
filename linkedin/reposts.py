"""Queued LinkedIn reposts: strict validation, ledger transitions and one worker run.

Boundera Social Studio owns approval and scheduling. When the user schedules
an approved repost, its dashboard writes one queued ``LinkedInRepost`` row with
``scheduled_for`` and later reads the result back; the worker claims a row only
once that time has come. This module never retries: one attempt per row, and anything after the
final click is attempted is ``uncertain``. Module import stays stdlib-only so
the supervisor can read constants before Django setup. See
docs/linkedin-reposts.md.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import threading
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

REPOST_OPERATORS = ("Arian", "Chuka")
# Accepted --operator spellings; Eddy is Chuka's other name.
_OPERATOR_INPUTS = frozenset({"arian", "chuka", "eddy"})

MAX_COMMENTARY_CHARS = 3000
MAX_COMMENTARY_FILE_BYTES = 16_384
MAX_EXTERNAL_REF_CHARS = 100
POST_URL_RE = re.compile(
    r"https://www\.linkedin\.com/feed/update/urn:li:(activity|ugcPost|share):([0-9]{1,30})/"
)
_EXTERNAL_REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,99}")
_ERROR_TYPE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,79}")

# Commentary is typed with this randomized per-key delay, so the budget below
# bounds every row: fixed browser work plus the slowest possible typing.
COMMENTARY_TYPE_MIN_DELAY_MS = 80
COMMENTARY_TYPE_MAX_DELAY_MS = 250
BASE_BUDGET_SECONDS = 240
PROCESS_GRACE_SECONDS = 10
# Supervisor backstop for one child: the longest row budget, plus Django/CDP
# startup and the child's own cleanup.
SUPERVISOR_TIMEOUT_SECONDS = (
    BASE_BUDGET_SECONDS
    + math.ceil(MAX_COMMENTARY_CHARS * COMMENTARY_TYPE_MAX_DELAY_MS / 1000)
    + 3 * PROCESS_GRACE_SECONDS
)


def reposts_enabled() -> bool:
    return os.getenv("ENABLE_LINKEDIN_REPOSTS", "false").strip().lower() in {"1", "true", "yes", "on"}


def budget_seconds(commentary: str) -> int:
    """Wall-clock bound for one row, measured from its claim."""
    typing = math.ceil(len(commentary or "") * COMMENTARY_TYPE_MAX_DELAY_MS / 1000)
    return BASE_BUDGET_SECONDS + typing


def safe_error_type(value: str | None) -> str:
    """Class-name-shaped identifier only; never a raw message or page text."""
    value = (value or "").strip()
    return value if _ERROR_TYPE_RE.fullmatch(value) else "RepostError"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _invalid(message: str):
    from linkedin.exceptions import LinkedInRepostValidationError

    return LinkedInRepostValidationError(message)


def validate_operator(value: str | None) -> str:
    """Accept only Arian, Chuka or Eddy (case-insensitive); return the canonical handle."""
    from linkedin.operators import resolve_operator

    key = (value or "").strip().lower()
    if key not in _OPERATOR_INPUTS:
        raise _invalid("--operator must be Arian, Chuka or Eddy.")
    operator = resolve_operator(key)
    if operator not in REPOST_OPERATORS:
        raise _invalid("--operator must be Arian, Chuka or Eddy.")
    return operator


def validate_post_url(value: str | None) -> str:
    url = (value or "").strip()
    if not POST_URL_RE.fullmatch(url):
        raise _invalid(
            "--post-url must be exactly https://www.linkedin.com/feed/update/"
            "urn:li:(activity|ugcPost|share):<digits>/"
        )
    return url


def validate_external_ref(value: str | None) -> str:
    ref = value or ""
    if not _EXTERNAL_REF_RE.fullmatch(ref):
        raise _invalid(
            f"--external-ref must be 1-{MAX_EXTERNAL_REF_CHARS} characters of "
            "letters, digits, '.', '_', ':' or '-', starting with a letter or digit."
        )
    return ref


def validate_commentary(value: str) -> str:
    """Exact text to type. Empty means a plain repost; nothing is normalized."""
    text = value or ""
    if len(text) > MAX_COMMENTARY_CHARS:
        raise _invalid(f"Commentary must be at most {MAX_COMMENTARY_CHARS} characters.")
    if text != text.strip():
        raise _invalid("Commentary must not start or end with whitespace.")
    if "\ufeff" in text:
        raise _invalid("Commentary must not contain a byte-order mark.")
    # Only \n is typed as a line break (Shift+Enter). Tab would move focus out
    # of the editor and \r is ambiguous, so other control characters are refused.
    if any(char != "\n" and unicodedata.category(char) == "Cc" for char in text):
        raise _invalid("Commentary may contain line breaks (\\n) but no other control characters.")
    return text


def read_commentary_file(path: str | Path) -> str:
    path = Path(path)
    try:
        if path.stat().st_size > MAX_COMMENTARY_FILE_BYTES:
            raise _invalid("Commentary file is too large.")
        raw = path.read_bytes()
    except OSError:
        raise _invalid("Commentary file could not be read.") from None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise _invalid("Commentary file must be UTF-8.") from None
    return validate_commentary(text)


def urn_from_post_url(url: str) -> str:
    match = POST_URL_RE.fullmatch(url or "")
    if not match:
        raise _invalid("Not a canonical LinkedIn post URL.")
    return f"urn:li:{match.group(1)}:{match.group(2)}"


_URN_TYPES = {"activity": "activity", "ugcpost": "ugcPost", "share": "share"}
_URN_RE = re.compile(r"urn(?::|%3A)li(?::|%3A)(activity|ugcPost|share)(?::|%3A)([0-9]{1,30})", re.IGNORECASE)


def normalize_urn(value: str | None) -> str | None:
    match = _URN_RE.fullmatch((value or "").strip())
    if not match:
        return None
    return f"urn:li:{_URN_TYPES[match.group(1).lower()]}:{match.group(2)}"


def post_url_for_urn(urn: str) -> str:
    normalized = normalize_urn(urn)
    if normalized is None:
        raise _invalid("Not a LinkedIn post URN.")
    return f"https://www.linkedin.com/feed/update/{normalized}/"


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
        raise _invalid("--post-at must include a time zone, for example 2026-10-30T13:15:00Z.")
    return value


def enqueue_repost(*, operator: str, post_url: str, external_ref: str, commentary: str = "",
                   scheduled_for: datetime | None = None):
    """Create one queued row, or return the identical existing row.

    Returns ``(row, created)``. A different payload under the same
    external_ref raises ``LinkedInRepostConflictError`` and changes nothing.
    ``scheduled_for`` (not before this time) is set only when the row is created.
    """
    from django.db import IntegrityError, transaction

    from linkedin.exceptions import LinkedInRepostConflictError
    from linkedin.models import LinkedInRepost

    payload = {
        "operator": validate_operator(operator),
        "post_url": validate_post_url(post_url),
        "commentary": validate_commentary(commentary),
    }
    external_ref = validate_external_ref(external_ref)
    scheduled_for = validate_scheduled_for(scheduled_for)

    def existing():
        row = LinkedInRepost.objects.get(external_ref=external_ref)
        if any(getattr(row, name) != value for name, value in payload.items()):
            raise LinkedInRepostConflictError(
                "external_ref already exists with a different payload; nothing changed."
            )
        return row, False

    if LinkedInRepost.objects.filter(external_ref=external_ref).exists():
        return existing()
    try:
        with transaction.atomic():
            return LinkedInRepost.objects.create(external_ref=external_ref, scheduled_for=scheduled_for, **payload), True
    except IntegrityError:
        # A concurrent enqueue of the same ref won; compare against it.
        return existing()


def _row(external_ref: str):
    from linkedin.exceptions import LinkedInRepostStateError
    from linkedin.models import LinkedInRepost

    try:
        return LinkedInRepost.objects.get(external_ref=validate_external_ref(external_ref))
    except LinkedInRepost.DoesNotExist:
        raise LinkedInRepostStateError("No LinkedIn repost has that external_ref.") from None


def is_stale_running(row, now: datetime | None = None) -> bool:
    """A running row past its child's bound can no longer be in progress."""
    if row.status != "running":
        return False
    if row.started_at is None:
        return True
    limit = timedelta(seconds=budget_seconds(row.commentary) + PROCESS_GRACE_SECONDS)
    return (now or _now()) - row.started_at > limit


def repost_status(external_ref: str, now: datetime | None = None) -> dict:
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
    }


def cancel_repost(external_ref: str) -> dict:
    """Only a queued row can become skipped; any other state is refused."""
    from linkedin.exceptions import LinkedInRepostStateError
    from linkedin.models import LinkedInRepost

    row = _row(external_ref)
    now = _now()
    updated = LinkedInRepost.objects.filter(pk=row.pk, status=LinkedInRepost.Status.QUEUED).update(
        status=LinkedInRepost.Status.SKIPPED, completed_at=now, updated_at=now,
    )
    if updated != 1:
        row.refresh_from_db()
        raise LinkedInRepostStateError(f"Only queued reposts can be cancelled; status is {row.status}.")
    return {"external_ref": row.external_ref, "status": "skipped", "cancelled": True}


def has_queued_repost(operator: str, now: datetime | None = None) -> bool:
    """Does this operator have a queued row whose time has come?"""
    from linkedin.models import LinkedInRepost

    return LinkedInRepost.objects.filter(_due(now or _now()), operator=operator, status=LinkedInRepost.Status.QUEUED).exists()


def sweep_stale_running(operator: str, now: datetime | None = None) -> list[str]:
    """Mark this operator's interrupted rows uncertain. They are never retried.

    The supervisor runs one repost child per sender at a time, so a running
    row with an attempted click belongs to a previous child that died before
    recording its result.
    """
    from django.db import transaction

    from linkedin.models import LinkedInRepost

    now = now or _now()
    swept = []
    with transaction.atomic():
        rows = list(
            LinkedInRepost.objects.select_for_update()
            .filter(operator=operator, status=LinkedInRepost.Status.RUNNING)
            .order_by("pk")
        )
        for row in rows:
            if row.click_attempted_at is None and not is_stale_running(row, now):
                continue
            updated = LinkedInRepost.objects.filter(pk=row.pk, status=LinkedInRepost.Status.RUNNING).update(
                status=LinkedInRepost.Status.UNCERTAIN, error_type="RepostInterruptedError",
                completed_at=now, updated_at=now,
            )
            if updated:
                swept.append(row.external_ref)
    for ref in swept:
        logger.warning("LinkedIn repost %s was interrupted; marked uncertain and never retried", ref)
    return swept


def claim_next(operator: str, now: datetime | None = None):
    """Atomically move this operator's earliest due queued row to running.

    Rows scheduled for later stay queued. Returns None while another of this
    operator's rows is still running.
    """
    from django.db import transaction
    from django.db.models.functions import Coalesce

    from linkedin.models import LinkedInRepost

    now = now or _now()
    with transaction.atomic():
        if LinkedInRepost.objects.filter(operator=operator, status=LinkedInRepost.Status.RUNNING).exists():
            return None
        row = (
            LinkedInRepost.objects.select_for_update(skip_locked=True)
            .filter(_due(now), operator=operator, status=LinkedInRepost.Status.QUEUED)
            .order_by(Coalesce("scheduled_for", "created_at"), "pk")
            .first()
        )
        if row is None:
            return None
        updated = LinkedInRepost.objects.filter(_due(now), pk=row.pk, status=LinkedInRepost.Status.QUEUED).update(
            status=LinkedInRepost.Status.RUNNING, started_at=now, updated_at=now,
        )
        if updated != 1:
            return None
    row.refresh_from_db()
    return row


def mark_click_attempted(pk: int, now: datetime | None = None) -> None:
    """Persist the attempt immediately before the final click, or refuse to click."""
    from linkedin.exceptions import RepostStateChangedError
    from linkedin.models import LinkedInRepost

    now = now or _now()
    updated = LinkedInRepost.objects.filter(
        pk=pk, status=LinkedInRepost.Status.RUNNING, click_attempted_at__isnull=True,
    ).update(click_attempted_at=now, updated_at=now)
    if updated != 1:
        raise RepostStateChangedError("Repost row changed before the final click")


def record_sent(pk: int, result_url: str, now: datetime | None = None) -> str:
    """Only a verified permalink makes a row sent; anything else is uncertain."""
    from linkedin.models import LinkedInRepost

    if not POST_URL_RE.fullmatch(result_url or ""):
        return record_failure(pk, error_type="RepostUnverifiedError", uncertain=True, now=now)
    now = now or _now()
    LinkedInRepost.objects.filter(pk=pk, status=LinkedInRepost.Status.RUNNING).update(
        status=LinkedInRepost.Status.SENT, result_url=result_url, error_type="",
        completed_at=now, updated_at=now,
    )
    return LinkedInRepost.objects.values_list("status", flat=True).get(pk=pk)


def record_failure(pk: int, *, error_type: str, uncertain: bool, now: datetime | None = None) -> str:
    """Failed only when no final click was ever persisted; otherwise uncertain."""
    from django.db.models import Case, Value, When

    from linkedin.models import LinkedInRepost

    now = now or _now()
    status = Case(
        When(click_attempted_at__isnull=False, then=Value(LinkedInRepost.Status.UNCERTAIN)),
        default=Value(LinkedInRepost.Status.UNCERTAIN if uncertain else LinkedInRepost.Status.FAILED),
    )
    LinkedInRepost.objects.filter(pk=pk, status=LinkedInRepost.Status.RUNNING).update(
        status=status, error_type=safe_error_type(error_type), completed_at=now, updated_at=now,
    )
    return LinkedInRepost.objects.values_list("status", flat=True).get(pk=pk)


# ---------------------------------------------------------------------------
# Worker identity, owned-target receipts and watchdog
# ---------------------------------------------------------------------------


def worker_operator(linkedin_username: str) -> str:
    """Resolve this checkout's exact sender; refuse stopped, inactive or ambiguous ones.

    Uses the exact single-profile lookup of ``read_sender_stop``: no operator
    alias, first-active or Django-username fallback.
    """
    from linkedin.exceptions import LinkedInRepostWorkerRefused
    from linkedin.models import LinkedInProfile
    from linkedin.operators import resolve_operator

    username = (linkedin_username or "").strip()
    if not username:
        raise LinkedInRepostWorkerRefused("LINKEDIN_USERNAME is not configured.")
    operator = resolve_operator(username)
    if operator not in REPOST_OPERATORS:
        raise LinkedInRepostWorkerRefused("This sender is not enabled for reposts.")
    profiles = list(
        LinkedInProfile.objects.filter(linkedin_username__iexact=username)
        .order_by("pk").values_list("active", "stop_requested")[:2]
    )
    if len(profiles) != 1:
        raise LinkedInRepostWorkerRefused(
            f"Local LinkedIn username matches {'multiple' if profiles else 'no'} profiles."
        )
    active, stop_requested = profiles[0]
    if stop_requested or not active:
        raise LinkedInRepostWorkerRefused("Sender is stopped or inactive.")
    return operator


def repost_directory(root: Path, linkedin_username: str) -> Path:
    username = (linkedin_username or "").strip()
    if not username:
        raise ValueError("Reposts require an exact configured LinkedIn account")
    key = hashlib.sha256(username.lower().encode()).hexdigest()[:24]
    return Path(root) / "data" / "linkedin-reposts" / key


class RepostTargetReceipt:
    """Local record of the repost-owned CDP targets, so a killed child's tab
    can still be closed by its supervisor. Only these exact targets are closed."""

    def __init__(self, root: Path, linkedin_username: str, port: int):
        self.path = repost_directory(root, linkedin_username) / "targets.json"
        self.port = int(port)
        self._ids: list[str] = []
        self._lock = threading.Lock()

    def _write(self) -> None:
        from linkedin.feed_sampling import write_json

        if self._ids:
            write_json(self.path, {"cdp_port": self.port, "target_ids": list(self._ids)})
        else:
            self.path.unlink(missing_ok=True)

    def add(self, target_id: str) -> None:
        with self._lock:
            self._ids.append(target_id)
            self._write()

    def remove(self, target_id: str) -> None:
        with self._lock:
            if target_id in self._ids:
                self._ids.remove(target_id)
            self._write()

    def close_all(self) -> int:
        from linkedin.feed_sampling import close_sample_target

        with self._lock:
            port, ids = self.port, list(self._ids)
            try:
                state = json.loads(self.path.read_text(encoding="utf-8"))
                port = int(state.get("cdp_port", port))
                ids += [value for value in state.get("target_ids", []) if isinstance(value, str)]
            except FileNotFoundError:
                pass
            except (OSError, ValueError, TypeError, AttributeError) as exc:
                logger.error("Repost target receipt needs review: %s", type(exc).__name__)
            closed = sum(close_sample_target(port, target_id) for target_id in dict.fromkeys(ids))
            self._ids.clear()
            self.path.unlink(missing_ok=True)
            return closed


class RepostWatchdog:
    """Independent wall-clock bound for one row, including hung browser calls.

    On expiry it closes only the repost-owned targets and exits the child; the
    row stays running and the next worker run marks it uncertain.
    """

    def __init__(self, receipt: RepostTargetReceipt, seconds: float):
        self.receipt = receipt
        self.timer = threading.Timer(seconds, self.expire)
        self.timer.daemon = True

    def start(self) -> None:
        self.timer.start()

    def target_created(self, target_id: str) -> None:
        self.receipt.add(target_id)

    def target_closed(self, target_id: str) -> None:
        self.receipt.remove(target_id)

    def expire(self) -> None:
        try:
            self.receipt.close_all()
        finally:
            os._exit(124)

    def stop(self) -> None:
        self.timer.cancel()


def announce(row) -> None:
    """One Slack message (feed channel) for a finished row. Never changes its outcome."""
    try:
        from linkedin.notifications.slack import notify_repost_outcome

        notify_repost_outcome(
            operator=row.operator, status=row.status, post_url=row.post_url,
            result_url=row.result_url or None, error_type=row.error_type or None,
        )
    except Exception as exc:
        logger.warning("LinkedIn repost %s Slack message failed: %s", row.external_ref, type(exc).__name__)


def run_once(*, linkedin_username: str, root: Path, port: int, attach=None, perform=None, page_opener=None,
             notify=None) -> dict:
    """Run at most one queued row for this checkout's sender.

    The row is claimed only after the daemon browser is attached, so a browser
    that is not up yet leaves the row queued. Every outcome after the claim is
    terminal: sent (verified permalink), failed (no final click) or uncertain.
    Each terminal outcome, including interrupted rows swept to uncertain, gets
    one Slack message through ``notify`` (``announce`` by default).
    """
    from linkedin.exceptions import RepostSendError, RepostUncertainError
    from linkedin.models import LinkedInRepost

    notify = notify or announce
    if perform is None or attach is None or page_opener is None:
        from linkedin.actions import repost as actions

        attach = attach or actions.attached_browser
        perform = perform or actions.perform_repost
        page_opener = page_opener or actions.owned_page

    operator = worker_operator(linkedin_username)
    receipt = RepostTargetReceipt(root, linkedin_username, port)
    # Tabs left by a killed child belong to this feature only.
    receipt.close_all()
    summary = {"operator": operator, "swept": sweep_stale_running(operator), "claimed": None}
    for ref in summary["swept"]:
        notify(LinkedInRepost.objects.get(external_ref=ref))
    if not has_queued_repost(operator):
        return summary

    with attach(port) as browser:
        row = claim_next(operator)
        if row is None:
            return summary
        summary["claimed"] = row.external_ref
        logger.warning("LinkedIn repost %s claimed for %s", row.external_ref, operator)
        watchdog = RepostWatchdog(receipt, budget_seconds(row.commentary))
        watchdog.start()
        try:
            result_url = perform(
                lambda url: page_opener(
                    browser, url, on_target=watchdog.target_created, on_closed=watchdog.target_closed,
                ),
                post_url=row.post_url,
                commentary=row.commentary,
                on_click_attempt=lambda: mark_click_attempted(row.pk),
            )
        except RepostSendError as exc:
            error_type = safe_error_type(exc.error_type)
            status = record_failure(
                row.pk, error_type=error_type, uncertain=isinstance(exc, RepostUncertainError),
            )
            summary.update(status=status, error_type=error_type, result_url=None)
        except Exception as exc:
            # Unexpected: record conservatively, then crash. A persisted click
            # attempt always makes the row uncertain.
            record_failure(row.pk, error_type=type(exc).__name__, uncertain=False)
            notify(LinkedInRepost.objects.get(pk=row.pk))
            raise
        else:
            status = record_sent(row.pk, result_url)
            sent = status == "sent"
            summary.update(
                status=status, error_type=None if sent else "RepostUnverifiedError",
                result_url=result_url if sent else None,
            )
        finally:
            watchdog.stop()
    logger.warning(
        "LinkedIn repost %s finished: %s %s", summary["claimed"], summary["status"], summary["error_type"] or "",
    )
    notify(LinkedInRepost.objects.get(pk=row.pk))
    return summary
