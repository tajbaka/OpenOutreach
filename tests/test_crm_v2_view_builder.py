from datetime import datetime, timedelta, UTC

import pytest

from crm.models import Account, Lead, Opportunity, OpportunityAction, OpportunityContact, SalesOwner
from linkedin.crm_v2_evidence import ResolvedAccountEvidence
from linkedin.crm_v2_policy import AccountPolicyFacts, ConversationEvidence, evaluate_account
from linkedin.crm_v2_view_builder import build_crm_v2_database_view
from linkedin.notifications import crm_v2_sheets as sheet


pytestmark = pytest.mark.django_db
NOW = datetime(2026, 8, 26, 15, tzinfo=UTC)


def _evidence(
    opportunity,
    *,
    facts,
    target=None,
    reminder_do_not_outreach=False,
):
    decision = evaluate_account(facts, today=NOW.date())
    return ResolvedAccountEvidence(
        account_key=facts.account_key,
        account_name=opportunity.account.name,
        lead_ids=tuple(opportunity.contacts.values_list("lead_id", flat=True)),
        opportunity_id=str(opportunity.id),
        owner=opportunity.owner.handle if opportunity.owner_id else "",
        key_contacts=(target.full_name,) if target is not None else (),
        last_meaningful_touch=NOW - timedelta(hours=1),
        reminder_target_lead_id=target.id if target is not None else None,
        trigger_message_id=None,
        trigger_meeting_id=None,
        facts=facts,
        decision=decision,
        reminder_do_not_outreach=reminder_do_not_outreach,
    )


def test_builder_emits_one_active_account_and_only_due_now_action():
    owner = SalesOwner.objects.get(handle="Arian")
    account = Account.objects.create(name="Ramp", domain="ramp.com")
    opportunity = Opportunity.objects.create(
        account=account,
        owner=owner,
        name="Ramp",
        source=Opportunity.Source.SHEET,
        active_account=True,
    )
    target = Lead.objects.create(first_name="Zelia", company_name="Ramp", email="z@ramp.com")
    OpportunityContact.objects.create(opportunity=opportunity, lead=target)
    inbound_at = NOW - timedelta(hours=1)
    facts = AccountPolicyFacts(
        account_key="ramp",
        gmail=ConversationEvidence(
            human_inbound_count=1,
            substantive_inbound_count=1,
            outbound_count=1,
            latest_human_inbound_on=inbound_at,
            latest_substantive_inbound_on=inbound_at,
            latest_outbound_on=NOW - timedelta(days=1),
        ),
    )
    action = OpportunityAction.objects.create(
        opportunity=opportunity,
        target_lead=target,
        kind=OpportunityAction.Kind.NEEDS_RESPONSE,
        description="Reply to Zelia",
        due_on=NOW.date(),
        idempotency_key="v2:reply:test",
    )

    view = build_crm_v2_database_view([_evidence(opportunity, facts=facts, target=target)])

    assert len(view.rows.active_accounts) == 1
    assert len(view.rows.actions) == 1
    assert view.rows.active_accounts[0][sheet.COL_ATTENTION] == "Now"
    assert view.rows.actions[0][sheet.COL_ACTION_ID] == str(action.id)
    assert view.rows.actions[0][sheet.COL_LEAD_ID] == str(target.id)


def test_builder_keeps_waiting_account_out_of_actions_and_marks_dno():
    owner = SalesOwner.objects.get(handle="Arian")
    account = Account.objects.create(name="StackArmor")
    opportunity = Opportunity.objects.create(
        account=account,
        owner=owner,
        name="StackArmor",
        manual_pin=True,
        source=Opportunity.Source.MANUAL,
        active_account=True,
    )
    facts = AccountPolicyFacts(
        account_key="stackarmor",
        manual_pin=True,
        do_not_outreach=True,
        waiting_until=NOW.date() + timedelta(days=5),
    )

    view = build_crm_v2_database_view([_evidence(opportunity, facts=facts)])

    assert len(view.rows.actions) == 0
    row = view.rows.active_accounts[0]
    assert row[sheet.COL_ATTENTION] == "Waiting"
    assert row[sheet.COL_OUTREACH] == "Stopped"


def test_builder_publishes_unowned_action_as_unassigned():
    account = Account.objects.create(name="Unowned Primary")
    opportunity = Opportunity.objects.create(
        account=account,
        owner=None,
        source=Opportunity.Source.MANUAL,
        manual_pin=True,
        active_account=True,
    )
    facts = AccountPolicyFacts(
        account_key="unowned primary",
        manual_pin=True,
    )
    action = OpportunityAction.objects.create(
        opportunity=opportunity,
        kind=OpportunityAction.Kind.NEXT_STEP,
        description="Define and schedule the next step",
        due_on=NOW.date(),
        idempotency_key=f"v2:{opportunity.id}:account:authoritative-next-step",
    )

    view = build_crm_v2_database_view([
        _evidence(opportunity, facts=facts),
    ])

    assert view.rows.active_accounts[0][sheet.COL_OWNER] == "Unassigned"
    assert view.rows.active_accounts[0][sheet.COL_ATTENTION] == "Needs contact"
    assert view.rows.active_accounts[0][sheet.COL_STAGE] == "Radar only"
    assert view.rows.actions[0][sheet.COL_ACTION_ID] == str(action.id)
    assert view.rows.actions[0][sheet.COL_OWNER] == "Unassigned"
    assert view.rows.actions[0][sheet.COL_CHANNEL] == ""
    assert view.rows.actions[0][sheet.COL_DRAFT] == ""


def test_builder_uses_curated_pipeline_stage_not_legacy_evidence_stage():
    owner = SalesOwner.objects.get(handle="Arian")
    account = Account.objects.create(name="Curated Pipeline")
    opportunity = Opportunity.objects.create(
        account=account,
        owner=owner,
        source=Opportunity.Source.MANUAL,
        manual_pin=True,
        active_account=True,
        stage=Opportunity.Stage.EVALUATION,
        sales_motion_step=5,
        pipeline_stage=Opportunity.PipelineStage.DISCOVERY,
    )
    facts = AccountPolicyFacts(
        account_key="curated pipeline",
        manual_pin=True,
    )

    view = build_crm_v2_database_view([
        _evidence(opportunity, facts=facts),
    ])

    assert view.rows.active_accounts[0][sheet.COL_STAGE] == "Discovery"


def test_builder_marks_exact_dno_action_stopped_and_hides_delivery_text():
    owner = SalesOwner.objects.get(handle="Arian")
    account = Account.objects.create(name="Mixed Target")
    opportunity = Opportunity.objects.create(
        account=account,
        owner=owner,
        source=Opportunity.Source.SYSTEM,
        active_account=True,
    )
    target = Lead.objects.create(
        first_name="Stopped",
        company_name="Mixed Target",
        email="stopped@mixed-target.example",
    )
    OpportunityContact.objects.create(opportunity=opportunity, lead=target)
    inbound_at = NOW - timedelta(hours=1)
    facts = AccountPolicyFacts(
        account_key="mixed target",
        gmail=ConversationEvidence(
            human_inbound_count=1,
            substantive_inbound_count=1,
            latest_human_inbound_on=inbound_at,
            latest_substantive_inbound_on=inbound_at,
        ),
    )
    OpportunityAction.objects.create(
        opportunity=opportunity,
        target_lead=target,
        kind=OpportunityAction.Kind.NEEDS_RESPONSE,
        description="Respond",
        due_on=NOW.date(),
        channel="email",
        draft="Human draft that must not be delivered",
        idempotency_key="human:mixed-target",
    )

    view = build_crm_v2_database_view([
        _evidence(
            opportunity,
            facts=facts,
            target=target,
            reminder_do_not_outreach=True,
        )
    ])

    active = view.rows.active_accounts[0]
    action = view.rows.actions[0]
    assert active[sheet.COL_OUTREACH] == "Stopped"
    assert action[sheet.COL_OUTREACH] == "Stopped"
    assert action[sheet.COL_CHANNEL] == ""
    assert action[sheet.COL_DRAFT] == ""


def test_builder_requires_recollection_after_new_opportunity_creation():
    facts = AccountPolicyFacts(account_key="ramp", sales_motion_active=True)
    row = ResolvedAccountEvidence(
        account_key="ramp",
        account_name="Ramp",
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

    with pytest.raises(ValueError, match="recollected"):
        build_crm_v2_database_view([row])


def test_builder_orders_active_accounts_and_actions_by_attention_and_due_date():
    owner = SalesOwner.objects.get(handle="Arian")

    waiting_account = Account.objects.create(name="Aardvark Waiting")
    waiting_opportunity = Opportunity.objects.create(
        account=waiting_account,
        owner=owner,
        source=Opportunity.Source.MANUAL,
        manual_pin=True,
        active_account=True,
    )
    waiting_facts = AccountPolicyFacts(
        account_key="aardvark waiting",
        manual_pin=True,
        waiting_until=NOW.date() + timedelta(days=7),
    )

    due_account = Account.objects.create(name="Middle Due")
    due_opportunity = Opportunity.objects.create(
        account=due_account,
        owner=owner,
        source=Opportunity.Source.SYSTEM,
        active_account=True,
    )
    due_action = OpportunityAction.objects.create(
        opportunity=due_opportunity,
        kind=OpportunityAction.Kind.NEXT_STEP,
        description="Complete today's next step",
        due_on=NOW.date(),
        idempotency_key="human:middle-due",
    )
    due_facts = AccountPolicyFacts(
        account_key="middle due",
        human_current_action=True,
        next_action_due_on=NOW.date(),
    )

    overdue_account = Account.objects.create(name="Zulu Overdue")
    overdue_opportunity = Opportunity.objects.create(
        account=overdue_account,
        owner=owner,
        source=Opportunity.Source.SYSTEM,
        active_account=True,
    )
    overdue_action = OpportunityAction.objects.create(
        opportunity=overdue_opportunity,
        kind=OpportunityAction.Kind.NEXT_STEP,
        description="Complete overdue next step",
        due_on=NOW.date() - timedelta(days=1),
        idempotency_key="human:zulu-overdue",
    )
    overdue_facts = AccountPolicyFacts(
        account_key="zulu overdue",
        human_current_action=True,
        next_action_due_on=NOW.date() - timedelta(days=1),
    )

    view = build_crm_v2_database_view([
        _evidence(waiting_opportunity, facts=waiting_facts),
        _evidence(due_opportunity, facts=due_facts),
        _evidence(overdue_opportunity, facts=overdue_facts),
    ])

    assert [row[sheet.COL_ACCOUNT] for row in view.rows.active_accounts] == [
        "Zulu Overdue",
        "Middle Due",
        "Aardvark Waiting",
    ]
    assert [row[sheet.COL_ACTION_ID] for row in view.rows.actions] == [
        str(overdue_action.id),
        str(due_action.id),
    ]
