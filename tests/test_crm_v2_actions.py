from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from crm.models import (
    Account,
    Lead,
    MeetingNote,
    Message,
    Opportunity,
    OpportunityAction,
    OpportunityContact,
    SalesOwner,
)
from linkedin.crm_v2_actions import (
    apply_action_reconciliation,
    dry_run_action_reconciliation,
)
from linkedin.crm_v2_evidence import ResolvedAccountEvidence
from linkedin.crm_v2_policy import (
    AccountPolicyFacts,
    ConversationEvidence,
    ReminderState,
    evaluate_account,
)


pytestmark = pytest.mark.django_db
NOW = datetime(2026, 8, 26, 15, tzinfo=UTC)


def _opportunity(
    name: str,
    *,
    owner=True,
    active=True,
    source=Opportunity.Source.SYSTEM,
):
    account = Account.objects.create(name=name)
    return Opportunity.objects.create(
        account=account,
        name=name,
        owner=SalesOwner.objects.get(handle="Arian") if owner else None,
        source=source,
        active_account=active,
    )


def _resolved(
    opportunity,
    facts,
    *,
    lead_ids=(),
    target_lead_id=None,
    trigger_message_id=None,
    trigger_meeting_id=None,
    trigger_meeting_note_id=None,
    commitment_description="",
    reminder_do_not_outreach=False,
):
    return ResolvedAccountEvidence(
        account_key=facts.account_key,
        account_name=opportunity.account.name,
        lead_ids=tuple(lead_ids),
        opportunity_id=str(opportunity.id),
        owner=opportunity.owner.handle if opportunity.owner else "",
        key_contacts=(),
        last_meaningful_touch=None,
        reminder_target_lead_id=target_lead_id,
        trigger_message_id=trigger_message_id,
        trigger_meeting_id=trigger_meeting_id,
        facts=facts,
        decision=evaluate_account(facts, today=NOW.date()),
        trigger_meeting_note_id=trigger_meeting_note_id,
        commitment_description=commitment_description,
        reminder_do_not_outreach=reminder_do_not_outreach,
    )


def _link(opportunity, lead):
    OpportunityContact.objects.create(
        opportunity=opportunity,
        lead=lead,
        role=OpportunityContact.Role.STAKEHOLDER,
    )


def test_opportunity_lock_is_scoped_away_from_nullable_owner_join():
    opportunity = _opportunity("Scoped Lock")
    facts = AccountPolicyFacts(
        account_key="scoped lock",
        sales_motion_active=True,
    )
    evidence = _resolved(opportunity, facts)
    manager = Opportunity.objects
    original_select_for_update = manager.select_for_update
    lock_calls = []

    def scoped_select_for_update(*args, **kwargs):
        lock_calls.append((args, kwargs))
        return original_select_for_update(*args, **kwargs)

    with patch.object(
        manager,
        "select_for_update",
        side_effect=scoped_select_for_update,
    ):
        apply_action_reconciliation([evidence], evaluated_at=NOW)

    assert lock_calls == [((), {"of": ("self",)})]


def test_authoritative_sales_motion_without_calendar_or_gmail_gets_no_action():
    opportunity = _opportunity(
        "Ramp",
        source=Opportunity.Source.SHEET,
    )
    facts = AccountPolicyFacts(account_key="ramp", sales_motion_active=True)
    evidence = _resolved(opportunity, facts)

    report = apply_action_reconciliation([evidence], evaluated_at=NOW)

    assert evidence.decision.admitted is True
    assert evidence.decision.reminder.state == ReminderState.NONE
    assert report.actions_created == 0
    assert not OpportunityAction.objects.filter(opportunity=opportunity).exists()


def test_exact_gmail_inbound_target_and_trigger_are_preserved():
    opportunity = _opportunity("Exact Gmail")
    lead = Lead.objects.create(
        first_name="Exact",
        last_name="Contact",
        company_name="Exact Gmail",
        email="exact@example-company.test",
    )
    _link(opportunity, lead)
    inbound_at = NOW - timedelta(hours=1)
    message = Message.objects.create(
        lead=lead,
        source=Message.Source.GMAIL,
        external_id="gmail-exact-inbound",
        direction=Message.Direction.INBOUND,
        sender=lead.email,
        body="Can we discuss the sandbox tomorrow?",
        sent_at=inbound_at,
    )
    gmail = ConversationEvidence(
        human_inbound_count=1,
        substantive_inbound_count=1,
        outbound_count=1,
        latest_human_inbound_on=inbound_at,
        latest_substantive_inbound_on=inbound_at,
        latest_outbound_on=NOW - timedelta(days=1),
    )
    facts = AccountPolicyFacts(account_key="exact gmail", gmail=gmail)
    evidence = _resolved(
        opportunity,
        facts,
        lead_ids=(lead.id,),
        target_lead_id=lead.id,
        trigger_message_id=message.id,
    )

    report = apply_action_reconciliation([evidence], evaluated_at=NOW)

    action = OpportunityAction.objects.get(opportunity=opportunity)
    assert report.issues == []
    assert action.kind == OpportunityAction.Kind.NEEDS_RESPONSE
    assert action.target_lead == lead
    assert action.trigger_message == message
    assert action.channel == "email"
    assert action.draft == ""

    refreshed_facts = AccountPolicyFacts(
        account_key="exact gmail",
        gmail=gmail,
        next_action_due_on=NOW.date(),
    )
    refreshed = _resolved(
        opportunity,
        refreshed_facts,
        lead_ids=(lead.id,),
        target_lead_id=lead.id,
        trigger_message_id=message.id,
    )
    assert refreshed.decision.reminder.state == ReminderState.DUE_TODAY

    repeated = apply_action_reconciliation([refreshed], evaluated_at=NOW)

    action.refresh_from_db()
    assert repeated.actions_unchanged == 1
    assert action.kind == OpportunityAction.Kind.NEEDS_RESPONSE
    assert action.description == "Respond to the latest human inbound"
    assert action.channel == "email"


def test_unedited_legacy_system_task_is_replaced_by_exact_v2_work():
    opportunity = _opportunity("Replace legacy generated")
    target = Lead.objects.create(
        first_name="Exact",
        company_name="Replace legacy generated",
        email="exact@replace-legacy.example",
    )
    _link(opportunity, target)
    legacy = OpportunityAction.objects.create(
        opportunity=opportunity,
        kind=OpportunityAction.Kind.NEXT_STEP,
        description="Old generated follow-up",
        idempotency_key="system:legacy-generated",
    )
    inbound_at = NOW - timedelta(hours=1)
    message = Message.objects.create(
        lead=target,
        source=Message.Source.GMAIL,
        external_id="replace-legacy-inbound",
        thread_external_id="replace-legacy-thread",
        direction=Message.Direction.INBOUND,
        body="Can we review the proposal?",
        sent_at=inbound_at,
    )
    facts = AccountPolicyFacts(
        account_key="replace legacy generated",
        gmail=ConversationEvidence(
            human_inbound_count=1,
            substantive_inbound_count=1,
            latest_human_inbound_on=inbound_at,
            latest_substantive_inbound_on=inbound_at,
        ),
    )
    evidence = _resolved(
        opportunity,
        facts,
        lead_ids=(target.id,),
        target_lead_id=target.id,
        trigger_message_id=message.id,
    )

    report = apply_action_reconciliation([evidence], evaluated_at=NOW)

    legacy.refresh_from_db()
    current = OpportunityAction.objects.get(
        opportunity=opportunity,
        status=OpportunityAction.Status.OPEN,
    )
    assert report.actions_cancelled == 1
    assert report.actions_created == 1
    assert legacy.status == OpportunityAction.Status.CANCELLED
    assert current.idempotency_key.startswith("v2:")
    assert current.target_lead == target


def test_unlinked_exact_target_fails_closed_without_guessing():
    opportunity = _opportunity("Wrong target")
    linked = Lead.objects.create(
        first_name="Linked",
        company_name="Wrong target",
        email="linked@wrong-target.test",
    )
    unlinked = Lead.objects.create(
        first_name="Unlinked",
        company_name="Wrong target",
        email="unlinked@wrong-target.test",
    )
    _link(opportunity, linked)
    inbound_at = NOW - timedelta(hours=1)
    gmail = ConversationEvidence(
        human_inbound_count=1,
        substantive_inbound_count=1,
        latest_human_inbound_on=inbound_at,
        latest_substantive_inbound_on=inbound_at,
    )
    facts = AccountPolicyFacts(account_key="wrong target", gmail=gmail)
    evidence = _resolved(
        opportunity,
        facts,
        lead_ids=(linked.id, unlinked.id),
        target_lead_id=unlinked.id,
    )

    report = apply_action_reconciliation([evidence], evaluated_at=NOW)

    assert OpportunityAction.objects.filter(opportunity=opportunity).count() == 0
    assert report.issues[0].reason == "reminder_target_not_linked_to_opportunity"


def test_stackarmor_dno_keeps_targetless_manual_reminder_but_no_delivery_fields():
    opportunity = _opportunity(
        "stackArmor",
        source=Opportunity.Source.MANUAL,
    )
    facts = AccountPolicyFacts(
        account_key="stackarmor",
        manual_pin=True,
        do_not_outreach=True,
        latest_completed_external_meeting_on=NOW.date() - timedelta(days=1),
        post_meeting_followup_required=True,
    )
    evidence = _resolved(opportunity, facts)

    report = apply_action_reconciliation([evidence], evaluated_at=NOW)

    action = OpportunityAction.objects.get(opportunity=opportunity)
    assert report.actions_created == 1
    assert evidence.decision.admitted is True
    assert evidence.decision.reminder.automated_outreach_allowed is False
    assert action.target_lead is None
    assert action.channel == ""
    assert action.draft == ""


def test_mixed_account_dno_target_gets_no_delivery_channel():
    opportunity = _opportunity("Mixed target DNO")
    target = Lead.objects.create(
        first_name="Stopped",
        company_name="Mixed target DNO",
        email="stopped@mixed-target.example",
    )
    _link(opportunity, target)
    inbound_at = NOW - timedelta(hours=1)
    message = Message.objects.create(
        lead=target,
        source=Message.Source.GMAIL,
        external_id="mixed-dno-inbound",
        thread_external_id="mixed-dno-thread",
        direction=Message.Direction.INBOUND,
        body="Can you send the details?",
        sent_at=inbound_at,
    )
    facts = AccountPolicyFacts(
        account_key="mixed target dno",
        gmail=ConversationEvidence(
            human_inbound_count=1,
            substantive_inbound_count=1,
            latest_human_inbound_on=inbound_at,
            latest_substantive_inbound_on=inbound_at,
        ),
    )
    evidence = _resolved(
        opportunity,
        facts,
        lead_ids=(target.id,),
        target_lead_id=target.id,
        trigger_message_id=message.id,
        reminder_do_not_outreach=True,
    )

    apply_action_reconciliation([evidence], evaluated_at=NOW)

    action = OpportunityAction.objects.get(opportunity=opportunity)
    assert evidence.facts.do_not_outreach is False
    assert action.target_lead == target
    assert action.channel == ""
    assert action.draft == ""


def test_unowned_opportunity_does_not_create_sender_work():
    opportunity = _opportunity("Unowned", owner=False)
    facts = AccountPolicyFacts(account_key="unowned", manual_pin=True)
    evidence = _resolved(opportunity, facts)

    report = apply_action_reconciliation([evidence], evaluated_at=NOW)

    assert report.unowned_skipped == 1
    assert OpportunityAction.objects.filter(opportunity=opportunity).count() == 0


def test_old_review_state_cancels_only_replaceable_v2_action():
    opportunity = _opportunity("Old review")
    old_at = NOW - timedelta(days=45)
    gmail = ConversationEvidence(
        human_inbound_count=1,
        substantive_inbound_count=1,
        outbound_count=1,
        latest_human_inbound_on=old_at,
        latest_substantive_inbound_on=old_at,
        latest_outbound_on=old_at - timedelta(hours=1),
    )
    facts = AccountPolicyFacts(
        account_key="old review",
        manual_pin=True,
        gmail=gmail,
    )
    evidence = _resolved(opportunity, facts)
    old_action = OpportunityAction.objects.create(
        opportunity=opportunity,
        description="Old v2 work",
        idempotency_key=f"v2:{opportunity.id}:old",
    )
    assert evidence.decision.reminder.state == ReminderState.REVIEW

    report = apply_action_reconciliation([evidence], evaluated_at=NOW)

    old_action.refresh_from_db()
    assert report.actions_cancelled == 1
    assert old_action.status == OpportunityAction.Status.CANCELLED
    assert OpportunityAction.objects.filter(
        opportunity=opportunity,
        status__in=[OpportunityAction.Status.OPEN, OpportunityAction.Status.WAITING],
    ).count() == 0


@pytest.mark.parametrize(
    ("idempotency_key", "human_revision"),
    (("manual:keep", 0), ("v2:human-edited", 1)),
)
def test_non_v2_and_human_edited_current_actions_are_preserved(
    idempotency_key,
    human_revision,
):
    opportunity = _opportunity(f"Preserve {idempotency_key}")
    facts = AccountPolicyFacts(
        account_key=f"preserve {idempotency_key}",
        manual_pin=True,
    )
    evidence = _resolved(opportunity, facts)
    action = OpportunityAction.objects.create(
        opportunity=opportunity,
        description="Human's exact task",
        due_on=NOW.date() + timedelta(days=7),
        idempotency_key=idempotency_key,
        human_revision=human_revision,
    )

    report = apply_action_reconciliation([evidence], evaluated_at=NOW)

    action.refresh_from_db()
    assert report.human_actions_preserved == 1
    assert report.actions_created == 0
    assert report.actions_updated == 0
    assert report.actions_cancelled == 0
    assert action.status == OpportunityAction.Status.OPEN
    assert action.description == "Human's exact task"
    assert action.due_on == NOW.date() + timedelta(days=7)


def test_dry_run_matches_apply_and_repeated_apply_is_idempotent():
    opportunity = _opportunity("Idempotent")
    facts = AccountPolicyFacts(
        account_key="idempotent",
        manual_pin=True,
        latest_completed_external_meeting_on=NOW.date() - timedelta(days=1),
        post_meeting_followup_required=True,
    )
    evidence = _resolved(opportunity, facts)

    dry_run = dry_run_action_reconciliation([evidence], evaluated_at=NOW)
    assert OpportunityAction.objects.filter(opportunity=opportunity).count() == 0

    applied = apply_action_reconciliation([evidence], evaluated_at=NOW)
    action = OpportunityAction.objects.get(opportunity=opportunity)

    assert dry_run.actions_created == applied.actions_created == 1
    assert dry_run.actions_cancelled == applied.actions_cancelled == 0
    assert dry_run.changes == applied.changes
    assert str(action.id) == applied.changes[0].action_id

    repeated = apply_action_reconciliation([evidence], evaluated_at=NOW)
    action.refresh_from_db()
    assert repeated.actions_created == 0
    assert repeated.actions_updated == 0
    assert repeated.actions_unchanged == 1
    assert str(action.id) == applied.changes[0].action_id
    assert OpportunityAction.objects.filter(opportunity=opportunity).count() == 1


def test_active_accounts_scope_creates_only_in_scope_and_retires_generated_work():
    in_scope = _opportunity("In Active Accounts")
    generated_outside = _opportunity("Generated Outside Active Accounts")
    human_outside = _opportunity("Human Outside Active Accounts")
    generated_action = OpportunityAction.objects.create(
        opportunity=generated_outside,
        description="Generated task outside the manual ledger",
        idempotency_key=(
            f"v2:{generated_outside.id}:account:authoritative-next-step"
        ),
    )
    human_action = OpportunityAction.objects.create(
        opportunity=human_outside,
        description="Human task outside the manual ledger",
        idempotency_key="human:outside-active-accounts",
    )

    def evidence(opportunity):
        facts = AccountPolicyFacts(
            account_key=opportunity.account.normalized_name,
            manual_pin=True,
            latest_completed_external_meeting_on=NOW.date() - timedelta(days=1),
            post_meeting_followup_required=True,
        )
        return _resolved(opportunity, facts)

    report = apply_action_reconciliation(
        [evidence(in_scope), evidence(generated_outside), evidence(human_outside)],
        evaluated_at=NOW,
        active_account_opportunity_ids=[in_scope.id],
    )

    generated_action.refresh_from_db()
    human_action.refresh_from_db()
    assert report.active_account_scope_enforced is True
    assert report.active_account_scope_rows == 1
    assert report.out_of_scope_rows == 2
    assert report.actions_created == 1
    assert report.actions_cancelled == 1
    assert report.human_actions_preserved == 1
    assert generated_action.status == OpportunityAction.Status.CANCELLED
    assert human_action.status == OpportunityAction.Status.OPEN
    assert OpportunityAction.objects.filter(
        opportunity=in_scope,
        status=OpportunityAction.Status.OPEN,
    ).count() == 1


def test_calendar_action_remains_stable_after_fresh_due_recollection():
    opportunity = _opportunity("Primary account-level")
    initial_facts = AccountPolicyFacts(
        account_key="primary account level",
        manual_pin=True,
        latest_completed_external_meeting_on=NOW.date() - timedelta(days=1),
        post_meeting_followup_required=True,
    )
    initial = _resolved(opportunity, initial_facts)
    assert initial.decision.reminder.state == ReminderState.POST_MEETING_FOLLOWUP

    apply_action_reconciliation([initial], evaluated_at=NOW)
    action = OpportunityAction.objects.get(opportunity=opportunity)
    stable_id = action.id
    stable_key = action.idempotency_key
    assert action.target_lead is None

    # A fresh evidence collection sees the current action's due date.  That
    # state transition must update the same account-level task, not reject it
    # for lacking a guessed contact or manufacture another action.
    refreshed_facts = AccountPolicyFacts(
        account_key="primary account level",
        manual_pin=True,
        latest_completed_external_meeting_on=NOW.date() - timedelta(days=1),
        post_meeting_followup_required=True,
        next_action_due_on=NOW.date(),
    )
    refreshed = _resolved(opportunity, refreshed_facts)
    assert refreshed.decision.reminder.state == ReminderState.DUE_TODAY

    report = apply_action_reconciliation([refreshed], evaluated_at=NOW)

    action.refresh_from_db()
    assert report.actions_unchanged == 1
    assert action.id == stable_id
    assert action.idempotency_key == stable_key
    assert action.description == "Complete the post-meeting follow-up"
    assert OpportunityAction.objects.filter(opportunity=opportunity).count() == 1


def test_note_commitment_fails_closed_when_linked_to_another_opportunity():
    opportunity = _opportunity("Expected Account")
    other = _opportunity("Other Account")
    note = MeetingNote.objects.create(
        source=MeetingNote.Source.GRANOLA,
        external_id="wrong-opportunity-note",
        opportunity=other,
        scheduled_start_at=NOW - timedelta(days=1),
        summary_markdown="- Send pricing (Arian)",
        detail_status=MeetingNote.DetailStatus.COMPLETE,
        match_status=MeetingNote.MatchStatus.MATCHED,
        match_method=MeetingNote.MatchMethod.MANUAL,
    )
    facts = AccountPolicyFacts(
        account_key="expected account",
        manual_pin=True,
        latest_completed_external_meeting_on=NOW.date() - timedelta(days=1),
        post_meeting_followup_required=True,
    )
    evidence = _resolved(
        opportunity,
        facts,
        trigger_meeting_note_id=str(note.id),
        commitment_description="Send pricing",
    )

    report = apply_action_reconciliation([evidence], evaluated_at=NOW)

    assert report.actions_created == 0
    assert [issue.reason for issue in report.issues] == [
        "trigger_meeting_note_not_linked_to_opportunity"
    ]
    assert not OpportunityAction.objects.filter(opportunity=opportunity).exists()


def test_inactive_opportunity_cancels_v2_action_without_touching_account():
    opportunity = _opportunity("Inactive", active=False)
    facts = AccountPolicyFacts(account_key="inactive", manual_pin=True)
    evidence = _resolved(opportunity, facts)
    action = OpportunityAction.objects.create(
        opportunity=opportunity,
        description="Should disappear from Actions",
        idempotency_key=f"v2:{opportunity.id}:account:authoritative-next-step",
    )

    report = apply_action_reconciliation([evidence], evaluated_at=NOW)

    opportunity.refresh_from_db()
    action.refresh_from_db()
    assert report.ineligible_rows == 1
    assert report.actions_cancelled == 1
    assert opportunity.active_account is False
    assert action.status == OpportunityAction.Status.CANCELLED


def test_people_only_row_without_opportunity_is_a_normal_noop():
    facts = AccountPolicyFacts(account_key="ordinary people only")
    evidence = ResolvedAccountEvidence(
        account_key=facts.account_key,
        account_name="Ordinary People Only",
        lead_ids=(),
        opportunity_id="",
        owner="",
        key_contacts=(),
        last_meaningful_touch=None,
        reminder_target_lead_id=None,
        trigger_message_id=None,
        trigger_meeting_id=None,
        facts=facts,
        decision=evaluate_account(facts, today=NOW.date()),
    )

    report = apply_action_reconciliation([evidence], evaluated_at=NOW)

    assert report.issues == []
    assert report.ineligible_rows == 1
    assert report.actions_created == 0
    assert report.actions_cancelled == 0


def test_people_only_row_with_prior_opportunity_cancels_stale_v2_action():
    opportunity = _opportunity("Formerly active account")
    action = OpportunityAction.objects.create(
        opportunity=opportunity,
        description="Stale generated follow-up",
        idempotency_key=f"v2:{opportunity.id}:account:authoritative-next-step",
    )
    facts = AccountPolicyFacts(account_key="formerly active account")
    evidence = _resolved(opportunity, facts)
    assert evidence.decision.admitted is False

    report = apply_action_reconciliation([evidence], evaluated_at=NOW)

    action.refresh_from_db()
    assert report.issues == []
    assert report.ineligible_rows == 1
    assert report.actions_cancelled == 1
    assert action.status == OpportunityAction.Status.CANCELLED


def test_admitted_row_without_reconciled_opportunity_still_fails_closed():
    facts = AccountPolicyFacts(
        account_key="missing admitted opportunity",
        manual_pin=True,
    )
    evidence = ResolvedAccountEvidence(
        account_key=facts.account_key,
        account_name="Missing Admitted Opportunity",
        lead_ids=(),
        opportunity_id="",
        owner="Arian",
        key_contacts=(),
        last_meaningful_touch=None,
        reminder_target_lead_id=None,
        trigger_message_id=None,
        trigger_meeting_id=None,
        facts=facts,
        decision=evaluate_account(facts, today=NOW.date()),
    )

    report = apply_action_reconciliation([evidence], evaluated_at=NOW)

    assert [issue.reason for issue in report.issues] == [
        "missing_opportunity_id"
    ]
    assert report.actions_created == 0


def test_cancelled_v2_action_is_reused_with_the_same_stable_id():
    opportunity = _opportunity("Reusable")
    facts = AccountPolicyFacts(
        account_key="reusable",
        manual_pin=True,
        latest_completed_external_meeting_on=NOW.date() - timedelta(days=1),
        post_meeting_followup_required=True,
    )
    evidence = _resolved(opportunity, facts)

    apply_action_reconciliation([evidence], evaluated_at=NOW)
    action = OpportunityAction.objects.get(opportunity=opportunity)
    stable_id = action.id
    action.status = OpportunityAction.Status.CANCELLED
    action.save(update_fields={"status"})

    report = apply_action_reconciliation([evidence], evaluated_at=NOW)

    action.refresh_from_db()
    assert report.actions_reused == 1
    assert action.id == stable_id
    assert action.status == OpportunityAction.Status.OPEN
    assert OpportunityAction.objects.filter(opportunity=opportunity).count() == 1
