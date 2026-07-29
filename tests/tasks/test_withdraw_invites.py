from datetime import timedelta
from unittest.mock import Mock, patch

import pytest
from django.db.utils import OperationalError
from django.utils import timezone

from crm.models import ClosingReason, Deal, Lead
from linkedin.actions.invitations import WithdrawalResult
from linkedin.enums import ProfileState
from linkedin.exceptions import SkipProfile
from linkedin.models import ActionLog, Task
from linkedin.operators import resolve_operator
from linkedin.tasks.withdraw_invites import (
    _record_confirmed_withdrawal,
    handle_withdraw_invites,
)


def _stale_deal(fake_session, public_id="alice", *, sender=None, ledger=True):
    operator = sender or resolve_operator(
        fake_session.linkedin_profile.linkedin_username,
    )
    lead = Lead.objects.create(
        first_name=public_id.title(),
        company_name="Acme",
        linkedin_url=f"https://www.linkedin.com/in/{public_id}/",
        public_identifier=public_id,
    )
    return Deal.objects.create(
        lead=lead,
        campaign=fake_session.campaign,
        state=ProfileState.PENDING,
        invitation_sender=operator if ledger else "",
        invitation_sent_at=(
            timezone.now() - timedelta(days=31)
            if ledger
            else None
        ),
    )


def _task(fake_session):
    operator = resolve_operator(fake_session.linkedin_profile.linkedin_username)
    return Task.objects.create(
        task_type=Task.TaskType.WITHDRAW_INVITES,
        status=Task.Status.RUNNING,
        scheduled_at=timezone.now(),
        started_at=timezone.now(),
        payload={"operator": operator},
    )


@pytest.fixture
def cap_reached(fake_session):
    check = Mock(return_value=True)
    fake_session.linkedin_profile.reached_local_daily_connect_cap_only = check
    return check


@pytest.mark.django_db
@patch("linkedin.tasks.withdraw_invites.ENABLE_STALE_INVITE_WITHDRAWAL", True)
@patch("linkedin.tasks.withdraw_invites.STALE_INVITE_AGE_DAYS", 30)
@patch("linkedin.tasks.sweep_connections.reconcile_pending_connections")
@patch("linkedin.tasks.withdraw_invites.withdraw_pending_invitation")
def test_confirmed_withdrawal_stamps_terminal_audit(
    mock_withdraw,
    mock_reconcile,
    fake_session,
    cap_reached,
):
    deal = _stale_deal(fake_session)
    mock_reconcile.return_value = (1, 0, 0)
    mock_withdraw.return_value = WithdrawalResult.WITHDRAWN

    handle_withdraw_invites(_task(fake_session), fake_session, {})

    deal.refresh_from_db()
    assert deal.state == ProfileState.FAILED
    assert deal.closing_reason == ClosingReason.FAILED
    assert deal.invitation_withdrawn_at is not None
    assert not deal.lead.disqualified
    assert ActionLog.objects.filter(
        linkedin_profile=fake_session.linkedin_profile,
        campaign=fake_session.campaign,
        action_type=ActionLog.ActionType.WITHDRAW_INVITE,
    ).count() == 1


@pytest.mark.django_db
@patch("linkedin.tasks.withdraw_invites.ENABLE_STALE_INVITE_WITHDRAWAL", True)
@patch("linkedin.tasks.withdraw_invites.STALE_INVITE_WITHDRAWAL_DAILY_LIMIT", 5)
@patch("linkedin.tasks.sweep_connections.reconcile_pending_connections", return_value=(6, 0, 0))
@patch(
    "linkedin.tasks.withdraw_invites.withdraw_pending_invitation",
    return_value=WithdrawalResult.WITHDRAWN,
)
def test_withdraws_at_most_five_successes_per_sender_day(
    mock_withdraw,
    _mock_reconcile,
    fake_session,
    cap_reached,
):
    for index in range(6):
        _stale_deal(fake_session, f"lead-{index}")

    handle_withdraw_invites(_task(fake_session), fake_session, {})

    assert mock_withdraw.call_count == 5
    assert ActionLog.objects.filter(
        linkedin_profile=fake_session.linkedin_profile,
        action_type=ActionLog.ActionType.WITHDRAW_INVITE,
    ).count() == 5
    assert Deal.objects.filter(invitation_withdrawn_at__isnull=False).count() == 5


@pytest.mark.django_db
@patch("linkedin.tasks.withdraw_invites.ENABLE_STALE_INVITE_WITHDRAWAL", True)
@patch("linkedin.tasks.sweep_connections.reconcile_pending_connections")
@patch("linkedin.tasks.withdraw_invites.withdraw_pending_invitation")
def test_reconciles_acceptances_before_first_profile_action(
    mock_withdraw,
    mock_reconcile,
    fake_session,
    cap_reached,
):
    _stale_deal(fake_session)
    events = []
    mock_reconcile.side_effect = lambda session: events.append("reconcile") or (1, 0, 0)
    mock_withdraw.side_effect = (
        lambda session, profile: events.append("withdraw") or WithdrawalResult.WITHDRAWN
    )

    handle_withdraw_invites(_task(fake_session), fake_session, {})

    assert events == ["reconcile", "withdraw"]


@pytest.mark.django_db
@patch("linkedin.tasks.withdraw_invites.ENABLE_STALE_INVITE_WITHDRAWAL", True)
@patch("linkedin.tasks.sweep_connections.reconcile_pending_connections")
@patch("linkedin.tasks.withdraw_invites.withdraw_pending_invitation")
def test_ignores_pending_rows_without_project_send_ledger_or_wrong_sender(
    mock_withdraw,
    mock_reconcile,
    fake_session,
    cap_reached,
):
    _stale_deal(fake_session, "legacy", ledger=False)
    _stale_deal(fake_session, "other", sender="Another Operator")
    mock_reconcile.return_value = (2, 0, 0)

    handle_withdraw_invites(_task(fake_session), fake_session, {})

    mock_withdraw.assert_not_called()
    assert Deal.objects.filter(state=ProfileState.PENDING).count() == 2


@pytest.mark.django_db
@patch("linkedin.tasks.withdraw_invites.ENABLE_STALE_INVITE_WITHDRAWAL", True)
@patch("linkedin.tasks.sweep_connections.reconcile_pending_connections")
@patch("linkedin.tasks.withdraw_invites.withdraw_pending_invitation")
def test_last_second_acceptance_uses_shared_acceptance_path(
    mock_withdraw,
    mock_reconcile,
    fake_session,
    cap_reached,
):
    deal = _stale_deal(fake_session)
    mock_reconcile.return_value = (1, 0, 0)
    mock_withdraw.return_value = WithdrawalResult.CONNECTED

    with patch("linkedin.tasks.sweep_connections.process_accepted_deal") as mock_accept:
        handle_withdraw_invites(_task(fake_session), fake_session, {})

    mock_accept.assert_called_once()
    assert mock_accept.call_args.args[1].pk == deal.pk
    assert not ActionLog.objects.filter(
        action_type=ActionLog.ActionType.WITHDRAW_INVITE,
    ).exists()


@pytest.mark.django_db
@patch("linkedin.tasks.withdraw_invites.ENABLE_STALE_INVITE_WITHDRAWAL", True)
@patch("linkedin.tasks.sweep_connections.reconcile_pending_connections")
@patch("linkedin.tasks.withdraw_invites.withdraw_pending_invitation")
def test_no_verified_daily_cap_means_no_reconciliation_or_ui(
    mock_withdraw,
    mock_reconcile,
    fake_session,
):
    _stale_deal(fake_session)
    fake_session.linkedin_profile.reached_local_daily_connect_cap_only = Mock(
        return_value=False,
    )

    handle_withdraw_invites(_task(fake_session), fake_session, {})

    mock_reconcile.assert_not_called()
    mock_withdraw.assert_not_called()


@pytest.mark.django_db
@patch("linkedin.tasks.withdraw_invites.ENABLE_STALE_INVITE_WITHDRAWAL", True)
@patch(
    "linkedin.tasks.sweep_connections.reconcile_pending_connections",
    return_value=(1, 0, 0),
)
@patch(
    "linkedin.tasks.withdraw_invites.withdraw_pending_invitation",
    side_effect=SkipProfile("profile returned 404"),
)
def test_disappeared_profile_is_a_recoverable_skip(
    mock_withdraw,
    _mock_reconcile,
    fake_session,
    cap_reached,
):
    deal = _stale_deal(fake_session)

    handle_withdraw_invites(_task(fake_session), fake_session, {})

    deal.refresh_from_db()
    assert mock_withdraw.call_count == 1
    assert deal.state == ProfileState.PENDING
    assert deal.invitation_withdrawn_at is None


@pytest.mark.django_db
def test_post_click_db_write_retries_once(fake_session):
    deal = _stale_deal(fake_session)
    with (
        patch(
            "linkedin.tasks.withdraw_invites._persist_confirmed_withdrawal",
            side_effect=[OperationalError("dead"), True],
        ) as mock_persist,
        patch("linkedin.tasks.withdraw_invites.connections.close_all") as mock_close,
    ):
        result = _record_confirmed_withdrawal(
            deal_id=deal.pk,
            linkedin_profile=fake_session.linkedin_profile,
        )

    assert result is True
    assert mock_persist.call_count == 2
    mock_close.assert_called_once()
