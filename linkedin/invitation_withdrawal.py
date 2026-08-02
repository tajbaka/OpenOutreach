"""Standalone stale-invitation selection, safety checks, and persistence."""
from __future__ import annotations

import hashlib
import logging
import math
import os
from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterator, Sequence

from django.db import connections, transaction
from django.db.utils import InterfaceError, OperationalError
from django.utils import timezone

from linkedin.actions.invitations import (
    SentInvitationTarget,
    WithdrawalResult,
    scan_sent_invitations,
    withdraw_sent_invitation,
)
from linkedin.enums import ProfileState
from linkedin.exceptions import (
    InvitationWithdrawalConflictError,
    InvitationWithdrawalError,
)
from linkedin.models import ActionLog
from linkedin.operators import resolve_operator

logger = logging.getLogger(__name__)

LEGACY_EVIDENCE_WINDOW_SECONDS = 10
_DB_DEAD_ERRORS = (OperationalError, InterfaceError)


@dataclass(frozen=True)
class WithdrawalCandidate:
    deal_id: int
    campaign_id: int
    campaign_name: str
    lead_name: str
    public_identifier: str
    profile_url: str
    sent_at: datetime
    evidence: str
    legacy_action_log_id: int | None = None


@dataclass(frozen=True)
class WithdrawalPlan:
    operator: str
    since: datetime | None
    cutoff: datetime
    limit: int | None
    pending_total: int
    proven_total: int
    eligible_total: int
    candidates: tuple[WithdrawalCandidate, ...]
    exclusions: tuple[tuple[str, int], ...]

    @property
    def exclusion_counts(self) -> dict[str, int]:
        return dict(self.exclusions)


@dataclass(frozen=True)
class WithdrawalBatchResult:
    planned: int
    accepted: int
    withdrawn: int
    not_pending: int
    skipped: int


def _public_identifier(deal) -> str:
    from linkedin.db.urls import url_to_public_id

    return (
        (deal.lead.public_identifier or "").strip()
        or (url_to_public_id(deal.lead.linkedin_url) or "").strip()
    )


def _candidate_from_deal(
    deal,
    *,
    sent_at: datetime,
    evidence: str,
    legacy_action_log_id: int | None = None,
) -> WithdrawalCandidate:
    public_identifier = _public_identifier(deal)
    lead_name = (
        f"{deal.lead.first_name or ''} {deal.lead.last_name or ''}".strip()
        or public_identifier
    )
    return WithdrawalCandidate(
        deal_id=deal.pk,
        campaign_id=deal.campaign_id,
        campaign_name=deal.campaign.name,
        lead_name=lead_name,
        public_identifier=public_identifier,
        profile_url=deal.lead.linkedin_url,
        sent_at=sent_at,
        evidence=evidence,
        legacy_action_log_id=legacy_action_log_id,
    )


def _legacy_log_matches(deals, *, linkedin_profile) -> dict[int, object]:
    """Return only one-to-one legacy Deal → connect-log matches.

    The old send path saved the Deal as PENDING, immediately wrote one
    sender/campaign CONNECT ActionLog, then stored ``sent_note`` without
    touching ``update_date``. Evidence is accepted only when exactly one
    matching log exists in the following ten seconds and that log cannot
    also match another legacy Deal.
    """
    if not deals:
        return {}

    campaign_ids = {deal.campaign_id for deal in deals}
    earliest = min(deal.update_date for deal in deals)
    latest = max(deal.update_date for deal in deals) + timedelta(
        seconds=LEGACY_EVIDENCE_WINDOW_SECONDS,
    )
    logs = list(
        ActionLog.objects.filter(
            linkedin_profile=linkedin_profile,
            campaign_id__in=campaign_ids,
            action_type=ActionLog.ActionType.CONNECT,
            created_at__gte=earliest,
            created_at__lte=latest,
        ).order_by("created_at", "id")
    )
    logs_by_campaign: dict[int, list] = defaultdict(list)
    for log in logs:
        logs_by_campaign[log.campaign_id].append(log)

    matches_by_deal: dict[int, list] = {}
    deals_by_log: dict[int, list[int]] = defaultdict(list)
    for deal in deals:
        matches = [
            log
            for log in logs_by_campaign.get(deal.campaign_id, [])
            if 0
            <= (log.created_at - deal.update_date).total_seconds()
            <= LEGACY_EVIDENCE_WINDOW_SECONDS
        ]
        matches_by_deal[deal.pk] = matches
        for log in matches:
            deals_by_log[log.pk].append(deal.pk)

    unique: dict[int, object] = {}
    for deal in deals:
        matches = matches_by_deal[deal.pk]
        if len(matches) != 1:
            continue
        match = matches[0]
        if deals_by_log[match.pk] == [deal.pk]:
            unique[deal.pk] = match
    return unique


def build_withdrawal_plan(
    *,
    linkedin_profile,
    operator: str,
    since: datetime | None = None,
    cutoff: datetime,
    limit: int | None,
) -> WithdrawalPlan:
    """Build a newest-first, positively attributed, account-wide candidate pool."""
    from crm.models import Deal

    if not operator:
        raise ValueError("operator is required")
    if timezone.is_naive(cutoff):
        raise ValueError("cutoff must be timezone-aware")
    if since is not None and timezone.is_naive(since):
        raise ValueError("since must be timezone-aware")
    if since is not None and since >= cutoff:
        raise ValueError("since must be earlier than cutoff")
    if limit is not None and limit <= 0:
        raise ValueError("limit must be greater than zero")

    pending = list(
        Deal.objects.filter(
            state=ProfileState.PENDING,
            invitation_withdrawn_at__isnull=True,
        )
        .select_related("lead", "campaign")
        .only(
            "id",
            "campaign_id",
            "state",
            "sent_note",
            "invitation_sent_at",
            "invitation_sender",
            "invitation_withdrawn_at",
            "update_date",
            "lead__first_name",
            "lead__last_name",
            "lead__linkedin_url",
            "lead__public_identifier",
            "campaign__name",
        )
        .order_by("id")
    )
    legacy_deals = [
        deal
        for deal in pending
        if deal.invitation_sent_at is None
        and not (deal.invitation_sender or "").strip()
        and (deal.sent_note or "").strip()
    ]
    legacy_matches = _legacy_log_matches(
        legacy_deals,
        linkedin_profile=linkedin_profile,
    )

    exclusions: Counter[str] = Counter()
    proven: list[WithdrawalCandidate] = []
    for deal in pending:
        sent_at = deal.invitation_sent_at
        sender = (deal.invitation_sender or "").strip()
        candidate: WithdrawalCandidate | None = None

        if sent_at is not None and sender:
            if resolve_operator(sender) != operator:
                exclusions["different_sender"] += 1
                continue
            candidate = _candidate_from_deal(
                deal,
                sent_at=sent_at,
                evidence="ledger",
            )
        elif sent_at is not None or sender:
            exclusions["incomplete_ledger"] += 1
            continue
        elif not (deal.sent_note or "").strip():
            exclusions["no_project_send_evidence"] += 1
            continue
        else:
            legacy_log = legacy_matches.get(deal.pk)
            if legacy_log is None:
                exclusions["legacy_evidence_missing_or_ambiguous"] += 1
                continue
            candidate = _candidate_from_deal(
                deal,
                sent_at=legacy_log.created_at,
                evidence="legacy_connect_log",
                legacy_action_log_id=legacy_log.pk,
            )

        if not candidate.public_identifier:
            exclusions["missing_profile_identifier"] += 1
            continue
        proven.append(candidate)

    proven_total = len(proven)
    before_cutoff: list[WithdrawalCandidate] = []
    for candidate in proven:
        if since is not None and candidate.sent_at < since:
            exclusions["before_since_boundary"] += 1
            continue
        if candidate.sent_at >= cutoff:
            exclusions["not_before_cutoff"] += 1
            continue
        before_cutoff.append(candidate)

    candidates_by_profile: dict[str, list[WithdrawalCandidate]] = defaultdict(list)
    for candidate in before_cutoff:
        candidates_by_profile[candidate.public_identifier.casefold()].append(candidate)

    eligible: list[WithdrawalCandidate] = []
    for candidates in candidates_by_profile.values():
        if len(candidates) != 1:
            exclusions["duplicate_profile_evidence"] += len(candidates)
            continue
        eligible.append(candidates[0])

    eligible.sort(
        key=lambda candidate: (candidate.sent_at, candidate.deal_id),
        reverse=True,
    )
    return WithdrawalPlan(
        operator=operator,
        since=since,
        cutoff=cutoff,
        limit=limit,
        pending_total=len(pending),
        proven_total=proven_total,
        eligible_total=len(eligible),
        candidates=tuple(eligible),
        exclusions=tuple(sorted(exclusions.items())),
    )


def _legacy_evidence_is_current(
    *,
    deal,
    candidate: WithdrawalCandidate,
    linkedin_profile,
) -> bool:
    if (
        deal.invitation_sent_at is not None
        or (deal.invitation_sender or "").strip()
        or not (deal.sent_note or "").strip()
        or candidate.legacy_action_log_id is None
    ):
        return False
    log = ActionLog.objects.filter(
        pk=candidate.legacy_action_log_id,
        linkedin_profile=linkedin_profile,
        campaign_id=deal.campaign_id,
        action_type=ActionLog.ActionType.CONNECT,
    ).first()
    if log is None:
        return False
    delta = (log.created_at - deal.update_date).total_seconds()
    return 0 <= delta <= LEGACY_EVIDENCE_WINDOW_SECONDS


def _persist_confirmed_withdrawal(
    *,
    candidate: WithdrawalCandidate,
    linkedin_profile,
    operator: str,
    withdrawn_at: datetime,
) -> bool:
    """Atomically persist one confirmed withdrawal and its audit row."""
    from crm.models import ClosingReason, Deal

    with transaction.atomic():
        deal = Deal.objects.select_for_update().get(pk=candidate.deal_id)
        if deal.invitation_withdrawn_at is not None:
            return False
        if deal.state != ProfileState.PENDING:
            logger.warning(
                "Withdrawal was confirmed in LinkedIn but Deal %s is now %s; "
                "preserving the newer CRM state",
                deal.pk,
                deal.state,
            )
            return False

        if candidate.evidence == "ledger":
            evidence_valid = (
                deal.invitation_sent_at == candidate.sent_at
                and resolve_operator(deal.invitation_sender) == operator
            )
        else:
            evidence_valid = _legacy_evidence_is_current(
                deal=deal,
                candidate=candidate,
                linkedin_profile=linkedin_profile,
            )
        if not evidence_valid:
            raise InvitationWithdrawalError(
                f"Deal {deal.pk} send evidence changed after planning"
            )

        deal.state = ProfileState.FAILED
        deal.closing_reason = ClosingReason.FAILED
        deal.reason = (
            f"Project-sent invitation from {candidate.sent_at.date().isoformat()} "
            "was withdrawn by the standalone maintenance command"
        )
        deal.invitation_withdrawn_at = withdrawn_at
        update_fields = [
            "state",
            "closing_reason",
            "reason",
            "invitation_withdrawn_at",
            "update_date",
        ]
        if candidate.evidence == "legacy_connect_log":
            deal.invitation_sent_at = candidate.sent_at
            deal.invitation_sender = operator
            update_fields.extend(["invitation_sent_at", "invitation_sender"])
        deal.save(update_fields=update_fields)
        ActionLog.objects.create(
            linkedin_profile=linkedin_profile,
            campaign_id=deal.campaign_id,
            action_type=ActionLog.ActionType.WITHDRAW_INVITE,
        )
        return True


def record_confirmed_withdrawal(
    *,
    candidate: WithdrawalCandidate,
    linkedin_profile,
    operator: str,
) -> bool:
    """Retry the irreversible-click ledger write once on a dead DB socket."""
    withdrawn_at = timezone.now()
    try:
        return _persist_confirmed_withdrawal(
            candidate=candidate,
            linkedin_profile=linkedin_profile,
            operator=operator,
            withdrawn_at=withdrawn_at,
        )
    except _DB_DEAD_ERRORS as error:
        logger.warning(
            "Withdrawal DB write hit a dead connection for Deal %s after "
            "LinkedIn confirmed the click; recycling and retrying once: %s",
            candidate.deal_id,
            error,
        )
        connections.close_all()
        return _persist_confirmed_withdrawal(
            candidate=candidate,
            linkedin_profile=linkedin_profile,
            operator=operator,
            withdrawn_at=withdrawn_at,
        )


def _fresh_deal(candidate: WithdrawalCandidate):
    from crm.models import Deal

    return Deal.objects.select_related("lead", "campaign").get(pk=candidate.deal_id)


def _recycle_database_connection() -> None:
    connections.close_all()
    connections["default"].ensure_connection()


def _approximate_timeline_depth_days(
    candidates: Sequence[WithdrawalCandidate],
) -> int | None:
    if not candidates:
        return None
    now = timezone.now()
    oldest_selected = min(candidate.sent_at for candidate in candidates)
    age_days = (now - oldest_selected).total_seconds() / 86400
    return max(0, math.ceil(age_days) + 2)


def apply_withdrawal_batch(
    *,
    session,
    candidates: Sequence[WithdrawalCandidate],
    linkedin_profile,
    operator: str,
    withdrawal_limit: int | None = None,
) -> WithdrawalBatchResult:
    """Scan the Sent page once, then withdraw only exact URL/name matches."""
    if withdrawal_limit is not None and withdrawal_limit <= 0:
        raise ValueError("withdrawal_limit must be greater than zero")

    pending_candidates: list[WithdrawalCandidate] = []
    not_pending = 0
    skipped = 0

    for candidate in candidates:
        deal = _fresh_deal(candidate)
        if deal.state != ProfileState.PENDING or deal.invitation_withdrawn_at is not None:
            not_pending += 1
            continue
        pending_candidates.append(candidate)

    scan = scan_sent_invitations(
        session,
        [
            SentInvitationTarget(
                public_identifier=candidate.public_identifier,
                expected_name=candidate.lead_name,
            )
            for candidate in pending_candidates
        ],
        approximate_max_age_days=_approximate_timeline_depth_days(
            pending_candidates,
        ),
    )
    matched = scan.by_public_identifier
    logger.info(
        "Sent Invitations scan completed: cards=%d rounds=%d matches=%d/%d "
        "end=%s timeline_depth=%s oldest_visible_days=%s",
        scan.cards_seen,
        scan.scroll_rounds,
        len(matched),
        len(pending_candidates),
        scan.reached_end,
        scan.reached_timeline_depth,
        scan.oldest_visible_days,
    )
    _recycle_database_connection()

    withdrawn = 0
    for candidate in pending_candidates:
        if withdrawal_limit is not None and withdrawn >= withdrawal_limit:
            break
        key = candidate.public_identifier.casefold()
        if key not in matched:
            logger.warning(
                "Deal %s (%s) is absent from LinkedIn's Sent Invitations "
                "page; leaving CRM unchanged",
                candidate.deal_id,
                candidate.public_identifier,
            )
            not_pending += 1
            continue
        deal = _fresh_deal(candidate)
        if deal.state != ProfileState.PENDING or deal.invitation_withdrawn_at is not None:
            not_pending += 1
            continue
        try:
            result = withdraw_sent_invitation(
                session,
                SentInvitationTarget(
                    public_identifier=candidate.public_identifier,
                    expected_name=candidate.lead_name,
                ),
            )
        except InvitationWithdrawalError as error:
            logger.warning(
                "Skipping unsafe withdrawal for Deal %s: %s",
                candidate.deal_id,
                error,
            )
            skipped += 1
            continue

        if result == WithdrawalResult.NOT_PENDING:
            logger.warning(
                "Deal %s disappeared from Sent Invitations before withdrawal; "
                "leaving CRM unchanged",
                candidate.deal_id,
            )
            not_pending += 1
            continue
        if record_confirmed_withdrawal(
            candidate=candidate,
            linkedin_profile=linkedin_profile,
            operator=operator,
        ):
            withdrawn += 1
        else:
            not_pending += 1

    return WithdrawalBatchResult(
        planned=len(candidates),
        accepted=0,
        withdrawn=withdrawn,
        not_pending=not_pending,
        skipped=skipped,
    )


def assert_no_daemon_conflict(
    *,
    operator: str,
    account_username: str,
    now: datetime | None = None,
) -> None:
    """Fail closed when this sender's daemon or persistent Chromium is live."""
    from linkedin.browser.cookie_store import profile_dir_for
    from linkedin.conf import PEER_STALE_MINUTES
    from linkedin.models import DaemonHeartbeat

    current_time = now or timezone.now()
    heartbeat = DaemonHeartbeat.objects.filter(sender=operator).first()
    if (
        heartbeat is not None
        and heartbeat.last_alive is not None
        and heartbeat.last_alive
        >= current_time - timedelta(minutes=PEER_STALE_MINUTES)
    ):
        raise InvitationWithdrawalConflictError(
            f"{operator}'s daemon heartbeat is fresh "
            f"({heartbeat.last_alive.isoformat()}); stop that daemon before applying"
        )

    profile_dir = profile_dir_for(account_username)
    live_markers = [
        profile_dir / "SingletonLock",
        profile_dir / "SingletonSocket",
        profile_dir / "SingletonCookie",
    ]
    present = [str(path) for path in live_markers if os.path.lexists(path)]
    if present:
        raise InvitationWithdrawalConflictError(
            "The daemon Chromium profile still has active lock markers: "
            + ", ".join(present)
        )


def _advisory_lock_key(operator: str) -> int:
    digest = hashlib.blake2b(
        f"openoutreach:withdraw-invitations:{operator}".encode(),
        digest_size=8,
    ).digest()
    return int.from_bytes(digest, byteorder="big", signed=True)


@contextmanager
def sender_advisory_lock(operator: str) -> Iterator[None]:
    """Hold a cross-host Postgres lock without using the browser-work socket."""
    lock_connection = connections["default"].copy(
        alias=f"withdraw_invitations_{operator.casefold()}",
    )
    try:
        if lock_connection.vendor != "postgresql":
            yield
            return

        key = _advisory_lock_key(operator)
        with lock_connection.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(%s)", [key])
            acquired = bool(cursor.fetchone()[0])
        if not acquired:
            raise InvitationWithdrawalConflictError(
                f"Another withdrawal command is already running for {operator}"
            )
        try:
            yield
        finally:
            with lock_connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [key])
    finally:
        lock_connection.close()


class BoundStandaloneSession:
    """Expose a verified DB sender profile through a standalone browser."""

    def __init__(self, browser_session, linkedin_profile):
        self._browser_session = browser_session
        self.linkedin_profile = linkedin_profile
        self.django_user = linkedin_profile.user
        self.campaign = None

    @property
    def campaigns(self):
        from linkedin.models import Campaign

        return Campaign.objects.filter(user=self.django_user)

    def __getattr__(self, name):
        return getattr(self._browser_session, name)
