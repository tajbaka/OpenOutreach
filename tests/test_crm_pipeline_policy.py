from datetime import UTC, datetime

import pytest

from crm.models import Account, Opportunity, OpportunityPipelineEvent
from linkedin.crm_pipeline_policy import (
    qualifies_for_pipeline_triage,
    reconcile_pipeline_triage,
)
from linkedin.crm_v2_evidence import ResolvedAccountEvidence
from linkedin.crm_v2_policy import (
    AccountPolicyFacts,
    ConversationEvidence,
    evaluate_account,
)


pytestmark = pytest.mark.django_db
NOW = datetime(2026, 8, 26, 15, tzinfo=UTC)


def _row(facts, *, opportunity=None):
    return ResolvedAccountEvidence(
        account_key=facts.account_key,
        account_name=facts.account_key,
        lead_ids=(),
        opportunity_id=str(opportunity.id) if opportunity else "",
        owner="",
        key_contacts=(),
        last_meaningful_touch=None,
        reminder_target_lead_id=None,
        trigger_message_id=None,
        trigger_meeting_id=None,
        facts=facts,
        decision=evaluate_account(facts, today=NOW.date()),
    )


def test_meeting_only_does_not_enter_pipeline_triage():
    facts = AccountPolicyFacts(
        account_key="cloudflare.com",
        latest_completed_external_meeting_on=NOW.date(),
        post_meeting_followup_required=True,
    )
    assert qualifies_for_pipeline_triage(_row(facts)) is False


def test_meeting_plus_human_gmail_enters_pipeline_triage():
    facts = AccountPolicyFacts(
        account_key="prescientsecurity.com",
        latest_completed_external_meeting_on=NOW.date(),
        gmail=ConversationEvidence(
            human_inbound_count=1,
            substantive_inbound_count=1,
            latest_human_inbound_on=NOW,
            latest_substantive_inbound_on=NOW,
        ),
    )
    assert qualifies_for_pipeline_triage(_row(facts)) is True


def test_pipeline_triage_apply_is_idempotent_and_never_advances_stage():
    account = Account.objects.create(name="Ramp", domain="ramp.com")
    opportunity = Opportunity.objects.create(account=account, name="Ramp")
    facts = AccountPolicyFacts(account_key="ramp.com", sales_motion_active=True)
    row = _row(facts, opportunity=opportunity)

    first = reconcile_pipeline_triage([row], apply=True, evaluated_at=NOW)
    second = reconcile_pipeline_triage([row], apply=True, evaluated_at=NOW)

    opportunity.refresh_from_db()
    assert first.promoted == 1
    assert second.promoted == 0
    assert second.preserved == 1
    assert opportunity.pipeline_stage == Opportunity.PipelineStage.TRIAGE
    assert OpportunityPipelineEvent.objects.count() == 1


def test_pipeline_triage_dry_run_rolls_back():
    account = Account.objects.create(name="StackArmor", domain="stackarmor.com")
    opportunity = Opportunity.objects.create(account=account, name="StackArmor")
    facts = AccountPolicyFacts(account_key="stackarmor.com", manual_pin=True)

    report = reconcile_pipeline_triage(
        [_row(facts, opportunity=opportunity)],
        apply=False,
        evaluated_at=NOW,
    )

    opportunity.refresh_from_db()
    assert report.promoted == 1
    assert opportunity.pipeline_stage == ""
    assert OpportunityPipelineEvent.objects.count() == 0
