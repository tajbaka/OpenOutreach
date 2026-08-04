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
    SentInvitationMatch,
    WithdrawalResult,
    scan_sent_invitations_by_age,
    withdraw_sent_invitation_by_public_identifier,
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
DATE_LABEL_TOLERANCE_DAYS = 5
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
    match: SentInvitationMatch | None = None,
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
        _create_withdrawal_record(
            linkedin_profile=linkedin_profile,
            deal=deal,
            candidate=candidate,
            public_identifier=candidate.public_identifier,
            displayed_name=match.displayed_name if match is not None else candidate.lead_name,
            sent_label=match.sent_label if match is not None else "",
            withdrawn_at=withdrawn_at,
            source="crm_matched",
        )
        return True


def _create_withdrawal_record(
    *,
    linkedin_profile,
    deal=None,
    candidate: WithdrawalCandidate | None = None,
    public_identifier: str,
    displayed_name: str = "",
    sent_label: str = "",
    withdrawn_at: datetime,
    source: str,
) -> None:
    from linkedin.models import InvitationWithdrawalRecord

    profile_url = ""
    if candidate is not None:
        profile_url = candidate.profile_url
    elif public_identifier:
        profile_url = f"https://www.linkedin.com/in/{public_identifier}/"

    InvitationWithdrawalRecord.objects.create(
        linkedin_profile=linkedin_profile,
        deal=deal,
        public_identifier=public_identifier,
        linkedin_url=profile_url,
        displayed_name=displayed_name,
        sent_label=sent_label,
        source=source,
        withdrawn_at=withdrawn_at,
    )


def _record_unmapped_withdrawal(
    *,
    linkedin_profile,
    match: SentInvitationMatch,
    withdrawn_at: datetime,
) -> None:
    try:
        _create_withdrawal_record(
            linkedin_profile=linkedin_profile,
            public_identifier=match.public_identifier,
            displayed_name=match.displayed_name,
            sent_label=match.sent_label,
            withdrawn_at=withdrawn_at,
            source="date_based",
        )
    except _DB_DEAD_ERRORS as error:
        logger.warning(
            "Withdrawal ledger write hit a dead connection for %s after "
            "LinkedIn confirmed the click; recycling and retrying once: %s",
            match.public_identifier,
            error,
        )
        connections.close_all()
        _create_withdrawal_record(
            linkedin_profile=linkedin_profile,
            public_identifier=match.public_identifier,
            displayed_name=match.displayed_name,
            sent_label=match.sent_label,
            withdrawn_at=withdrawn_at,
            source="date_based",
        )


def _persist_live_matched_withdrawal(
    *,
    linkedin_profile,
    operator: str,
    match: SentInvitationMatch,
    withdrawn_at: datetime,
    existing_record=None,
) -> bool:
    """Reconcile authenticated live evidence to one operator-owned Deal.

    The Sent Invitations page proves which LinkedIn account sent the invite;
    an exact public-identifier match plus campaign ownership is therefore
    enough to repair legacy rows that predate the sender/timestamp ledger.
    Other operators' Deals for the same Lead remain untouched.
    """
    from crm.models import ClosingReason, Deal

    with transaction.atomic():
        deals = list(
            Deal.objects.select_for_update()
            .filter(
                campaign__user=linkedin_profile.user,
                lead__public_identifier__iexact=match.public_identifier,
                state=ProfileState.PENDING,
                invitation_withdrawn_at__isnull=True,
            )
            .select_related("lead", "campaign")[:2]
        )
        if len(deals) != 1:
            return False

        deal = deals[0]
        deal.state = ProfileState.FAILED
        deal.closing_reason = ClosingReason.FAILED
        deal.reason = (
            f"Invitation visible in {operator}'s Sent Invitations as "
            f"{match.sent_label!r} was withdrawn by the standalone "
            "maintenance command"
        )
        deal.invitation_sender = operator
        deal.invitation_withdrawn_at = withdrawn_at
        deal.save(
            update_fields=[
                "state", "closing_reason", "reason", "invitation_sender",
                "invitation_withdrawn_at", "update_date",
            ],
        )
        ActionLog.objects.create(
            linkedin_profile=linkedin_profile,
            campaign_id=deal.campaign_id,
            action_type=ActionLog.ActionType.WITHDRAW_INVITE,
        )
        if existing_record is None:
            _create_withdrawal_record(
                linkedin_profile=linkedin_profile,
                deal=deal,
                public_identifier=match.public_identifier,
                displayed_name=match.displayed_name,
                sent_label=match.sent_label,
                withdrawn_at=withdrawn_at,
                source="crm_matched",
            )
        else:
            existing_record.deal = deal
            existing_record.linkedin_url = deal.lead.linkedin_url
            existing_record.source = "crm_matched"
            existing_record.save(update_fields=["deal", "linkedin_url", "source"])
        return True


def record_live_or_unmapped_withdrawal(
    *,
    linkedin_profile,
    operator: str,
    match: SentInvitationMatch,
    withdrawn_at: datetime,
) -> None:
    """Persist one live-confirmed click, linking a unique owned Deal when possible."""
    try:
        if _persist_live_matched_withdrawal(
            linkedin_profile=linkedin_profile,
            operator=operator,
            match=match,
            withdrawn_at=withdrawn_at,
        ):
            return
        _record_unmapped_withdrawal(
            linkedin_profile=linkedin_profile,
            match=match,
            withdrawn_at=withdrawn_at,
        )
    except _DB_DEAD_ERRORS as error:
        logger.warning(
            "Live withdrawal persistence hit a dead connection for %s; "
            "recycling and retrying once: %s",
            match.public_identifier,
            error,
        )
        connections.close_all()
        if _persist_live_matched_withdrawal(
            linkedin_profile=linkedin_profile,
            operator=operator,
            match=match,
            withdrawn_at=withdrawn_at,
        ):
            return
        _record_unmapped_withdrawal(
            linkedin_profile=linkedin_profile,
            match=match,
            withdrawn_at=withdrawn_at,
        )


def reconcile_unmapped_withdrawals(
    *,
    linkedin_profile,
    operator: str,
    since: datetime,
) -> int:
    """Link recent date-based audit rows to unique operator-owned Deals."""
    from linkedin.models import InvitationWithdrawalRecord

    reconciled = 0
    records = list(
        InvitationWithdrawalRecord.objects.filter(
            linkedin_profile=linkedin_profile,
            deal__isnull=True,
            source=InvitationWithdrawalRecord.Source.DATE_BASED,
            withdrawn_at__gte=since,
        ).order_by("withdrawn_at", "id")
    )
    for record in records:
        match = SentInvitationMatch(
            public_identifier=record.public_identifier,
            displayed_name=record.displayed_name,
            sent_label=record.sent_label,
        )
        if _persist_live_matched_withdrawal(
            linkedin_profile=linkedin_profile,
            operator=operator,
            match=match,
            withdrawn_at=record.withdrawn_at,
            existing_record=record,
        ):
            reconciled += 1
    return reconciled


def record_confirmed_withdrawal(
    *,
    candidate: WithdrawalCandidate,
    linkedin_profile,
    operator: str,
    match: SentInvitationMatch | None = None,
) -> bool:
    """Retry the irreversible-click ledger write once on a dead DB socket."""
    withdrawn_at = timezone.now()
    try:
        return _persist_confirmed_withdrawal(
            candidate=candidate,
            linkedin_profile=linkedin_profile,
            operator=operator,
            withdrawn_at=withdrawn_at,
            match=match,
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
            match=match,
        )


def _fresh_deal(candidate: WithdrawalCandidate):
    from crm.models import Deal

    return Deal.objects.select_related("lead", "campaign").get(pk=candidate.deal_id)


def _recycle_database_connection() -> None:
    connection = connections["default"]
    if connection.in_atomic_block:
        return
    connections.close_all()
    connection.ensure_connection()


def _approximate_timeline_depth_days(
    candidates: Sequence[WithdrawalCandidate],
) -> int | None:
    if not candidates:
        return None
    now = timezone.now()
    oldest_selected = min(candidate.sent_at for candidate in candidates)
    age_days = (now - oldest_selected).total_seconds() / 86400
    return max(0, math.ceil(age_days) + 2)


def _date_label_age_window(
    *,
    since: datetime | None,
    cutoff: datetime,
) -> tuple[int, int | None]:
    now = timezone.now()
    min_age_days = math.floor((now - cutoff).total_seconds() / 86400)
    min_age_days = max(0, min_age_days - DATE_LABEL_TOLERANCE_DAYS)
    if since is None:
        return min_age_days, None

    max_age_days = math.ceil((now - since).total_seconds() / 86400)
    max_age_days = max(min_age_days, max_age_days + DATE_LABEL_TOLERANCE_DAYS)
    return min_age_days, max_age_days


def apply_withdrawal_batch(
    *,
    session,
    candidates: Sequence[WithdrawalCandidate],
    linkedin_profile,
    operator: str,
    since: datetime | None = None,
    cutoff: datetime,
    withdrawal_limit: int | None = None,
) -> WithdrawalBatchResult:
    """Withdraw visible Sent cards by date, updating CRM when a card maps back."""
    if withdrawal_limit is not None and withdrawal_limit <= 0:
        raise ValueError("withdrawal_limit must be greater than zero")
    if timezone.is_naive(cutoff):
        raise ValueError("cutoff must be timezone-aware")
    if since is not None and timezone.is_naive(since):
        raise ValueError("since must be timezone-aware")

    candidates_by_id = {
        candidate.public_identifier.casefold(): candidate
        for candidate in candidates
    }
    min_age_days, max_age_days = _date_label_age_window(
        since=since,
        cutoff=cutoff,
    )
    not_pending = 0
    skipped = 0

    scan = scan_sent_invitations_by_age(
        session,
        min_age_days=min_age_days,
        max_age_days=max_age_days,
        match_limit=withdrawal_limit,
    )
    logger.info(
        "Date-based Sent Invitations scan completed: cards=%d rounds=%d "
        "date_matches=%d min_age_days=%d max_age_days=%s end=%s "
        "timeline_depth=%s oldest_visible_days=%s",
        scan.cards_seen,
        scan.scroll_rounds,
        len(scan.matches),
        min_age_days,
        max_age_days,
        scan.reached_end,
        scan.reached_timeline_depth,
        scan.oldest_visible_days,
    )
    _recycle_database_connection()

    withdrawn = 0
    for match in scan.matches:
        if withdrawal_limit is not None and withdrawn >= withdrawal_limit:
            break
        try:
            result = withdraw_sent_invitation_by_public_identifier(
                session,
                match.public_identifier,
            )
        except InvitationWithdrawalError as error:
            logger.warning(
                "Skipping unsafe date-based withdrawal for %s: %s",
                match.public_identifier,
                error,
            )
            skipped += 1
            continue

        if result == WithdrawalResult.NOT_PENDING:
            logger.warning(
                "%s disappeared from Sent Invitations before withdrawal",
                match.public_identifier,
            )
            not_pending += 1
            continue
        withdrawn_at = timezone.now()
        withdrawn += 1

        candidate = candidates_by_id.get(match.public_identifier.casefold())
        if candidate is None:
            record_live_or_unmapped_withdrawal(
                linkedin_profile=linkedin_profile,
                operator=operator,
                match=match,
                withdrawn_at=withdrawn_at,
            )
            logger.info(
                "Date-based withdrawal for %s had no eligible CRM candidate; "
                "LinkedIn withdrawal was still confirmed",
                match.public_identifier,
            )
            continue
        deal = _fresh_deal(candidate)
        if deal.state != ProfileState.PENDING or deal.invitation_withdrawn_at is not None:
            logger.info(
                "Date-based withdrawal for %s maps to Deal %s, but CRM is "
                "already non-pending; preserving CRM state",
                match.public_identifier,
                candidate.deal_id,
            )
            continue
        if not record_confirmed_withdrawal(
            candidate=candidate,
            linkedin_profile=linkedin_profile,
            operator=operator,
            match=match,
        ):
            logger.info(
                "Date-based withdrawal for %s maps to Deal %s, but CRM write "
                "was idempotently skipped",
                match.public_identifier,
                candidate.deal_id,
            )

    return WithdrawalBatchResult(
        planned=len(scan.matches),
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
