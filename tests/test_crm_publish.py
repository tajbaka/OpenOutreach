from datetime import timedelta

import pytest
from django.utils import timezone

from crm.models import (
    Account,
    Lead,
    Message,
    Opportunity,
    OpportunityAction,
    OpportunityContact,
    SalesOwner,
)
from linkedin.crm_publish import build_crm_view_rows
from linkedin.crm_service import recalculate_actions
from linkedin.notifications import crm_sheets


@pytest.mark.django_db
def test_views_use_canonical_stage_owner_and_stable_ids():
    now = timezone.now()
    owner = SalesOwner.objects.get(handle="Arian")
    opportunity = Opportunity.objects.create(
        account=Account.objects.create(name="Ramp"),
        owner=owner,
        stage=Opportunity.Stage.DISCOVERY,
        sales_motion_step=2,
    )
    lead = Lead.objects.create(
        first_name="Zelia",
        last_name="Pantani",
        company_name="Ramp",
        linkedin_url="https://linkedin.com/in/zelia-pantani",
    )
    OpportunityContact.objects.create(
        opportunity=opportunity,
        lead=lead,
        role=OpportunityContact.Role.CHAMPION,
    )
    Message.objects.create(
        lead=lead,
        source=Message.Source.LINKEDIN,
        external_id="ramp-inbound",
        direction=Message.Direction.INBOUND,
        body="Can you tailor the sandbox?",
        sent_at=now - timedelta(hours=1),
    )

    action_report = recalculate_actions(apply=True, now=now)
    rows = build_crm_view_rows(
        action_report,
        granola_available=False,
        synced_at=now,
    )

    assert rows.opportunities[0][crm_sheets.COL_OPPORTUNITY_ID] == str(opportunity.id)
    assert rows.pipeline[0][crm_sheets.COL_OPPORTUNITY_ID] == str(opportunity.id)
    assert rows.pipeline[0]["Discovery"].startswith("Ramp\nArian")
    followup = rows.followups_by_owner["Arian"][0]
    assert followup[crm_sheets.COL_LEAD_ID] == str(lead.id)
    assert followup[crm_sheets.COL_ACTION_ID]
    assert followup[crm_sheets.COL_OPPORTUNITY_ID] == str(opportunity.id)


@pytest.mark.django_db
def test_unowned_daily_action_goes_to_recovery_not_sender_tabs():
    now = timezone.now()
    opportunity = Opportunity.objects.create(
        account=Account.objects.create(name="Unowned"),
        stage=Opportunity.Stage.DISCOVERY,
        sales_motion_step=2,
    )
    lead = Lead.objects.create(
        first_name="Taylor",
        company_name="Unowned",
        linkedin_url="https://linkedin.com/in/unowned-taylor",
    )
    OpportunityContact.objects.create(opportunity=opportunity, lead=lead)
    Message.objects.create(
        lead=lead,
        source=Message.Source.GMAIL,
        external_id="unowned-inbound",
        direction=Message.Direction.INBOUND,
        sent_at=now,
    )

    report = recalculate_actions(apply=True, now=now)
    rows = build_crm_view_rows(report, granola_available=False, synced_at=now)

    assert rows.followups_by_owner == {}
    assert rows.recovery[0][crm_sheets.COL_RECOVERY_ELIGIBILITY] == "unassigned_current_action"


@pytest.mark.django_db
def test_sender_row_uses_persisted_action_target_not_champion_or_contact_order():
    now = timezone.now()
    owner = SalesOwner.objects.get(handle="Arian")
    opportunity = Opportunity.objects.create(
        account=Account.objects.create(name="Multi-contact"),
        owner=owner,
        stage=Opportunity.Stage.DISCOVERY,
        sales_motion_step=2,
        last_meaningful_activity_at=now,
    )
    champion = Lead.objects.create(
        first_name="Champion",
        company_name="Multi-contact",
        linkedin_url="https://linkedin.com/in/champion-first",
    )
    recipient = Lead.objects.create(
        first_name="Recipient",
        company_name="Multi-contact",
        linkedin_url="https://linkedin.com/in/persisted-recipient",
    )
    OpportunityContact.objects.create(
        opportunity=opportunity,
        lead=champion,
        role=OpportunityContact.Role.CHAMPION,
        is_primary=True,
    )
    OpportunityContact.objects.create(
        opportunity=opportunity,
        lead=recipient,
        role=OpportunityContact.Role.STAKEHOLDER,
    )
    action = OpportunityAction.objects.create(
        opportunity=opportunity,
        target_lead=recipient,
        kind=OpportunityAction.Kind.NEXT_STEP,
        description="Contact the persisted recipient",
        due_on=timezone.localdate(),
    )

    report = recalculate_actions(apply=False, now=now)
    rows = build_crm_view_rows(report, granola_available=False, synced_at=now)

    followup = rows.followups_by_owner[owner.handle][0]
    assert followup[crm_sheets.COL_ACTION_ID] == str(action.id)
    assert followup[crm_sheets.COL_LEAD_ID] == str(recipient.id)
    assert followup[crm_sheets.COL_CONTACT] == "Recipient"


@pytest.mark.django_db
def test_targetless_daily_action_is_recovery_not_guessed_sender_work():
    now = timezone.now()
    owner = SalesOwner.objects.get(handle="Arian")
    opportunity = Opportunity.objects.create(
        account=Account.objects.create(name="Missing target"),
        owner=owner,
        stage=Opportunity.Stage.DISCOVERY,
        sales_motion_step=2,
        last_meaningful_activity_at=now,
    )
    for suffix in ("one", "two"):
        lead = Lead.objects.create(
            first_name=suffix.title(),
            company_name="Missing target",
            linkedin_url=f"https://linkedin.com/in/missing-target-{suffix}",
        )
        OpportunityContact.objects.create(opportunity=opportunity, lead=lead)
    OpportunityAction.objects.create(
        opportunity=opportunity,
        kind=OpportunityAction.Kind.NEXT_STEP,
        description="Ambiguous recipient",
        due_on=timezone.localdate(),
    )

    report = recalculate_actions(apply=False, now=now)
    rows = build_crm_view_rows(report, granola_available=False, synced_at=now)

    assert rows.followups_by_owner == {}
    assert rows.recovery[0][crm_sheets.COL_RECOVERY_ELIGIBILITY] == (
        "unresolved_action_target"
    )


@pytest.mark.django_db
def test_inactivity_age_is_published_to_opportunities_and_recovery():
    now = timezone.now()
    owner = SalesOwner.objects.get(handle="Arian")
    opportunity = Opportunity.objects.create(
        account=Account.objects.create(name="Aging account"),
        owner=owner,
        stage=Opportunity.Stage.DISCOVERY,
        sales_motion_step=2,
        last_meaningful_activity_at=now - timedelta(days=30),
    )
    lead = Lead.objects.create(
        first_name="Aging",
        company_name="Aging account",
        linkedin_url="https://linkedin.com/in/aging-contact",
    )
    OpportunityContact.objects.create(opportunity=opportunity, lead=lead)
    Message.objects.create(
        lead=lead,
        operator=owner,
        source=Message.Source.LINKEDIN,
        external_id="aging-outbound",
        direction=Message.Direction.OUTBOUND,
        body="Checking in on the next step.",
        sent_at=now - timedelta(days=30),
    )

    report = recalculate_actions(apply=False, now=now)
    rows = build_crm_view_rows(report, granola_available=False, synced_at=now)

    assert rows.opportunities[0][crm_sheets.COL_INACTIVITY_AGE] == "30"
    assert rows.recovery[0][crm_sheets.COL_INACTIVITY_AGE] == 30
