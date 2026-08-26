"""Conservative Gmail-discovery to email-first Lead reconciliation.

The Gmail context sync returns exact external participants only after seeing a
human inbound message and an outbound message in the same mailbox-scoped
thread.  This module is the deliberately narrow write bridge for those
structured candidates.  It creates only ``Lead`` rows: it never persists Gmail
messages, creates outbound work, or sends anything.

Dry-run and apply share the exact transaction path.  Dry-run inserts are rolled
back, while apply commits the whole batch atomically.  Candidate outcomes are
identified by input position and sanitized issue codes so routine report
logging does not disclose an email address or display name.
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone as datetime_timezone
from typing import Any

from django.db import transaction
from django.utils import timezone

from crm.models import Lead


__all__ = (
    "EmailFirstLeadOutcome",
    "EmailFirstLeadReport",
    "apply_email_first_leads",
    "dry_run_email_first_leads",
    "reconcile_email_first_leads",
)


MAX_CANDIDATE_AGE_DAYS = 120

PUBLIC_EMAIL_DOMAINS = frozenset({
    "aol.com",
    "fastmail.com",
    "gmail.com",
    "googlemail.com",
    "hey.com",
    "hotmail.com",
    "icloud.com",
    "live.com",
    "mail.com",
    "me.com",
    "msn.com",
    "outlook.com",
    "pm.me",
    "proton.me",
    "protonmail.com",
    "yahoo.com",
    "ymail.com",
})

INTERNAL_BOUNDERA_DOMAINS = frozenset({
    "boundera.ai",
    "boundera.com",
    "getboundera.ai",
    "getboundera.com",
})

_MAILBOX_ACCOUNT_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
_LOCAL_PART_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+$")
_DOMAIN_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")
_SPACE_RE = re.compile(r"\s+")

_AUTOMATED_LOCAL_PARTS = frozenset({
    "alert",
    "alerts",
    "autoresponder",
    "bounce",
    "daemon",
    "donotreply",
    "mailerdaemon",
    "newsletter",
    "newsletters",
    "notification",
    "notifications",
    "noreply",
    "postmaster",
    "updates",
})
_ROLE_LOCAL_PARTS = frozenset({
    "admin",
    "billing",
    "contact",
    "hello",
    "info",
    "marketing",
    "sales",
    "security",
    "support",
    "team",
})

# A small explicit list avoids pretending to implement the full public suffix
# list while still deriving the ordinary company identity for common ccTLDs.
_COMMON_TWO_LEVEL_SUFFIXES = frozenset({
    "ac.uk",
    "co.in",
    "co.jp",
    "co.nz",
    "co.uk",
    "com.au",
    "com.br",
    "com.mx",
    "com.sg",
    "org.uk",
})

_STATUS_CREATED = "created"
_STATUS_EXISTING = "existing"
_STATUS_REVIEW_ONLY = "review_only"
_STATUS_REJECTED = "rejected"
_COUNT_KEYS = (
    _STATUS_CREATED,
    _STATUS_EXISTING,
    _STATUS_REVIEW_ONLY,
    _STATUS_REJECTED,
)


@dataclass(frozen=True)
class EmailFirstLeadOutcome:
    """One unique email identity's sanitized reconciliation result."""

    input_indexes: tuple[int, ...]
    status: str
    issue_code: str
    lead_id: int | None = None
    derived_account_key: str = ""


@dataclass
class EmailFirstLeadReport:
    """Deterministic, log-safe summary of one reconciliation batch."""

    applied: bool
    evaluated_at: datetime
    input_candidates: int
    outcomes: list[EmailFirstLeadOutcome] = field(default_factory=list)
    created_lead_ids: tuple[int, ...] = ()

    def counts(self) -> dict[str, int]:
        """Return only aggregate categories; never emails or names."""
        observed = Counter(outcome.status for outcome in self.outcomes)
        return {key: observed.get(key, 0) for key in _COUNT_KEYS}

    def issue_counts(self) -> dict[str, int]:
        """Return deterministic sanitized issue-code aggregates."""
        observed = Counter(outcome.issue_code for outcome in self.outcomes)
        return dict(sorted(observed.items()))


@dataclass(frozen=True)
class _PreparedCandidate:
    input_index: int
    mailbox_account_key: str
    email: str
    display_name: str
    domain: str
    last_inbound_at: datetime
    latest_thread_id: str
    thread_count: int
    eligibility_issue: str
    derived_account_key: str
    company_name: str

    def duplicate_signature(self) -> tuple[Any, ...]:
        return (
            self.mailbox_account_key,
            self.email,
            self.display_name.casefold(),
            self.domain,
            self.last_inbound_at,
            self.latest_thread_id,
            self.thread_count,
            self.eligibility_issue,
            self.derived_account_key,
            self.company_name.casefold(),
        )


@dataclass(frozen=True)
class _PreparationFailure:
    input_index: int
    issue_code: str
    normalized_email: str = ""


def dry_run_email_first_leads(
    candidates: Iterable[Mapping[str, object]],
    *,
    evaluated_at: datetime | None = None,
    max_age_days: int = MAX_CANDIDATE_AGE_DAYS,
) -> EmailFirstLeadReport:
    """Exercise the exact insertion path and roll every database write back."""
    return reconcile_email_first_leads(
        candidates,
        apply=False,
        evaluated_at=evaluated_at,
        max_age_days=max_age_days,
    )


def apply_email_first_leads(
    candidates: Iterable[Mapping[str, object]],
    *,
    evaluated_at: datetime | None = None,
    max_age_days: int = MAX_CANDIDATE_AGE_DAYS,
) -> EmailFirstLeadReport:
    """Atomically create only candidates that pass every strict gate."""
    return reconcile_email_first_leads(
        candidates,
        apply=True,
        evaluated_at=evaluated_at,
        max_age_days=max_age_days,
    )


def reconcile_email_first_leads(
    candidates: Iterable[Mapping[str, object]],
    *,
    apply: bool = False,
    evaluated_at: datetime | None = None,
    max_age_days: int = MAX_CANDIDATE_AGE_DAYS,
) -> EmailFirstLeadReport:
    """Reconcile structured Gmail discovery candidates into ``Lead`` rows.

    The candidate contract is the shape returned by
    ``GmailContextSyncResult.unmapped_external_participants``.  Presence in
    that collection is the upstream bidirectional-human assertion; this layer
    independently requires its exact mailbox-scoped thread proof, recency,
    business-domain identity, and an unambiguous case-insensitive email.
    """
    rows = tuple(candidates)
    observed_at = evaluated_at or timezone.now()
    if timezone.is_naive(observed_at):
        raise ValueError("evaluated_at must be timezone-aware")
    observed_at = observed_at.astimezone(datetime_timezone.utc)
    if isinstance(max_age_days, bool) or not isinstance(max_age_days, int):
        raise ValueError("max_age_days must be a positive integer")
    if max_age_days <= 0:
        raise ValueError("max_age_days must be a positive integer")

    prepared, initial_outcomes = _prepare_batch(
        rows,
        observed_at=observed_at,
        max_age_days=max_age_days,
    )
    report = EmailFirstLeadReport(
        applied=apply,
        evaluated_at=observed_at,
        input_candidates=len(rows),
        outcomes=list(initial_outcomes),
    )
    committed_created_ids: list[int] = []

    with transaction.atomic():
        for candidate, input_indexes in prepared:
            outcome = _reconcile_candidate(
                candidate,
                input_indexes=input_indexes,
                apply=apply,
            )
            report.outcomes.append(outcome)
            if apply and outcome.status == _STATUS_CREATED and outcome.lead_id:
                committed_created_ids.append(outcome.lead_id)

        # The same rows were created and validated in dry-run.  Marking this
        # atomic block rollback-only gives exact database-path behavior without
        # leaking candidate Leads into later Gmail or Sheet workflows.
        if not apply:
            transaction.set_rollback(True)

    report.outcomes.sort(key=lambda outcome: outcome.input_indexes)
    report.created_lead_ids = tuple(committed_created_ids) if apply else ()
    return report


def _prepare_batch(
    rows: tuple[object, ...],
    *,
    observed_at: datetime,
    max_age_days: int,
) -> tuple[
    list[tuple[_PreparedCandidate, tuple[int, ...]]],
    list[EmailFirstLeadOutcome],
]:
    by_email: dict[str, list[_PreparedCandidate | _PreparationFailure]] = defaultdict(list)
    ungrouped_failures: list[_PreparationFailure] = []

    for index, raw in enumerate(rows):
        result = _prepare_candidate(
            raw,
            input_index=index,
            observed_at=observed_at,
            max_age_days=max_age_days,
        )
        if isinstance(result, _PreparationFailure):
            if result.normalized_email:
                by_email[result.normalized_email].append(result)
            else:
                ungrouped_failures.append(result)
        else:
            by_email[result.email].append(result)

    outcomes = [
        EmailFirstLeadOutcome(
            input_indexes=(failure.input_index,),
            status=_STATUS_REJECTED,
            issue_code=failure.issue_code,
        )
        for failure in ungrouped_failures
    ]
    prepared_groups: list[tuple[_PreparedCandidate, tuple[int, ...]]] = []

    for normalized_email in sorted(by_email):
        group = by_email[normalized_email]
        indexes = tuple(sorted(item.input_index for item in group))
        valid = [item for item in group if isinstance(item, _PreparedCandidate)]
        if len(valid) != len(group):
            outcomes.append(EmailFirstLeadOutcome(
                input_indexes=indexes,
                status=_STATUS_REJECTED,
                issue_code="conflicting_duplicate_candidate"
                if len(group) > 1
                else group[0].issue_code,
            ))
            continue

        first = valid[0]
        signatures = {item.duplicate_signature() for item in valid}
        if len(signatures) > 1:
            outcomes.append(EmailFirstLeadOutcome(
                input_indexes=indexes,
                status=_STATUS_REJECTED,
                issue_code="conflicting_duplicate_candidate",
                derived_account_key=first.derived_account_key,
            ))
            continue

        # Byte-for-byte equivalent repeated input is one email identity.  The
        # full input-index tuple keeps that collapse reviewable without logging
        # the address itself.
        prepared_groups.append((first, indexes))

    prepared_groups.sort(key=lambda item: item[0].email)
    return prepared_groups, outcomes


def _prepare_candidate(
    raw: object,
    *,
    input_index: int,
    observed_at: datetime,
    max_age_days: int,
) -> _PreparedCandidate | _PreparationFailure:
    if not isinstance(raw, Mapping):
        return _PreparationFailure(input_index, "malformed_candidate")

    normalized_email = _normalize_email(raw.get("email"))
    if not normalized_email:
        return _PreparationFailure(input_index, "invalid_email")

    mailbox_account_key = raw.get("account_key")
    if not isinstance(mailbox_account_key, str):
        return _PreparationFailure(
            input_index,
            "invalid_mailbox_account_key",
            normalized_email,
        )
    mailbox_account_key = mailbox_account_key.strip()
    if not _MAILBOX_ACCOUNT_KEY_RE.fullmatch(mailbox_account_key):
        return _PreparationFailure(
            input_index,
            "invalid_mailbox_account_key",
            normalized_email,
        )

    claimed_domain = raw.get("domain")
    if not isinstance(claimed_domain, str):
        return _PreparationFailure(
            input_index,
            "invalid_domain",
            normalized_email,
        )
    domain = _normalize_domain(claimed_domain)
    email_domain = normalized_email.rsplit("@", 1)[1]
    if not domain:
        return _PreparationFailure(
            input_index,
            "invalid_domain",
            normalized_email,
        )
    if domain != email_domain:
        return _PreparationFailure(
            input_index,
            "email_domain_mismatch",
            normalized_email,
        )

    if _is_internal_boundera_domain(domain):
        return _PreparationFailure(
            input_index,
            "internal_boundera_domain",
            normalized_email,
        )

    local_part = normalized_email.rsplit("@", 1)[0]
    local_class = _local_part_class(local_part)
    if local_class:
        return _PreparationFailure(input_index, local_class, normalized_email)

    last_inbound_at = _parse_aware_datetime(raw.get("last_inbound_at"))
    if last_inbound_at is None:
        return _PreparationFailure(
            input_index,
            "invalid_last_inbound_at",
            normalized_email,
        )
    if last_inbound_at > observed_at:
        return _PreparationFailure(
            input_index,
            "future_last_inbound_at",
            normalized_email,
        )

    latest_thread_id = raw.get("latest_thread_id")
    if not isinstance(latest_thread_id, str):
        return _PreparationFailure(
            input_index,
            "invalid_thread_identity",
            normalized_email,
        )
    latest_thread_id = latest_thread_id.strip()
    thread_prefix = f"{mailbox_account_key}:"
    if (
        not latest_thread_id.startswith(thread_prefix)
        or not latest_thread_id[len(thread_prefix):]
        or len(latest_thread_id) > 200
        or any(character.isspace() for character in latest_thread_id)
        or _CONTROL_RE.search(latest_thread_id)
    ):
        return _PreparationFailure(
            input_index,
            "invalid_thread_identity",
            normalized_email,
        )

    thread_count = raw.get("thread_count")
    if (
        isinstance(thread_count, bool)
        or not isinstance(thread_count, int)
        or thread_count < 1
    ):
        return _PreparationFailure(
            input_index,
            "invalid_thread_count",
            normalized_email,
        )

    display_name_value = raw.get("display_name", "")
    if display_name_value is None:
        display_name_value = ""
    if not isinstance(display_name_value, str):
        return _PreparationFailure(
            input_index,
            "invalid_display_name",
            normalized_email,
        )
    display_name = _clean_display_name(display_name_value)

    registrable_domain = _registrable_domain(domain)
    derived_account_key = f"domain:{registrable_domain}"
    company_name = _company_name_from_domain(registrable_domain)
    eligibility_issue = ""
    if domain in PUBLIC_EMAIL_DOMAINS:
        eligibility_issue = "public_mailbox_domain"
    elif last_inbound_at < observed_at - timedelta(days=max_age_days):
        eligibility_issue = "stale_candidate"

    return _PreparedCandidate(
        input_index=input_index,
        mailbox_account_key=mailbox_account_key,
        email=normalized_email,
        display_name=display_name,
        domain=domain,
        last_inbound_at=last_inbound_at,
        latest_thread_id=latest_thread_id,
        thread_count=thread_count,
        eligibility_issue=eligibility_issue,
        derived_account_key=derived_account_key,
        company_name=company_name,
    )


def _reconcile_candidate(
    candidate: _PreparedCandidate,
    *,
    input_indexes: tuple[int, ...],
    apply: bool,
) -> EmailFirstLeadOutcome:
    existing = list(
        Lead.objects.select_for_update()
        .filter(email__iexact=candidate.email)
        .order_by("id")
    )
    if len(existing) > 1:
        return EmailFirstLeadOutcome(
            input_indexes=input_indexes,
            status=_STATUS_REJECTED,
            issue_code="ambiguous_existing_email",
            derived_account_key=candidate.derived_account_key,
        )
    if len(existing) == 1:
        return EmailFirstLeadOutcome(
            input_indexes=input_indexes,
            status=_STATUS_EXISTING,
            issue_code="existing_email_identity",
            lead_id=existing[0].id,
            derived_account_key=candidate.derived_account_key,
        )
    if candidate.eligibility_issue:
        return EmailFirstLeadOutcome(
            input_indexes=input_indexes,
            status=_STATUS_REVIEW_ONLY,
            issue_code=candidate.eligibility_issue,
            derived_account_key=candidate.derived_account_key,
        )

    first_name, last_name = _person_name(candidate.display_name)
    lead = Lead.objects.create(
        first_name=first_name,
        last_name=last_name,
        company_name=candidate.company_name,
        linkedin_url="",
        email=candidate.email,
    )
    return EmailFirstLeadOutcome(
        input_indexes=input_indexes,
        status=_STATUS_CREATED,
        issue_code="email_first_lead_created",
        lead_id=lead.id if apply else None,
        derived_account_key=candidate.derived_account_key,
    )


def _normalize_email(value: object) -> str:
    if not isinstance(value, str):
        return ""
    candidate = value.strip().casefold()
    if not candidate or len(candidate) > 200:
        return ""
    if any(character.isspace() for character in candidate):
        return ""
    if candidate.count("@") != 1:
        return ""
    local_part, domain_value = candidate.split("@", 1)
    if (
        not local_part
        or len(local_part) > 64
        or not _LOCAL_PART_RE.fullmatch(local_part)
        or local_part.startswith(".")
        or local_part.endswith(".")
        or ".." in local_part
    ):
        return ""
    domain = _normalize_domain(domain_value)
    if not domain:
        return ""
    return f"{local_part}@{domain}"


def _normalize_domain(value: str) -> str:
    candidate = value.strip().casefold().rstrip(".")
    if not candidate or len(candidate) > 253 or "." not in candidate:
        return ""
    try:
        candidate = candidate.encode("idna").decode("ascii")
    except UnicodeError:
        return ""
    labels = candidate.split(".")
    if any(not _DOMAIN_LABEL_RE.fullmatch(label) for label in labels):
        return ""
    if labels[-1].isdigit() or len(labels[-1]) < 2:
        return ""
    return candidate


def _is_internal_boundera_domain(domain: str) -> bool:
    return any(
        domain == internal or domain.endswith(f".{internal}")
        for internal in INTERNAL_BOUNDERA_DOMAINS
    )


def _local_part_class(local_part: str) -> str:
    base = local_part.split("+", 1)[0].casefold()
    collapsed = re.sub(r"[._-]+", "", base)
    if collapsed in _AUTOMATED_LOCAL_PARTS:
        return "automated_local_part"
    if collapsed in _ROLE_LOCAL_PARTS:
        return "role_mailbox_local_part"
    return ""


def _parse_aware_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(datetime_timezone.utc)


def _clean_display_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    normalized = _CONTROL_RE.sub(" ", normalized)
    normalized = _SPACE_RE.sub(" ", normalized).strip(" \t\r\n\"'")
    # Gmail display names occasionally echo the address.  Do not turn an email
    # local part into a guessed person name.
    if "@" in normalized:
        return ""
    return normalized[:201].rstrip()


def _person_name(display_name: str) -> tuple[str, str]:
    if not display_name:
        return "", ""
    if display_name.count(",") == 1:
        family, given = (part.strip() for part in display_name.split(",", 1))
        if family and given:
            return given[:100].rstrip(), family[:100].rstrip()
    parts = display_name.split()
    if not parts:
        return "", ""
    first_name = parts[0][:100]
    last_name = " ".join(parts[1:])[:100].rstrip()
    return first_name, last_name


def _registrable_domain(domain: str) -> str:
    labels = domain.split(".")
    if len(labels) < 2:
        return domain
    suffix = ".".join(labels[-2:])
    if suffix in _COMMON_TWO_LEVEL_SUFFIXES and len(labels) >= 3:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _company_name_from_domain(registrable_domain: str) -> str:
    label = registrable_domain.split(".", 1)[0]
    words = _SPACE_RE.sub(" ", re.sub(r"[-_]+", " ", label)).strip()
    if not words:
        return registrable_domain[:200]
    return words.title()[:200]
