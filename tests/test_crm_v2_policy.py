from datetime import date, timedelta

import pytest

from linkedin.crm_v2_policy import (
    AccountPolicyFacts,
    AdmissionReasonCode,
    AdmissionStatus,
    COMPLETED_MEETING_ADMISSION_MAX_AGE_DAYS,
    ConversationEvidence,
    EvidenceTier,
    GMAIL_ADMISSION_MAX_AGE_DAYS,
    LINKEDIN_ADMISSION_MAX_AGE_DAYS,
    PolicyFlag,
    Priority,
    ReminderReasonCode,
    ReminderState,
    evaluate_account,
)


TODAY = date(2026, 8, 26)


def _gmail_inbound(*, days_ago: int = 0, replied: bool = False):
    inbound_on = TODAY - timedelta(days=days_ago)
    return ConversationEvidence(
        human_inbound_count=1,
        substantive_inbound_count=1,
        outbound_count=int(replied),
        latest_human_inbound_on=inbound_on,
        latest_substantive_inbound_on=inbound_on,
        latest_outbound_on=inbound_on if replied else None,
    )


def _linkedin_conversation(*, days_ago: int = 0, answered: bool = False):
    inbound_on = TODAY - timedelta(days=days_ago)
    return ConversationEvidence(
        human_inbound_count=1,
        substantive_inbound_count=1,
        outbound_count=2 if answered else 1,
        latest_human_inbound_on=inbound_on,
        latest_substantive_inbound_on=inbound_on,
        latest_outbound_on=(
            inbound_on
            if answered
            else inbound_on - timedelta(days=1)
        ),
    )


def test_manual_pin_and_sales_motion_are_authoritative_and_deterministic():
    decision = evaluate_account(
        AccountPolicyFacts(
            account_key="account:ramp",
            manual_pin=True,
            sales_motion_active=True,
            gmail=_gmail_inbound(replied=True),
        ),
        today=TODAY,
    )

    assert decision.status == AdmissionStatus.ACTIVE_ACCOUNT
    assert decision.primary_reason_code == AdmissionReasonCode.MANUAL_PIN
    assert decision.reason_codes == (
        AdmissionReasonCode.MANUAL_PIN,
        AdmissionReasonCode.SALES_MOTION_ACTIVE,
        AdmissionReasonCode.RECENT_GMAIL_BIDIRECTIONAL_THREAD,
    )
    assert decision.evidence_tier == EvidenceTier.AUTHORITATIVE
    assert decision.confidence_score == 100


@pytest.mark.parametrize(
    ("facts", "reason", "confidence"),
    [
        (
            AccountPolicyFacts(
                account_key="meeting:upcoming",
                upcoming_external_meeting_on=TODAY + timedelta(days=4),
            ),
            AdmissionReasonCode.UPCOMING_EXTERNAL_MEETING,
            98,
        ),
        (
            AccountPolicyFacts(
                account_key="meeting:completed",
                latest_completed_external_meeting_on=TODAY - timedelta(days=30),
            ),
            AdmissionReasonCode.RECENT_COMPLETED_EXTERNAL_MEETING,
            95,
        ),
        (
            AccountPolicyFacts(
                account_key="gmail:human",
                gmail=_gmail_inbound(),
            ),
            AdmissionReasonCode.RECENT_GMAIL_HUMAN_INBOUND,
            90,
        ),
        (
            AccountPolicyFacts(
                account_key="gmail:thread",
                gmail=_gmail_inbound(replied=True),
            ),
            AdmissionReasonCode.RECENT_GMAIL_BIDIRECTIONAL_THREAD,
            93,
        ),
    ],
)
def test_external_meetings_and_real_gmail_are_primary(facts, reason, confidence):
    decision = evaluate_account(facts, today=TODAY)

    assert decision.admitted
    assert decision.primary_reason_code == reason
    assert decision.evidence_tier == EvidenceTier.PRIMARY
    assert decision.confidence_score == confidence


def test_substantive_bidirectional_linkedin_is_secondary():
    decision = evaluate_account(
        AccountPolicyFacts(
            account_key="linkedin:real-conversation",
            linkedin=_linkedin_conversation(answered=True),
        ),
        today=TODAY,
    )

    assert decision.admitted
    assert decision.primary_reason_code == (
        AdmissionReasonCode.RECENT_LINKEDIN_SUBSTANTIVE_BIDIRECTIONAL
    )
    assert decision.evidence_tier == EvidenceTier.SECONDARY
    assert decision.confidence_score == 72
    assert decision.reminder.state == ReminderState.WAITING
    assert decision.priority == Priority.LOW


def test_answered_gmail_waits_then_becomes_a_real_followup_reminder():
    waiting = evaluate_account(
        AccountPolicyFacts(
            account_key="gmail:waiting",
            gmail=ConversationEvidence(
                human_inbound_count=1,
                substantive_inbound_count=1,
                outbound_count=1,
                latest_human_inbound_on=TODAY - timedelta(days=1),
                latest_substantive_inbound_on=TODAY - timedelta(days=1),
                latest_outbound_on=TODAY,
            ),
        ),
        today=TODAY,
    )
    due = evaluate_account(
        AccountPolicyFacts(
            account_key="gmail:due",
            gmail=ConversationEvidence(
                human_inbound_count=1,
                substantive_inbound_count=1,
                outbound_count=1,
                latest_human_inbound_on=TODAY - timedelta(days=8),
                latest_substantive_inbound_on=TODAY - timedelta(days=8),
                latest_outbound_on=TODAY - timedelta(days=5),
            ),
        ),
        today=TODAY,
    )

    assert waiting.reminder.state == ReminderState.WAITING
    assert waiting.reminder.reason_code == ReminderReasonCode.WAITING_FOR_GMAIL_REPLY
    assert waiting.reminder.due_on == TODAY + timedelta(days=4)
    assert not waiting.reminder.should_create_reminder
    assert due.reminder.state == ReminderState.FOLLOW_UP_DUE
    assert due.reminder.reason_code == ReminderReasonCode.GMAIL_FOLLOW_UP_DUE
    assert due.reminder.should_create_reminder


def test_old_qualifying_conversation_stays_active_without_cluttering_actions():
    decision = evaluate_account(
        AccountPolicyFacts(
            account_key="gmail:old-active",
            gmail=ConversationEvidence(
                human_inbound_count=1,
                substantive_inbound_count=1,
                latest_human_inbound_on=TODAY - timedelta(days=45),
                latest_substantive_inbound_on=TODAY - timedelta(days=45),
            ),
        ),
        today=TODAY,
    )

    assert decision.admitted
    assert decision.reminder.state == ReminderState.REVIEW
    assert decision.reminder.reason_code == (
        ReminderReasonCode.OLD_GMAIL_CONVERSATION_REVIEW
    )
    assert not decision.reminder.should_create_reminder


def test_authoritative_old_primary_relationship_gets_one_low_priority_review():
    decision = evaluate_account(
        AccountPolicyFacts(
            account_key="gmail:old-authoritative",
            manual_pin=True,
            gmail=_gmail_inbound(days_ago=45, replied=True),
        ),
        today=TODAY,
    )

    assert decision.admitted
    assert decision.reminder.state == ReminderState.REVIEW
    assert decision.reminder.should_create_reminder
    assert decision.reminder.due_on == TODAY
    assert decision.priority == Priority.LOW


def test_meeting_and_human_gmail_corroborate_an_old_recovery_review():
    decision = evaluate_account(
        AccountPolicyFacts(
            account_key="prescient-like",
            latest_completed_external_meeting_on=TODAY - timedelta(days=45),
            gmail=_gmail_inbound(days_ago=45, replied=True),
            post_meeting_followup_required=True,
        ),
        today=TODAY,
    )

    assert decision.admitted
    assert decision.reminder.state == ReminderState.REVIEW
    assert decision.reminder.should_create_reminder
    assert decision.reminder.due_on == TODAY
    assert decision.priority == Priority.LOW


def test_meeting_only_and_linkedin_only_do_not_create_recovery_work():
    meeting_only = evaluate_account(
        AccountPolicyFacts(
            account_key="cloudflare-like",
            latest_completed_external_meeting_on=TODAY - timedelta(days=45),
            post_meeting_followup_required=True,
        ),
        today=TODAY,
    )
    authoritative_linkedin_only = evaluate_account(
        AccountPolicyFacts(
            account_key="linkedin-only",
            manual_pin=True,
            linkedin=_linkedin_conversation(days_ago=45, answered=True),
        ),
        today=TODAY,
    )

    assert meeting_only.reminder.state == ReminderState.REVIEW
    assert not meeting_only.reminder.should_create_reminder
    assert authoritative_linkedin_only.reminder.state == ReminderState.REVIEW
    assert not authoritative_linkedin_only.reminder.should_create_reminder


@pytest.mark.parametrize(
    "facts",
    [
        AccountPolicyFacts(
            account_key="boundary:meeting",
            latest_completed_external_meeting_on=(
                TODAY
                - timedelta(days=COMPLETED_MEETING_ADMISSION_MAX_AGE_DAYS)
            ),
        ),
        AccountPolicyFacts(
            account_key="boundary:gmail",
            gmail=_gmail_inbound(days_ago=GMAIL_ADMISSION_MAX_AGE_DAYS),
        ),
        AccountPolicyFacts(
            account_key="boundary:linkedin",
            linkedin=_linkedin_conversation(
                days_ago=LINKEDIN_ADMISSION_MAX_AGE_DAYS,
                answered=True,
            ),
        ),
    ],
)
def test_channel_specific_admission_windows_are_inclusive(facts):
    assert evaluate_account(facts, today=TODAY).admitted


@pytest.mark.parametrize(
    ("evidence", "reason"),
    [
        (
            ConversationEvidence(
                outbound_count=2,
                latest_outbound_on=TODAY,
            ),
            AdmissionReasonCode.LINKEDIN_OUTBOUND_ONLY,
        ),
        (
            ConversationEvidence(
                human_inbound_count=1,
                acknowledgement_inbound_count=1,
                outbound_count=1,
                latest_human_inbound_on=TODAY,
                latest_outbound_on=TODAY - timedelta(days=1),
            ),
            AdmissionReasonCode.LINKEDIN_ACKNOWLEDGEMENT_ONLY,
        ),
        (
            ConversationEvidence(
                human_inbound_count=1,
                polite_decline_inbound_count=1,
                outbound_count=1,
                latest_human_inbound_on=TODAY,
                latest_outbound_on=TODAY - timedelta(days=1),
            ),
            AdmissionReasonCode.LINKEDIN_POLITE_DECLINE_ONLY,
        ),
        (
            ConversationEvidence(automated_inbound_count=1),
            AdmissionReasonCode.LINKEDIN_AUTOMATED_ONLY,
        ),
        (
            ConversationEvidence(connection_event_count=1),
            AdmissionReasonCode.LINKEDIN_CONNECTION_EVENT_ONLY,
        ),
        (
            ConversationEvidence(
                human_inbound_count=1,
                substantive_inbound_count=1,
                latest_human_inbound_on=TODAY,
                latest_substantive_inbound_on=TODAY,
            ),
            AdmissionReasonCode.LINKEDIN_NOT_BIDIRECTIONAL,
        ),
    ],
)
def test_weak_linkedin_signals_remain_people_only(evidence, reason):
    decision = evaluate_account(
        AccountPolicyFacts(
            account_key=f"weak:{reason.value}",
            linkedin=evidence,
        ),
        today=TODAY,
    )

    assert decision.status == AdmissionStatus.PEOPLE_ONLY
    assert decision.primary_reason_code == reason
    assert decision.evidence_tier == EvidenceTier.WEAK
    assert decision.priority == Priority.NONE
    assert decision.reminder.state == ReminderState.NONE
    assert not decision.reminder.should_create_reminder


@pytest.mark.parametrize(
    ("facts", "reason", "tier"),
    [
        (
            AccountPolicyFacts(
                account_key="stale:meeting",
                latest_completed_external_meeting_on=(
                    TODAY
                    - timedelta(
                        days=COMPLETED_MEETING_ADMISSION_MAX_AGE_DAYS + 1,
                    )
                ),
            ),
            AdmissionReasonCode.STALE_EXTERNAL_MEETING,
            EvidenceTier.PRIMARY,
        ),
        (
            AccountPolicyFacts(
                account_key="stale:gmail",
                gmail=_gmail_inbound(days_ago=GMAIL_ADMISSION_MAX_AGE_DAYS + 1),
            ),
            AdmissionReasonCode.STALE_GMAIL_HUMAN_ENGAGEMENT,
            EvidenceTier.PRIMARY,
        ),
        (
            AccountPolicyFacts(
                account_key="stale:linkedin",
                linkedin=_linkedin_conversation(
                    days_ago=LINKEDIN_ADMISSION_MAX_AGE_DAYS + 1,
                    answered=True,
                ),
            ),
            AdmissionReasonCode.STALE_LINKEDIN_SUBSTANTIVE_BIDIRECTIONAL,
            EvidenceTier.SECONDARY,
        ),
    ],
)
def test_stale_unpinned_evidence_remains_people_only(facts, reason, tier):
    decision = evaluate_account(facts, today=TODAY)

    assert not decision.admitted
    assert decision.primary_reason_code == reason
    assert decision.evidence_tier == tier
    assert decision.priority == Priority.NONE


def test_do_not_outreach_never_excludes_a_real_sales_relationship():
    decision = evaluate_account(
        AccountPolicyFacts(
            account_key="account:stackarmor",
            do_not_outreach=True,
            latest_completed_external_meeting_on=TODAY - timedelta(days=1),
            post_meeting_followup_required=True,
        ),
        today=TODAY,
    )

    assert decision.admitted
    assert decision.flags == (PolicyFlag.DO_NOT_OUTREACH,)
    assert not decision.automated_outreach_allowed
    assert decision.reminder.state == ReminderState.POST_MEETING_FOLLOWUP
    assert decision.reminder.should_create_reminder
    assert not decision.reminder.automated_outreach_allowed


def test_future_waiting_date_is_authoritative_over_other_reminders():
    decision = evaluate_account(
        AccountPolicyFacts(
            account_key="account:waiting",
            manual_pin=True,
            gmail=_gmail_inbound(),
            next_action_due_on=TODAY - timedelta(days=3),
            waiting_until=TODAY + timedelta(days=5),
        ),
        today=TODAY,
    )

    assert decision.reminder.state == ReminderState.WAITING
    assert decision.reminder.reason_code == ReminderReasonCode.WAITING_UNTIL
    assert decision.reminder.due_on == TODAY + timedelta(days=5)
    assert decision.priority == Priority.LOW


@pytest.mark.parametrize(
    ("facts", "state", "reason", "priority"),
    [
        (
            AccountPolicyFacts(
                account_key="reminder:overdue",
                manual_pin=True,
                next_action_due_on=TODAY - timedelta(days=1),
            ),
            ReminderState.OVERDUE_NEXT_ACTION,
            ReminderReasonCode.NEXT_ACTION_OVERDUE,
            Priority.URGENT,
        ),
        (
            AccountPolicyFacts(
                account_key="reminder:due",
                manual_pin=True,
                next_action_due_on=TODAY,
            ),
            ReminderState.DUE_TODAY,
            ReminderReasonCode.NEXT_ACTION_DUE_TODAY,
            Priority.HIGH,
        ),
        (
            AccountPolicyFacts(
                account_key="reminder:inbound",
                gmail=_gmail_inbound(),
            ),
            ReminderState.NEEDS_RESPONSE,
            ReminderReasonCode.UNANSWERED_GMAIL_HUMAN_INBOUND,
            Priority.URGENT,
        ),
        (
            AccountPolicyFacts(
                account_key="reminder:meeting",
                upcoming_external_meeting_on=TODAY + timedelta(days=3),
            ),
            ReminderState.MEETING_PREP,
            ReminderReasonCode.EXTERNAL_MEETING_IN_PREP_WINDOW,
            Priority.HIGH,
        ),
        (
            AccountPolicyFacts(
                account_key="reminder:post-meeting",
                latest_completed_external_meeting_on=TODAY - timedelta(days=1),
                post_meeting_followup_required=True,
            ),
            ReminderState.POST_MEETING_FOLLOWUP,
            ReminderReasonCode.POST_MEETING_FOLLOWUP_REQUIRED,
            Priority.HIGH,
        ),
        (
            AccountPolicyFacts(
                account_key="reminder:scheduled",
                manual_pin=True,
                next_action_due_on=TODAY + timedelta(days=4),
            ),
            ReminderState.SCHEDULED_NEXT_ACTION,
            ReminderReasonCode.NEXT_ACTION_SCHEDULED,
            Priority.LOW,
        ),
    ],
)
def test_reminder_states_have_stable_reason_and_priority(
    facts,
    state,
    reason,
    priority,
):
    decision = evaluate_account(facts, today=TODAY)

    assert decision.reminder.state == state
    assert decision.reminder.reason_code == reason
    assert decision.priority == priority


def test_far_future_meeting_waits_until_the_existing_prep_window():
    decision = evaluate_account(
        AccountPolicyFacts(
            account_key="meeting:far-future",
            upcoming_external_meeting_on=TODAY + timedelta(days=20),
        ),
        today=TODAY,
    )

    assert decision.admitted
    assert decision.reminder.state == ReminderState.WAITING
    assert decision.reminder.reason_code == (
        ReminderReasonCode.EXTERNAL_MEETING_OUTSIDE_PREP_WINDOW
    )
    assert decision.reminder.due_on == TODAY + timedelta(days=13)
    assert not decision.reminder.should_create_reminder


def test_conversation_shape_validation_fails_fast():
    with pytest.raises(ValueError, match="latest_human_inbound_on"):
        ConversationEvidence(human_inbound_count=1)
    with pytest.raises(ValueError, match="non-overlapping subsets"):
        ConversationEvidence(
            human_inbound_count=1,
            substantive_inbound_count=1,
            acknowledgement_inbound_count=1,
            latest_human_inbound_on=TODAY,
            latest_substantive_inbound_on=TODAY,
        )


def test_evaluation_rejects_future_channel_events_and_mislabeled_meetings():
    future = TODAY + timedelta(days=1)
    with pytest.raises(ValueError, match="gmail.latest_human_inbound_on"):
        evaluate_account(
            AccountPolicyFacts(
                account_key="invalid:gmail-future",
                gmail=ConversationEvidence(
                    human_inbound_count=1,
                    substantive_inbound_count=1,
                    latest_human_inbound_on=future,
                    latest_substantive_inbound_on=future,
                ),
            ),
            today=TODAY,
        )
    with pytest.raises(ValueError, match="upcoming_external_meeting_on"):
        evaluate_account(
            AccountPolicyFacts(
                account_key="invalid:past-upcoming",
                upcoming_external_meeting_on=TODAY - timedelta(days=1),
            ),
            today=TODAY,
        )
