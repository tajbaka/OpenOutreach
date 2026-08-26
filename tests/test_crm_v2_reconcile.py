from datetime import date, datetime, timedelta, UTC

import pytest

from crm.models import (
    Account,
    Lead,
    Opportunity,
    OpportunityAction,
    OpportunityContact,
    SalesOwner,
)
from linkedin import crm_v2_reconcile
from linkedin.crm_v2_evidence import ResolvedAccountEvidence
from linkedin.crm_v2_policy import (
    AccountPolicyFacts,
    ConversationEvidence,
    evaluate_account,
)
from linkedin.crm_v2_reconcile import (
    apply_reconciliation,
    dry_run_reconciliation,
)


pytestmark = pytest.mark.django_db
NOW = datetime(2026, 8, 26, 15, tzinfo=UTC)


def _resolved(
    *,
    account_key,
    account_name,
    facts,
    lead_ids=(),
    opportunity=None,
    last_touch=None,
    owner="",
    owner_is_override=False,
):
    return ResolvedAccountEvidence(
        account_key=account_key,
        account_name=account_name,
        lead_ids=tuple(lead_ids),
        opportunity_id=str(opportunity.id) if opportunity else "",
        owner=(
            owner
            or (
                opportunity.owner.handle
                if opportunity and opportunity.owner
                else ""
            )
        ),
        key_contacts=(),
        last_meaningful_touch=last_touch,
        reminder_target_lead_id=None,
        trigger_message_id=None,
        trigger_meeting_id=None,
        facts=facts,
        decision=evaluate_account(facts, today=NOW.date()),
        owner_is_override=owner_is_override,
    )


def test_ramp_sales_motion_dry_run_then_apply_creates_one_active_account():
    owner = SalesOwner.objects.get(handle="Arian")
    zelia = Lead.objects.create(
        first_name="Zelia",
        last_name="Pantani",
        company_name="Ramp",
        email="zelia@ramp.com",
    )
    facts = AccountPolicyFacts(
        account_key="ramp",
        sales_motion_active=True,
    )
    evidence = _resolved(
        account_key="ramp",
        account_name="Ramp",
        facts=facts,
        lead_ids=(zelia.id,),
        owner="Arian",
    )

    dry_run = dry_run_reconciliation([evidence], evaluated_at=NOW)

    assert dry_run.applied is False
    assert dry_run.accounts_created == 1
    assert dry_run.opportunities_created == 1
    assert dry_run.contacts_linked == 1
    assert not Account.objects.filter(normalized_name="ramp").exists()
    assert Opportunity.objects.count() == 0

    applied = apply_reconciliation([evidence], evaluated_at=NOW)

    assert applied.applied is True
    assert applied.accounts_created == 1
    assert applied.opportunities_created == 1
    account = Account.objects.get(normalized_name="ramp")
    assert account.domain == "ramp.com"
    opportunity = Opportunity.objects.get(account=account, motion_key="primary")
    assert opportunity.source == Opportunity.Source.SHEET
    assert opportunity.owner == owner
    assert applied.owners_assigned == 1
    assert opportunity.active_account is True
    assert opportunity.admission_reason == "sales_motion_active"
    assert opportunity.admission_reasons == ["sales_motion_active"]
    assert opportunity.admission_tier == Opportunity.AdmissionTier.AUTHORITATIVE
    assert opportunity.inactive_at is None
    assert opportunity.contacts.get().lead == zelia
    assert OpportunityAction.objects.count() == 0

    repeated = apply_reconciliation([evidence], evaluated_at=NOW)
    assert repeated.accounts_created == 0
    assert repeated.opportunities_created == 0
    assert repeated.contacts_linked == 0
    assert Opportunity.objects.count() == 1


def test_stackarmor_manual_pin_keeps_dno_contact_and_human_sales_fields():
    owner = SalesOwner.objects.get(handle="Arian")
    account = Account.objects.create(name="stackArmor")
    opportunity = Opportunity.objects.create(
        account=account,
        name="stackArmor opportunity",
        owner=owner,
        stage=Opportunity.Stage.DISCOVERY,
        sales_motion_step=2,
        source=Opportunity.Source.MANUAL,
        active_account=False,
        inactive_at=NOW - timedelta(days=1),
        inactive_reason="legacy_cleanup",
    )
    contact = Lead.objects.create(
        first_name="Matt",
        last_name="Reviewer",
        company_name="stackArmor",
        email="matt@stackarmor.com",
        disqualified=True,
    )
    facts = AccountPolicyFacts(
        account_key="stackarmor",
        manual_pin=True,
        do_not_outreach=True,
    )
    evidence = _resolved(
        account_key="stackarmor",
        account_name="stackArmor",
        facts=facts,
        lead_ids=(contact.id,),
        opportunity=opportunity,
    )

    report = apply_reconciliation([evidence], evaluated_at=NOW)

    opportunity.refresh_from_db()
    account.refresh_from_db()
    contact.refresh_from_db()
    assert report.opportunities_activated == 1
    assert report.contacts_linked == 1
    assert opportunity.active_account is True
    assert opportunity.manual_pin is True
    assert opportunity.admission_reason == "manual_pin"
    assert opportunity.inactive_at is None
    assert opportunity.inactive_reason == ""
    assert opportunity.owner == owner
    assert opportunity.stage == Opportunity.Stage.DISCOVERY
    assert opportunity.sales_motion_step == 2
    assert account.domain == "stackarmor.com"
    assert contact.disqualified is True
    assert OpportunityContact.objects.filter(
        opportunity=opportunity,
        lead=contact,
    ).exists()
    assert OpportunityAction.objects.count() == 0


def test_old_linkedin_only_bootstrap_opportunity_is_deactivated_not_deleted_or_closed():
    account = Account.objects.create(name="Old LinkedIn Account")
    opportunity = Opportunity.objects.create(
        account=account,
        name="Old LinkedIn Account",
        source=Opportunity.Source.BOOTSTRAP,
        stage=Opportunity.Stage.DISCOVERY,
        sales_motion_step=2,
        active_account=True,
    )
    old_touch = NOW - timedelta(days=120)
    linkedin = ConversationEvidence(
        human_inbound_count=1,
        substantive_inbound_count=1,
        outbound_count=1,
        latest_human_inbound_on=old_touch,
        latest_substantive_inbound_on=old_touch,
        latest_outbound_on=old_touch - timedelta(minutes=5),
    )
    facts = AccountPolicyFacts(
        account_key="old linkedin account",
        linkedin=linkedin,
    )
    evidence = _resolved(
        account_key="old linkedin account",
        account_name="Old LinkedIn Account",
        facts=facts,
        opportunity=opportunity,
        last_touch=old_touch,
    )

    report = apply_reconciliation([evidence], evaluated_at=NOW)

    opportunity.refresh_from_db()
    assert report.opportunities_deactivated == 1
    assert Opportunity.objects.filter(pk=opportunity.pk).exists()
    assert opportunity.active_account is False
    assert opportunity.admission_reason == "stale_linkedin_substantive_bidirectional"
    assert opportunity.inactive_reason == "stale_linkedin_substantive_bidirectional"
    assert opportunity.inactive_at == NOW
    assert opportunity.stage == Opportunity.Stage.DISCOVERY
    assert opportunity.closed_won_at is None
    assert opportunity.closed_lost_at is None
    assert OpportunityAction.objects.count() == 0


def test_ambiguous_account_name_fails_closed_without_creating_an_opportunity():
    Account.objects.create(name="Acme, Inc.")
    Account.objects.create(name="Acme Inc")
    facts = AccountPolicyFacts(
        account_key="acme inc",
        sales_motion_active=True,
    )
    evidence = _resolved(
        account_key="acme inc",
        account_name="Acme Inc",
        facts=facts,
    )

    report = apply_reconciliation([evidence], evaluated_at=NOW)

    assert report.skipped_ambiguous == 1
    assert report.issues[0].reason == "duplicate_normalized_account_name"
    assert Opportunity.objects.count() == 0


def test_explicit_owner_override_can_reassign_an_existing_account():
    arian = SalesOwner.objects.get(handle="Arian")
    athena = SalesOwner.objects.get(handle="Athena")
    account = Account.objects.create(name="Ramp")
    opportunity = Opportunity.objects.create(
        account=account,
        owner=athena,
        source=Opportunity.Source.SHEET,
        active_account=True,
    )
    facts = AccountPolicyFacts(account_key="ramp", sales_motion_active=True)
    evidence = _resolved(
        account_key="ramp",
        account_name="Ramp",
        facts=facts,
        opportunity=opportunity,
        owner="Arian",
        owner_is_override=True,
    )

    report = apply_reconciliation([evidence], evaluated_at=NOW)

    opportunity.refresh_from_db()
    assert opportunity.owner == arian
    assert report.owners_assigned == 1


def test_people_only_row_without_opportunity_anchor_skips_db_reconciliation(monkeypatch):
    facts = AccountPolicyFacts(account_key="people only")
    evidence = _resolved(
        account_key="people only",
        account_name="People Only",
        facts=facts,
        lead_ids=(999999,),
    )

    def fail_if_called(*args, **kwargs):
        pytest.fail("People-only evidence without an Opportunity must not query identity")

    monkeypatch.setattr(crm_v2_reconcile, "_reconcile_row", fail_if_called)

    report = dry_run_reconciliation([evidence], evaluated_at=NOW)

    assert report.issues == []
    assert report.people_only_rows == 1
    assert report.opportunities_unchanged == 1


def test_unevaluated_automated_duplicate_links_do_not_block_unadmitted_anchor():
    lead = Lead.objects.create(
        first_name="Legacy",
        last_name="Contact",
        company_name="Dormant Example",
        email="legacy@dormant.example",
    )
    canonical_account = Account.objects.create(name="Dormant Example")
    duplicate_account = Account.objects.create(name="Dormant Example Legacy")
    canonical = Opportunity.objects.create(
        account=canonical_account,
        source=Opportunity.Source.BOOTSTRAP,
        active_account=True,
    )
    duplicate = Opportunity.objects.create(
        account=duplicate_account,
        source=Opportunity.Source.SYSTEM,
        active_account=True,
    )
    OpportunityContact.objects.create(opportunity=canonical, lead=lead)
    OpportunityContact.objects.create(opportunity=duplicate, lead=lead)
    OpportunityAction.objects.create(
        opportunity=duplicate,
        description="Legacy generated task",
        idempotency_key="system:legacy-generated",
    )
    facts = AccountPolicyFacts(account_key="dormant example")
    evidence = _resolved(
        account_key="dormant example",
        account_name="Dormant Example",
        facts=facts,
        lead_ids=(lead.id,),
        opportunity=canonical,
    )

    report = apply_reconciliation([evidence], evaluated_at=NOW)

    canonical.refresh_from_db()
    duplicate.refresh_from_db()
    assert report.issues == []
    assert Account.objects.count() == 2
    assert Opportunity.objects.count() == 2
    assert OpportunityContact.objects.filter(lead=lead).count() == 2
    assert report.opportunities_deactivated == 2
    assert canonical.active_account is False
    assert duplicate.active_account is False
    assert canonical.admission_evaluated_at == NOW
    assert duplicate.admission_evaluated_at == NOW
    assert duplicate.source == Opportunity.Source.SYSTEM
    assert duplicate.account == duplicate_account
    assert [change.kind for change in report.changes].count(
        "legacy_duplicate_deactivated"
    ) == 1


@pytest.mark.parametrize(
    "unsafe_state",
    ["admitted", "active", "manual", "human_revision", "human_action", "human_stage"],
)
def test_duplicate_exact_lead_links_still_block_active_or_human_ambiguity(unsafe_state):
    lead = Lead.objects.create(
        first_name="Ambiguous",
        last_name="Contact",
        company_name="Ambiguous Example",
        email="contact@ambiguous.example",
    )
    canonical = Opportunity.objects.create(
        account=Account.objects.create(name="Ambiguous Example"),
        source=Opportunity.Source.BOOTSTRAP,
        active_account=False,
    )
    duplicate = Opportunity.objects.create(
        account=Account.objects.create(name="Ambiguous Example Legacy"),
        source=Opportunity.Source.SYSTEM,
        active_account=False,
    )
    OpportunityContact.objects.create(opportunity=canonical, lead=lead)
    OpportunityContact.objects.create(opportunity=duplicate, lead=lead)

    if unsafe_state == "active":
        duplicate.active_account = True
        duplicate.admission_evaluated_at = NOW - timedelta(days=1)
        duplicate.save(update_fields={
            "active_account",
            "admission_evaluated_at",
            "updated_at",
        })
    elif unsafe_state == "manual":
        duplicate.source = Opportunity.Source.MANUAL
        duplicate.save(update_fields={"source", "updated_at"})
    elif unsafe_state == "human_revision":
        duplicate.human_revision = 1
        duplicate.save(update_fields={"human_revision", "updated_at"})
    elif unsafe_state == "human_action":
        OpportunityAction.objects.create(
            opportunity=duplicate,
            description="Human task",
            idempotency_key="",
        )
    elif unsafe_state == "human_stage":
        duplicate.transition_to(
            Opportunity.Stage.DISCOVERY,
            sales_motion_step=2,
            source=Opportunity.Source.MANUAL,
            changed_at=NOW - timedelta(hours=1),
        )

    facts = AccountPolicyFacts(
        account_key="ambiguous example",
        sales_motion_active=unsafe_state == "admitted",
    )
    evidence = _resolved(
        account_key="ambiguous example",
        account_name="Ambiguous Example",
        facts=facts,
        lead_ids=(lead.id,),
        opportunity=canonical,
    )

    report = apply_reconciliation([evidence], evaluated_at=NOW)

    canonical.refresh_from_db()
    assert [issue.reason for issue in report.issues] == [
        "exact_leads_link_multiple_accounts"
    ]
    assert canonical.admission_evaluated_at is None
