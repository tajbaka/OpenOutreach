"""Sender-scoped cleanup of stale invitations positively sent by this project."""
from __future__ import annotations

import logging
from datetime import timedelta

from django.db import connections, transaction
from django.db.utils import InterfaceError, OperationalError
from django.utils import timezone

from linkedin.actions.invitations import WithdrawalResult, withdraw_pending_invitation
from linkedin.conf import (
    ENABLE_STALE_INVITE_WITHDRAWAL,
    STALE_INVITE_AGE_DAYS,
    STALE_INVITE_WITHDRAWAL_DAILY_LIMIT,
)
from linkedin.enums import ProfileState
from linkedin.exceptions import InvitationWithdrawalError, SkipProfile
from linkedin.models import ActionLog, Task, active_day_start
from linkedin.operators import resolve_operator

logger = logging.getLogger(__name__)

_DB_DEAD_ERRORS = (OperationalError, InterfaceError)


def enqueue_withdraw_invites(*, operator: str, delay_seconds: float = 0) -> None:
    """Ensure at most one pending/running withdrawal task for a sender."""
    if not ENABLE_STALE_INVITE_WITHDRAWAL:
        return
    if not operator:
        raise ValueError("enqueue_withdraw_invites requires a non-empty operator")
    if Task.objects.filter(
        task_type=Task.TaskType.WITHDRAW_INVITES,
        status__in=[Task.Status.PENDING, Task.Status.RUNNING],
        payload__operator=operator,
    ).exists():
        return
    Task.objects.create(
        task_type=Task.TaskType.WITHDRAW_INVITES,
        scheduled_at=timezone.now() + timedelta(seconds=delay_seconds),
        payload={"operator": operator},
    )


def maybe_enqueue_withdraw_invites(profile, *, operator: str) -> bool:
    """Enqueue only when the verified local daily connect cap is the sole block."""
    if (
        not ENABLE_STALE_INVITE_WITHDRAWAL
        or not profile.reached_local_daily_connect_cap_only()
    ):
        return False
    enqueue_withdraw_invites(operator=operator)
    return True


def _profile_for_deal(deal) -> dict:
    from linkedin.db.leads import lead_to_profile_dict

    stored = lead_to_profile_dict(deal.lead)
    if stored is None:
        raise InvitationWithdrawalError(
            f"Deal {deal.pk} has no usable LinkedIn profile identifier"
        )
    profile = dict(stored.get("profile") or {})
    profile["public_identifier"] = stored["public_identifier"]
    profile["url"] = stored["url"]
    return profile


def _persist_confirmed_withdrawal(*, deal_id: int, linkedin_profile, withdrawn_at) -> bool:
    """Atomically stamp the terminal Deal and one rate-limit audit row."""
    from crm.models import ClosingReason, Deal

    with transaction.atomic():
        deal = Deal.objects.select_for_update().select_related("campaign").get(pk=deal_id)
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

        deal.state = ProfileState.FAILED
        deal.closing_reason = ClosingReason.FAILED
        deal.reason = (
            f"Project-sent invitation unanswered for at least "
            f"{STALE_INVITE_AGE_DAYS} days and withdrawn"
        )
        deal.invitation_withdrawn_at = withdrawn_at
        deal.save(
            update_fields=[
                "state",
                "closing_reason",
                "reason",
                "invitation_withdrawn_at",
                "update_date",
            ]
        )
        ActionLog.objects.create(
            linkedin_profile=linkedin_profile,
            campaign=deal.campaign,
            action_type=ActionLog.ActionType.WITHDRAW_INVITE,
        )
        return True


def _record_confirmed_withdrawal(*, deal_id: int, linkedin_profile) -> bool:
    """Retry the post-click transaction once if the DB socket died."""
    withdrawn_at = timezone.now()
    try:
        return _persist_confirmed_withdrawal(
            deal_id=deal_id,
            linkedin_profile=linkedin_profile,
            withdrawn_at=withdrawn_at,
        )
    except _DB_DEAD_ERRORS as e:
        logger.warning(
            "Withdrawal DB write hit a dead connection for Deal %s after "
            "LinkedIn confirmed the click; recycling and retrying once: %s",
            deal_id,
            e,
        )
        connections.close_all()
        return _persist_confirmed_withdrawal(
            deal_id=deal_id,
            linkedin_profile=linkedin_profile,
            withdrawn_at=withdrawn_at,
        )


def handle_withdraw_invites(task, session, qualifiers) -> None:
    """Withdraw up to the remaining sender-local daily cleanup allowance."""
    if not ENABLE_STALE_INVITE_WITHDRAWAL:
        logger.debug("stale invitation withdrawal disabled — skipping task %s", task.pk)
        return

    operator = resolve_operator(session.linkedin_profile.linkedin_username)
    task_operator = (task.payload or {}).get("operator")
    if task_operator != operator:
        raise ValueError(
            f"withdraw_invites task belongs to {task_operator!r}, not {operator!r}"
        )

    # Recheck at execution time. A task that rolls into a new day or gains a
    # weekly/global/external blocker must not mutate LinkedIn.
    if not session.linkedin_profile.reached_local_daily_connect_cap_only():
        logger.info(
            "withdraw_invites: verified local daily connect cap is not the sole "
            "block for %s — skipping",
            operator,
        )
        return

    # Acceptance reconciliation always precedes any withdrawal candidate query.
    from linkedin.tasks.sweep_connections import (
        process_accepted_deal,
        reconcile_pending_connections,
    )

    reconciliation = reconcile_pending_connections(session)
    if not reconciliation.complete:
        logger.warning(
            "withdraw_invites: connection reconciliation stopped at %s; "
            "skipping withdrawals until a complete sweep succeeds",
            reconciliation.scrape.stop_reason,
        )
        return

    successful_today = ActionLog.objects.filter(
        linkedin_profile=session.linkedin_profile,
        action_type=ActionLog.ActionType.WITHDRAW_INVITE,
        created_at__gte=active_day_start(),
    ).count()
    remaining = max(STALE_INVITE_WITHDRAWAL_DAILY_LIMIT - successful_today, 0)
    if remaining == 0:
        logger.info("withdraw_invites: daily cleanup limit already reached for %s", operator)
        return

    from crm.models import Deal

    cutoff = timezone.now() - timedelta(days=STALE_INVITE_AGE_DAYS)
    eligible = list(
        Deal.objects.filter(
            state=ProfileState.PENDING,
            campaign__in=session.campaigns,
            invitation_sender=operator,
            invitation_sent_at__isnull=False,
            invitation_sent_at__lte=cutoff,
            invitation_withdrawn_at__isnull=True,
        )
        .select_related("lead", "campaign")
        .order_by("invitation_sent_at", "id")[:remaining]
    )

    withdrawn = 0
    for deal in eligible:
        # A successful withdrawal is a total daily LinkedIn mutation. Stop if
        # that creates any additional blocker alongside the connect-day cap.
        if not session.linkedin_profile.reached_local_daily_connect_cap_only():
            logger.info("withdraw_invites: a new safety blocker appeared — stopping")
            break

        session.campaign = deal.campaign
        try:
            result = withdraw_pending_invitation(session, _profile_for_deal(deal))
        except (InvitationWithdrawalError, SkipProfile) as e:
            logger.warning("Skipping unsafe withdrawal for Deal %s: %s", deal.pk, e)
            continue

        if result == WithdrawalResult.CONNECTED:
            process_accepted_deal(session, deal)
            continue
        if result == WithdrawalResult.NOT_PENDING:
            logger.warning(
                "Deal %s is no longer pending on LinkedIn but acceptance was not "
                "confirmed; leaving the CRM ledger unchanged",
                deal.pk,
            )
            continue

        if _record_confirmed_withdrawal(
            deal_id=deal.pk,
            linkedin_profile=session.linkedin_profile,
        ):
            withdrawn += 1

    logger.info(
        "withdraw_invites: %d confirmed withdrawal(s) for %s (daily max %d)",
        withdrawn,
        operator,
        STALE_INVITE_WITHDRAWAL_DAILY_LIMIT,
    )
