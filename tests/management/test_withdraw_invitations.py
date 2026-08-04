from contextlib import nullcontext
from datetime import date, datetime, timedelta
from io import StringIO
from unittest.mock import MagicMock, Mock, patch
from zoneinfo import ZoneInfo

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connections
from django.utils import timezone

from crm.models import ClosingReason, Deal, Lead
from linkedin.actions.invitations import (
    SentInvitationMatch,
    SentInvitationScan,
    WithdrawalResult,
)
from linkedin.enums import ProfileState
from linkedin.exceptions import InvitationWithdrawalConflictError
from linkedin.invitation_withdrawal import (
    LEGACY_EVIDENCE_WINDOW_SECONDS,
    WithdrawalBatchResult,
    apply_withdrawal_batch,
    assert_no_daemon_conflict,
    build_withdrawal_plan,
    record_confirmed_withdrawal,
    sender_advisory_lock,
)
from linkedin.management.commands.withdraw_invitations import _cutoff_for_date
from linkedin.models import ActionLog, DaemonHeartbeat
from linkedin.models import InvitationWithdrawalRecord
from linkedin.operators import resolve_operator


def _pending_deal(
    fake_session,
    public_id: str,
    *,
    sent_at=None,
    sender=None,
    sent_note="",
):
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
        invitation_sent_at=sent_at,
        invitation_sender=sender or "",
        sent_note=sent_note,
    )


def _legacy_evidence(fake_session, deal, *, transition_at, log_at=None):
    Deal.objects.filter(pk=deal.pk).update(update_date=transition_at)
    deal.refresh_from_db()
    log = ActionLog.objects.create(
        linkedin_profile=fake_session.linkedin_profile,
        campaign=deal.campaign,
        action_type=ActionLog.ActionType.CONNECT,
    )
    ActionLog.objects.filter(pk=log.pk).update(
        created_at=log_at or transition_at + timedelta(seconds=1),
    )
    log.refresh_from_db()
    return log


def _plan(
    fake_session,
    *,
    since=None,
    before=date(2026, 6, 1),
    limit=25,
):
    operator = resolve_operator(
        fake_session.linkedin_profile.linkedin_username,
    )
    return build_withdrawal_plan(
        linkedin_profile=fake_session.linkedin_profile,
        operator=operator,
        since=_cutoff_for_date(since) if since is not None else None,
        cutoff=_cutoff_for_date(before),
        limit=limit,
    )


@pytest.mark.django_db
def test_plan_requires_positive_sender_ledger_and_excludes_pending_alone(fake_session):
    old = timezone.make_aware(
        datetime(2026, 5, 1),
        timezone=ZoneInfo("America/Toronto"),
    )
    operator = resolve_operator(
        fake_session.linkedin_profile.linkedin_username,
    )
    eligible = _pending_deal(
        fake_session,
        "eligible",
        sent_at=old,
        sender=operator,
    )
    _pending_deal(
        fake_session,
        "other-sender",
        sent_at=old,
        sender="Another Operator",
    )
    _pending_deal(fake_session, "pending-alone")

    plan = _plan(fake_session)

    assert [candidate.deal_id for candidate in plan.candidates] == [eligible.pk]
    assert plan.exclusion_counts == {
        "different_sender": 1,
        "no_project_send_evidence": 1,
    }


@pytest.mark.django_db
def test_plan_accepts_only_unique_legacy_connect_evidence(fake_session):
    transition = timezone.make_aware(
        datetime(2026, 4, 1, 9),
        timezone=ZoneInfo("America/Toronto"),
    )
    unique = _pending_deal(
        fake_session,
        "legacy-unique",
        sent_note="Project note",
    )
    unique_log = _legacy_evidence(
        fake_session,
        unique,
        transition_at=transition,
    )
    ambiguous_a = _pending_deal(
        fake_session,
        "legacy-ambiguous-a",
        sent_note="Project note A",
    )
    ambiguous_b = _pending_deal(
        fake_session,
        "legacy-ambiguous-b",
        sent_note="Project note B",
    )
    shared_transition = transition + timedelta(hours=1)
    Deal.objects.filter(pk__in=[ambiguous_a.pk, ambiguous_b.pk]).update(
        update_date=shared_transition,
    )
    shared_log = ActionLog.objects.create(
        linkedin_profile=fake_session.linkedin_profile,
        campaign=fake_session.campaign,
        action_type=ActionLog.ActionType.CONNECT,
    )
    ActionLog.objects.filter(pk=shared_log.pk).update(
        created_at=shared_transition + timedelta(seconds=1),
    )

    plan = _plan(fake_session)

    assert len(plan.candidates) == 1
    candidate = plan.candidates[0]
    assert candidate.deal_id == unique.pk
    assert candidate.evidence == "legacy_connect_log"
    assert candidate.legacy_action_log_id == unique_log.pk
    assert plan.exclusion_counts["legacy_evidence_missing_or_ambiguous"] == 2


@pytest.mark.django_db
def test_legacy_plan_uses_two_bounded_queries(fake_session, django_assert_num_queries):
    deal = _pending_deal(
        fake_session,
        "legacy-query-count",
        sent_note="Project note",
    )
    _legacy_evidence(
        fake_session,
        deal,
        transition_at=timezone.now() - timedelta(days=90),
    )

    with django_assert_num_queries(2):
        plan = _plan(
            fake_session,
            before=date.today() + timedelta(days=1),
        )

    assert [candidate.deal_id for candidate in plan.candidates] == [deal.pk]


@pytest.mark.django_db
def test_plan_uses_exclusive_cutoff_and_newest_before_cutoff_order(fake_session):
    operator = resolve_operator(
        fake_session.linkedin_profile.linkedin_username,
    )
    zone = ZoneInfo("America/Toronto")
    before = date(2026, 6, 1)
    cutoff = timezone.make_aware(
        datetime.combine(before, datetime.min.time()),
        timezone=zone,
    )
    newest = _pending_deal(
        fake_session,
        "at-cutoff",
        sent_at=cutoff,
        sender=operator,
    )
    middle = _pending_deal(
        fake_session,
        "middle",
        sent_at=cutoff - timedelta(days=2),
        sender=operator,
    )
    oldest = _pending_deal(
        fake_session,
        "oldest",
        sent_at=cutoff - timedelta(days=10),
        sender=operator,
    )

    plan = _plan(fake_session, before=before, limit=1)

    assert [candidate.deal_id for candidate in plan.candidates] == [
        middle.pk,
        oldest.pk,
    ]
    assert plan.limit == 1
    assert plan.eligible_total == 2
    assert plan.exclusion_counts["not_before_cutoff"] == 1
    assert newest.pk != oldest.pk


@pytest.mark.django_db
def test_plan_uses_inclusive_since_boundary(fake_session):
    operator = resolve_operator(
        fake_session.linkedin_profile.linkedin_username,
    )
    zone = ZoneInfo("America/Toronto")
    _pending_deal(
        fake_session,
        "too-old",
        sent_at=timezone.make_aware(
            datetime(2026, 4, 30, 23, 59),
            timezone=zone,
        ),
        sender=operator,
    )
    boundary = _pending_deal(
        fake_session,
        "at-boundary",
        sent_at=timezone.make_aware(
            datetime(2026, 5, 1),
            timezone=zone,
        ),
        sender=operator,
    )

    plan = _plan(
        fake_session,
        since=date(2026, 5, 1),
        before=date(2026, 7, 30),
    )

    assert [candidate.deal_id for candidate in plan.candidates] == [boundary.pk]
    assert plan.exclusion_counts["before_since_boundary"] == 1


@pytest.mark.django_db
def test_duplicate_profile_evidence_is_excluded(fake_session):
    operator = resolve_operator(
        fake_session.linkedin_profile.linkedin_username,
    )
    old = timezone.now() - timedelta(days=90)
    lead = Lead.objects.create(
        first_name="Duplicate",
        linkedin_url="https://www.linkedin.com/in/duplicate/",
        public_identifier="duplicate",
    )
    first = Deal.objects.create(
        lead=lead,
        campaign=fake_session.campaign,
        state=ProfileState.PENDING,
        invitation_sent_at=old,
        invitation_sender=operator,
    )
    from linkedin.models import Campaign

    second_campaign = Campaign.objects.create(
        name="Second campaign",
        user=fake_session.django_user,
    )
    Deal.objects.create(
        lead=lead,
        campaign=second_campaign,
        state=ProfileState.PENDING,
        invitation_sent_at=old + timedelta(seconds=1),
        invitation_sender=operator,
    )

    plan = _plan(fake_session)

    assert first.pk
    assert plan.candidates == ()
    assert plan.exclusion_counts["duplicate_profile_evidence"] == 2


@pytest.mark.django_db
def test_default_command_is_db_only_dry_run(fake_session, monkeypatch):
    operator = resolve_operator(
        fake_session.linkedin_profile.linkedin_username,
    )
    deal = _pending_deal(
        fake_session,
        "dry-run",
        sent_at=timezone.now() - timedelta(days=90),
        sender=operator,
    )
    monkeypatch.setenv(
        "LINKEDIN_USERNAME",
        fake_session.linkedin_profile.linkedin_username,
    )
    monkeypatch.setenv("LINKEDIN_PASSWORD", "secret")
    output = StringIO()

    with patch(
        "linkedin.management.commands.withdraw_invitations.StandaloneLinkedInSession"
    ) as session_class:
        call_command(
            "withdraw_invitations",
            "--account",
            "primary",
            "--before",
            "2026-07-01",
            stdout=output,
        )

    session_class.assert_not_called()
    deal.refresh_from_db()
    assert deal.state == ProfileState.PENDING
    assert deal.invitation_withdrawn_at is None
    assert not ActionLog.objects.filter(
        action_type=ActionLog.ActionType.WITHDRAW_INVITE,
    ).exists()
    assert "[dry-run]" in output.getvalue()
    assert "Exact planned batch (newest before cutoff first)" in output.getvalue()
    assert "planned scan pool: 1/all eligible" in output.getvalue()
    assert "withdrawal target: all live matches" in output.getvalue()


@pytest.mark.django_db
def test_plan_without_limit_selects_every_eligible_candidate(fake_session):
    operator = resolve_operator(
        fake_session.linkedin_profile.linkedin_username,
    )
    old = timezone.now() - timedelta(days=90)
    first = _pending_deal(
        fake_session,
        "unlimited-first",
        sent_at=old,
        sender=operator,
    )
    second = _pending_deal(
        fake_session,
        "unlimited-second",
        sent_at=old + timedelta(minutes=1),
        sender=operator,
    )

    plan = build_withdrawal_plan(
        linkedin_profile=fake_session.linkedin_profile,
        operator=operator,
        cutoff=_cutoff_for_date(date.today() + timedelta(days=1)),
        limit=None,
    )

    assert plan.limit is None
    assert [candidate.deal_id for candidate in plan.candidates] == [
        second.pk,
        first.pk,
    ]


@pytest.mark.django_db
def test_apply_refuses_authenticated_identity_mismatch(fake_session, monkeypatch):
    operator = resolve_operator(
        fake_session.linkedin_profile.linkedin_username,
    )
    _pending_deal(
        fake_session,
        "identity-check",
        sent_at=timezone.now() - timedelta(days=90),
        sender=operator,
    )
    monkeypatch.setenv(
        "LINKEDIN_USERNAME",
        fake_session.linkedin_profile.linkedin_username,
    )
    monkeypatch.setenv("LINKEDIN_PASSWORD", "secret")
    browser_session = Mock()

    with (
        patch(
            "linkedin.management.commands.withdraw_invitations.sender_advisory_lock",
            return_value=nullcontext(),
        ),
        patch(
            "linkedin.management.commands.withdraw_invitations.assert_no_daemon_conflict",
        ),
        patch(
            "linkedin.management.commands.withdraw_invitations.StandaloneLinkedInSession"
        ) as session_class,
        patch(
            "linkedin.management.commands.withdraw_invitations._authenticated_display_name",
            return_value="Arian Taj",
        ),
        patch(
            "linkedin.management.commands.withdraw_invitations.apply_withdrawal_batch",
        ) as apply_batch,
    ):
        session_class.return_value.__enter__.return_value = browser_session
        with pytest.raises(CommandError, match="authenticated as"):
            call_command(
                "withdraw_invitations",
                "--account",
                "primary",
                "--before",
                "2026-07-01",
                "--apply",
            )

    apply_batch.assert_not_called()


@pytest.mark.django_db
def test_apply_verifies_identity_and_runs_exact_planned_batch(fake_session, monkeypatch):
    operator = resolve_operator(
        fake_session.linkedin_profile.linkedin_username,
    )
    deal = _pending_deal(
        fake_session,
        "apply-command",
        sent_at=timezone.now() - timedelta(days=90),
        sender=operator,
    )
    monkeypatch.setenv(
        "LINKEDIN_USERNAME",
        fake_session.linkedin_profile.linkedin_username,
    )
    monkeypatch.setenv("LINKEDIN_PASSWORD", "secret")
    browser_session = Mock()
    output = StringIO()

    with (
        patch(
            "linkedin.management.commands.withdraw_invitations.sender_advisory_lock",
            return_value=nullcontext(),
        ),
        patch(
            "linkedin.management.commands.withdraw_invitations.assert_no_daemon_conflict",
        ),
        patch(
            "linkedin.management.commands.withdraw_invitations.StandaloneLinkedInSession"
        ) as session_class,
        patch(
            "linkedin.management.commands.withdraw_invitations._authenticated_display_name",
            return_value=fake_session.linkedin_profile.linkedin_username,
        ),
        patch(
            "linkedin.management.commands.withdraw_invitations.apply_withdrawal_batch",
            return_value=WithdrawalBatchResult(
                planned=1,
                accepted=0,
                withdrawn=1,
                not_pending=0,
                skipped=0,
            ),
        ) as apply_batch,
    ):
        session_class.return_value.__enter__.return_value = browser_session
        call_command(
            "withdraw_invitations",
            "--account",
            "primary",
            "--before",
            (date.today() + timedelta(days=1)).isoformat(),
            "--limit",
            "1",
            "--apply",
            stdout=output,
        )

    candidates = apply_batch.call_args.kwargs["candidates"]
    assert [candidate.deal_id for candidate in candidates] == [deal.pk]
    assert apply_batch.call_args.kwargs["operator"] == operator
    assert apply_batch.call_args.kwargs["since"] is None
    assert apply_batch.call_args.kwargs["cutoff"] == _cutoff_for_date(
        date.today() + timedelta(days=1)
    )
    assert apply_batch.call_args.kwargs["withdrawal_limit"] == 1
    assert "Batch complete" in output.getvalue()
    assert "Exact planned batch omitted at normal apply verbosity" in output.getvalue()
    assert "Exact planned batch (newest before cutoff first)" not in output.getvalue()


@pytest.mark.django_db
def test_fresh_daemon_heartbeat_blocks_apply(fake_session):
    operator = resolve_operator(
        fake_session.linkedin_profile.linkedin_username,
    )
    DaemonHeartbeat.objects.create(
        sender=operator,
        last_alive=timezone.now(),
    )

    with pytest.raises(
        InvitationWithdrawalConflictError,
        match="heartbeat is fresh",
    ):
        assert_no_daemon_conflict(
            operator=operator,
            account_username=fake_session.linkedin_profile.linkedin_username,
        )


@pytest.mark.django_db
def test_daemon_browser_lock_marker_blocks_apply(fake_session):
    operator = resolve_operator(
        fake_session.linkedin_profile.linkedin_username,
    )

    with (
        patch("linkedin.invitation_withdrawal.os.path.lexists", return_value=True),
        pytest.raises(
            InvitationWithdrawalConflictError,
            match="active lock markers",
        ),
    ):
        assert_no_daemon_conflict(
            operator=operator,
            account_username=fake_session.linkedin_profile.linkedin_username,
        )


@pytest.mark.django_db
def test_postgres_sender_lock_rejects_second_run(fake_session):
    fake_connection = MagicMock()
    fake_connection.vendor = "postgresql"
    cursor = fake_connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = (False,)

    with (
        patch.object(
            connections["default"],
            "copy",
            return_value=fake_connection,
        ),
        pytest.raises(
            InvitationWithdrawalConflictError,
            match="already running",
        ),
    ):
        with sender_advisory_lock("Arian"):
            pass

    fake_connection.close.assert_called_once()


@pytest.mark.django_db
def test_postgres_sender_lock_is_released_after_run(fake_session):
    fake_connection = MagicMock()
    fake_connection.vendor = "postgresql"
    cursor = fake_connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = (True,)

    with patch.object(
        connections["default"],
        "copy",
        return_value=fake_connection,
    ):
        with sender_advisory_lock("Arian"):
            pass

    statements = [call.args[0] for call in cursor.execute.call_args_list]
    assert statements == [
        "SELECT pg_try_advisory_lock(%s)",
        "SELECT pg_advisory_unlock(%s)",
    ]
    fake_connection.close.assert_called_once()


@pytest.mark.django_db
def test_batch_scans_every_candidate_before_first_withdrawal(fake_session):
    operator = resolve_operator(
        fake_session.linkedin_profile.linkedin_username,
    )
    first = _pending_deal(
        fake_session,
        "first",
        sent_at=timezone.now() - timedelta(days=90),
        sender=operator,
    )
    second = _pending_deal(
        fake_session,
        "second",
        sent_at=timezone.now() - timedelta(days=80),
        sender=operator,
    )
    plan = _plan(fake_session, before=date.today() + timedelta(days=1))
    events = []

    def scan(_session, *, min_age_days, max_age_days=None, match_limit=None):
        assert min_age_days == 0
        assert max_age_days is None
        assert match_limit is None
        events.append("scan:date-window")
        return SentInvitationScan(
            matches=(
                SentInvitationMatch(
                    public_identifier="second",
                    displayed_name="Second",
                    sent_label="Sent 8 weeks ago",
                ),
                SentInvitationMatch(
                    public_identifier="first",
                    displayed_name="First",
                    sent_label="Sent 8 weeks ago",
                ),
            ),
            cards_seen=100,
            scroll_rounds=10,
            reached_end=False,
        )

    def withdraw(_session, public_identifier):
        events.append(f"withdraw:{public_identifier}")
        return WithdrawalResult.WITHDRAWN

    with (
        patch(
            "linkedin.invitation_withdrawal.scan_sent_invitations_by_age",
            side_effect=scan,
        ),
        patch(
            "linkedin.invitation_withdrawal.withdraw_sent_invitation_by_public_identifier",
            side_effect=withdraw,
        ),
    ):
        result = apply_withdrawal_batch(
            session=fake_session,
            candidates=plan.candidates,
            linkedin_profile=fake_session.linkedin_profile,
            operator=operator,
            cutoff=_cutoff_for_date(date.today() + timedelta(days=1)),
        )

    assert [candidate.deal_id for candidate in plan.candidates] == [
        second.pk,
        first.pk,
    ]
    assert events == [
        "scan:date-window",
        "withdraw:second",
        "withdraw:first",
    ]
    assert result.withdrawn == 2
    assert ActionLog.objects.filter(
        action_type=ActionLog.ActionType.WITHDRAW_INVITE,
    ).count() == 2
    assert InvitationWithdrawalRecord.objects.count() == 2


@pytest.mark.django_db
def test_withdrawal_limit_caps_confirmed_successes_not_scan_pool(fake_session):
    operator = resolve_operator(
        fake_session.linkedin_profile.linkedin_username,
    )
    first = _pending_deal(
        fake_session,
        "first-limit",
        sent_at=timezone.now() - timedelta(days=95),
        sender=operator,
    )
    second = _pending_deal(
        fake_session,
        "second-limit",
        sent_at=timezone.now() - timedelta(days=90),
        sender=operator,
    )
    third = _pending_deal(
        fake_session,
        "third-limit",
        sent_at=timezone.now() - timedelta(days=85),
        sender=operator,
    )
    plan = _plan(fake_session, before=date.today() + timedelta(days=1), limit=1)
    events = []

    def scan(_session, *, min_age_days, max_age_days=None, match_limit=None):
        assert min_age_days == 0
        assert max_age_days is None
        assert match_limit == 1
        events.append("scan:date-window")
        return SentInvitationScan(
            matches=(
                SentInvitationMatch(
                    public_identifier="third-limit",
                    displayed_name="Third Limit",
                    sent_label="Sent 3 months ago",
                ),
            ),
            cards_seen=100,
            scroll_rounds=10,
            reached_end=False,
        )

    def withdraw(_session, public_identifier):
        events.append(f"withdraw:{public_identifier}")
        return WithdrawalResult.WITHDRAWN

    with (
        patch(
            "linkedin.invitation_withdrawal.scan_sent_invitations_by_age",
            side_effect=scan,
        ),
        patch(
            "linkedin.invitation_withdrawal.withdraw_sent_invitation_by_public_identifier",
            side_effect=withdraw,
        ),
    ):
        result = apply_withdrawal_batch(
            session=fake_session,
            candidates=plan.candidates,
            linkedin_profile=fake_session.linkedin_profile,
            operator=operator,
            cutoff=_cutoff_for_date(date.today() + timedelta(days=1)),
            withdrawal_limit=1,
        )

    assert [candidate.deal_id for candidate in plan.candidates] == [
        third.pk,
        second.pk,
        first.pk,
    ]
    assert result.withdrawn == 1
    assert events == [
        "scan:date-window",
        "withdraw:third-limit",
    ]
    third.refresh_from_db()
    second.refresh_from_db()
    first.refresh_from_db()
    assert third.invitation_withdrawn_at is not None
    assert second.invitation_withdrawn_at is None
    assert first.invitation_withdrawn_at is None


@pytest.mark.django_db
def test_unmatched_date_card_is_withdrawn_and_recorded_without_crm(fake_session):
    operator = resolve_operator(
        fake_session.linkedin_profile.linkedin_username,
    )
    deal = _pending_deal(
        fake_session,
        "absent",
        sent_at=timezone.now() - timedelta(days=90),
        sender=operator,
    )
    candidate = _plan(
        fake_session,
        before=date.today() + timedelta(days=1),
    ).candidates[0]

    with (
        patch(
            "linkedin.invitation_withdrawal.scan_sent_invitations_by_age",
            return_value=SentInvitationScan(
                matches=(
                    SentInvitationMatch(
                        public_identifier="linkedin-only",
                        displayed_name="LinkedIn Only",
                        sent_label="Sent 3 months ago",
                    ),
                ),
                cards_seen=653,
                scroll_rounds=100,
                reached_end=True,
            ),
        ),
        patch(
            "linkedin.invitation_withdrawal.withdraw_sent_invitation_by_public_identifier",
            return_value=WithdrawalResult.WITHDRAWN,
        ) as withdraw,
    ):
        result = apply_withdrawal_batch(
            session=fake_session,
            candidates=[candidate],
            linkedin_profile=fake_session.linkedin_profile,
            operator=operator,
            cutoff=_cutoff_for_date(date.today() + timedelta(days=1)),
        )

    withdraw.assert_called_once_with(fake_session, "linkedin-only")
    deal.refresh_from_db()
    assert deal.state == ProfileState.PENDING
    assert deal.invitation_withdrawn_at is None
    assert result.withdrawn == 1
    assert result.not_pending == 0
    assert not ActionLog.objects.filter(
        action_type=ActionLog.ActionType.WITHDRAW_INVITE,
    ).exists()
    record = InvitationWithdrawalRecord.objects.get()
    assert record.public_identifier == "linkedin-only"
    assert record.deal is None


@pytest.mark.django_db
def test_not_pending_is_not_falsely_recorded(fake_session):
    operator = resolve_operator(
        fake_session.linkedin_profile.linkedin_username,
    )
    deal = _pending_deal(
        fake_session,
        "not-pending",
        sent_at=timezone.now() - timedelta(days=90),
        sender=operator,
    )
    candidate = _plan(
        fake_session,
        before=date.today() + timedelta(days=1),
    ).candidates[0]

    with patch(
        "linkedin.invitation_withdrawal.scan_sent_invitations_by_age",
        return_value=SentInvitationScan(
            matches=(
                SentInvitationMatch(
                    public_identifier=candidate.public_identifier,
                    displayed_name=candidate.lead_name,
                    sent_label="Sent 8 weeks ago",
                ),
            ),
            cards_seen=100,
            scroll_rounds=10,
            reached_end=False,
        ),
    ), patch(
        "linkedin.invitation_withdrawal.withdraw_sent_invitation_by_public_identifier",
        return_value=WithdrawalResult.NOT_PENDING,
    ):
        result = apply_withdrawal_batch(
            session=fake_session,
            candidates=[candidate],
            linkedin_profile=fake_session.linkedin_profile,
            operator=operator,
            cutoff=_cutoff_for_date(date.today() + timedelta(days=1)),
        )

    deal.refresh_from_db()
    assert result.not_pending == 1
    assert deal.state == ProfileState.PENDING
    assert deal.invitation_withdrawn_at is None


@pytest.mark.django_db
def test_confirmed_withdrawal_is_atomic_and_idempotent(fake_session):
    operator = resolve_operator(
        fake_session.linkedin_profile.linkedin_username,
    )
    deal = _pending_deal(
        fake_session,
        "idempotent",
        sent_at=timezone.now() - timedelta(days=90),
        sender=operator,
    )
    candidate = _plan(
        fake_session,
        before=date.today() + timedelta(days=1),
    ).candidates[0]

    assert record_confirmed_withdrawal(
        candidate=candidate,
        linkedin_profile=fake_session.linkedin_profile,
        operator=operator,
    )
    assert not record_confirmed_withdrawal(
        candidate=candidate,
        linkedin_profile=fake_session.linkedin_profile,
        operator=operator,
    )

    deal.refresh_from_db()
    assert deal.state == ProfileState.FAILED
    assert deal.closing_reason == ClosingReason.FAILED
    assert deal.invitation_withdrawn_at is not None
    assert ActionLog.objects.filter(
        action_type=ActionLog.ActionType.WITHDRAW_INVITE,
    ).count() == 1


@pytest.mark.django_db
def test_legacy_success_backfills_positive_ledger(fake_session):
    operator = resolve_operator(
        fake_session.linkedin_profile.linkedin_username,
    )
    deal = _pending_deal(
        fake_session,
        "legacy-backfill",
        sent_note="Project sent note",
    )
    transition = timezone.now() - timedelta(days=90)
    log = _legacy_evidence(
        fake_session,
        deal,
        transition_at=transition,
    )
    candidate = _plan(
        fake_session,
        before=date.today() + timedelta(days=1),
    ).candidates[0]

    assert candidate.legacy_action_log_id == log.pk
    assert record_confirmed_withdrawal(
        candidate=candidate,
        linkedin_profile=fake_session.linkedin_profile,
        operator=operator,
    )

    deal.refresh_from_db()
    assert deal.invitation_sent_at == log.created_at
    assert deal.invitation_sender == operator


@pytest.mark.django_db
def test_post_click_database_write_retries_once(fake_session):
    operator = resolve_operator(
        fake_session.linkedin_profile.linkedin_username,
    )
    _pending_deal(
        fake_session,
        "retry",
        sent_at=timezone.now() - timedelta(days=90),
        sender=operator,
    )
    candidate = _plan(
        fake_session,
        before=date.today() + timedelta(days=1),
    ).candidates[0]

    from django.db.utils import OperationalError

    with (
        patch(
            "linkedin.invitation_withdrawal._persist_confirmed_withdrawal",
            side_effect=[OperationalError("dead"), True],
        ) as persist,
        patch("linkedin.invitation_withdrawal.connections.close_all") as close_all,
    ):
        assert record_confirmed_withdrawal(
            candidate=candidate,
            linkedin_profile=fake_session.linkedin_profile,
            operator=operator,
        )

    assert persist.call_count == 2
    close_all.assert_called_once()


def test_legacy_window_is_intentionally_narrow():
    assert LEGACY_EVIDENCE_WINDOW_SECONDS == 10
