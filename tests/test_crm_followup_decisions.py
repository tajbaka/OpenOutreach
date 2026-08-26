from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

import pytest

from crm.models import (
    Account,
    Lead,
    Message,
    Opportunity,
    OpportunityAction,
    OpportunityContact,
    SalesOwner,
)
from linkedin.crm_followup_analysis import serialize_crm_followup_queue
from linkedin.crm_followup_decisions import (
    CrmFollowupDecisionError,
    apply_crm_followup_decisions,
    crm_followup_decision_from_mapping,
    load_crm_followup_decisions,
)
from linkedin.models import WorkflowRun


NOW = datetime(2026, 8, 26, 15, 0, tzinfo=UTC)


def _canonical_action(suffix: str) -> OpportunityAction:
    owner = SalesOwner.objects.get(handle="Arian")
    lead = Lead.objects.create(
        first_name=f"Lead{suffix}",
        last_name="Person",
        company_name=f"Account {suffix}",
        email=f"lead-{suffix}@example.com",
        linkedin_url=f"https://www.linkedin.com/in/decision-{suffix}/",
    )
    opportunity = Opportunity.objects.create(
        account=Account.objects.create(name=f"Account {suffix}"),
        name=f"Opportunity {suffix}",
        owner=owner,
        stage=Opportunity.Stage.DISCOVERY,
        sales_motion_step=2,
    )
    OpportunityContact.objects.create(
        opportunity=opportunity,
        lead=lead,
        role=OpportunityContact.Role.CHAMPION,
        is_primary=True,
    )
    inbound = Message.objects.create(
        lead=lead,
        source=Message.Source.LINKEDIN,
        external_id=f"decision-inbound-{suffix}",
        direction=Message.Direction.INBOUND,
        sender="Prospect",
        body="Can you send the next-step details?",
        sent_at=NOW - timedelta(hours=1),
    )
    return OpportunityAction.objects.create(
        opportunity=opportunity,
        target_lead=lead,
        kind=OpportunityAction.Kind.NEEDS_RESPONSE,
        description=f"Reply to canonical inbound {suffix}",
        due_on=NOW.date(),
        trigger_message=inbound,
    )


def _queue() -> dict[str, object]:
    return serialize_crm_followup_queue(now=NOW)


def _decision_row(candidate: dict[str, object], **overrides) -> dict[str, object]:
    row = {
        "action_id": candidate["action_id"],
        "opportunity_id": candidate["opportunity_id"],
        "lead_ids": candidate["lead_ids"],
        "context_fingerprint": candidate["context_fingerprint"],
        "recommended_next_step": "Send the requested details.",
        "relationship_summary": "They asked for a concrete next step.",
        "draft_email": "",
        "draft_linkedin": "",
        "needs_human_review": False,
        "review_reason": "",
    }
    row.update(overrides)
    return row


def _candidate_for(queue: dict[str, object], action: OpportunityAction) -> dict:
    return next(
        candidate
        for candidate in queue["candidates"]
        if candidate["action_id"] == str(action.id)
    )


@pytest.mark.django_db
def test_load_and_apply_populates_only_blank_action_draft_and_records_workflow(
    tmp_path,
):
    action = _canonical_action("apply")
    queue = _queue()
    candidate = _candidate_for(queue, action)
    decision_path = tmp_path / "decisions.json"
    decision_path.write_text(
        json.dumps({
            "decisions": [
                _decision_row(candidate, draft_email=" Here are the details. ")
            ],
        }),
        encoding="utf-8",
    )
    owner_id = action.opportunity.owner_id
    stage = action.opportunity.stage
    status = action.status
    description = action.description
    message_count = Message.objects.count()

    decisions = load_crm_followup_decisions(decision_path)
    result = apply_crm_followup_decisions(
        decisions,
        canonical_queue=queue,
    )

    action.refresh_from_db()
    action.opportunity.refresh_from_db()
    assert action.channel == "Email"
    assert action.draft == "Here are the details."
    assert action.human_revision == 1
    assert action.status == status
    assert action.description == description
    assert action.opportunity.owner_id == owner_id
    assert action.opportunity.stage == stage
    assert Message.objects.count() == message_count
    assert result.decisions_validated == 1
    assert result.drafts_requested == 1
    assert result.drafts_applied == 1
    assert result.email_drafts_applied == 1
    assert result.workflow_run_id is not None
    workflow = WorkflowRun.objects.get(pk=result.workflow_run_id)
    assert workflow.name == "crm-followup-decision-apply"
    assert workflow.counts == result.counts()


@pytest.mark.django_db
def test_existing_human_draft_is_preserved_without_revision_or_workflow():
    action = _canonical_action("preserve")
    action.channel = "Call"
    action.draft = "Human-authored plan"
    action.human_revision = 7
    action.save(update_fields={"channel", "draft", "human_revision", "updated_at"})
    queue = _queue()
    decision = crm_followup_decision_from_mapping(
        _decision_row(
            _candidate_for(queue, action),
            draft_linkedin="Codex-authored replacement",
        )
    )

    result = apply_crm_followup_decisions(
        [decision],
        canonical_queue=queue,
        record_workflow=False,
    )

    action.refresh_from_db()
    assert action.channel == "Call"
    assert action.draft == "Human-authored plan"
    assert action.human_revision == 7
    assert result.drafts_requested == 1
    assert result.drafts_applied == 0
    assert result.existing_drafts_preserved == 1
    assert result.workflow_run_id is None
    assert WorkflowRun.objects.filter(name="crm-followup-decision-apply").count() == 0


@pytest.mark.django_db
def test_existing_human_channel_is_preserved_when_draft_is_blank():
    action = _canonical_action("preserve-channel")
    action.channel = "Call"
    action.human_revision = 3
    action.save(update_fields={"channel", "human_revision", "updated_at"})
    queue = _queue()
    decision = crm_followup_decision_from_mapping(
        _decision_row(
            _candidate_for(queue, action),
            draft_email="Codex must not replace the selected channel",
        )
    )

    result = apply_crm_followup_decisions(
        [decision],
        canonical_queue=queue,
        record_workflow=False,
    )

    action.refresh_from_db()
    assert action.channel == "Call"
    assert action.draft == ""
    assert action.human_revision == 3
    assert result.drafts_applied == 0
    assert result.existing_drafts_preserved == 1


@pytest.mark.django_db
def test_blank_human_review_decision_is_a_true_noop():
    action = _canonical_action("review")
    queue = _queue()
    decision = crm_followup_decision_from_mapping(
        _decision_row(
            _candidate_for(queue, action),
            needs_human_review=True,
            review_reason="Relationship context is contradictory.",
        )
    )
    updated_at = action.updated_at

    result = apply_crm_followup_decisions(
        [decision],
        canonical_queue=queue,
        record_workflow=False,
    )

    action.refresh_from_db()
    assert action.channel == ""
    assert action.draft == ""
    assert action.human_revision == 0
    assert action.updated_at == updated_at
    assert result.no_op_decisions == 1
    assert result.human_reviews_requested == 1


@pytest.mark.django_db
def test_stale_member_rejects_entire_batch_before_any_draft_write():
    first = _canonical_action("atomic-one")
    second = _canonical_action("atomic-two")
    queue = _queue()
    first_decision = crm_followup_decision_from_mapping(
        _decision_row(
            _candidate_for(queue, first),
            draft_email="Valid first draft",
        )
    )
    stale_second = crm_followup_decision_from_mapping(
        _decision_row(
            _candidate_for(queue, second),
            context_fingerprint="0" * 64,
            draft_linkedin="Stale second draft",
        )
    )

    with pytest.raises(CrmFollowupDecisionError, match="Stale context_fingerprint"):
        apply_crm_followup_decisions(
            [first_decision, stale_second],
            canonical_queue=queue,
            record_workflow=False,
        )

    first.refresh_from_db()
    second.refresh_from_db()
    assert first.draft == second.draft == ""
    assert first.human_revision == second.human_revision == 0


@pytest.mark.django_db
def test_shared_account_batch_scopes_opportunity_lock_to_self_rows():
    first = _canonical_action("shared-lock-primary")
    second = _canonical_action("shared-lock-secondary")
    second.opportunity.account = first.opportunity.account
    second.opportunity.motion_key = "secondary"
    second.opportunity.save(update_fields={
        "account",
        "motion_key",
        "updated_at",
    })
    queue = _queue()
    decisions = [
        crm_followup_decision_from_mapping(
            _decision_row(
                _candidate_for(queue, action),
                draft_email=f"Draft for {action.opportunity.motion_key}",
            )
        )
        for action in (first, second)
    ]
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
        result = apply_crm_followup_decisions(
            decisions,
            canonical_queue=queue,
            record_workflow=False,
        )

    first.refresh_from_db()
    second.refresh_from_db()
    assert lock_calls == [((), {"of": ("self",)})]
    assert result.drafts_applied == 2
    assert first.draft == "Draft for primary"
    assert second.draft == "Draft for secondary"


@pytest.mark.django_db
def test_action_retarget_after_queue_rejects_draft_atomically():
    action = _canonical_action("retarget")
    queue = _queue()
    decision = crm_followup_decision_from_mapping(
        _decision_row(
            _candidate_for(queue, action),
            draft_email="This draft belongs to the old recipient",
        )
    )
    replacement = Lead.objects.create(
        first_name="Replacement",
        company_name=action.opportunity.account.name,
        linkedin_url="https://www.linkedin.com/in/replacement-recipient/",
    )
    OpportunityContact.objects.create(
        opportunity=action.opportunity,
        lead=replacement,
        role=OpportunityContact.Role.STAKEHOLDER,
    )
    action.target_lead = replacement
    action.save(update_fields={"target_lead", "updated_at"})

    with pytest.raises(CrmFollowupDecisionError, match="target changed"):
        apply_crm_followup_decisions(
            [decision],
            canonical_queue=queue,
            record_workflow=False,
        )

    action.refresh_from_db()
    assert action.draft == ""
    assert action.human_revision == 0


@pytest.mark.django_db
def test_stage_or_due_change_after_queue_rejects_stale_draft():
    action = _canonical_action("state-change")
    queue = _queue()
    decision = crm_followup_decision_from_mapping(
        _decision_row(
            _candidate_for(queue, action),
            draft_email="Draft from stale opportunity state",
        )
    )
    action.opportunity.stage = Opportunity.Stage.EVALUATION
    action.opportunity.sales_motion_step = 5
    action.opportunity.save(update_fields={
        "stage",
        "sales_motion_step",
        "updated_at",
    })
    action.due_on = NOW.date() + timedelta(days=1)
    action.save(update_fields={"due_on", "updated_at"})

    with pytest.raises(CrmFollowupDecisionError, match="action or opportunity changed"):
        apply_crm_followup_decisions(
            [decision],
            canonical_queue=queue,
            record_workflow=False,
        )

    action.refresh_from_db()
    assert action.draft == ""


@pytest.mark.django_db
def test_duplicate_and_unknown_action_ids_fail_closed():
    action = _canonical_action("identity")
    queue = _queue()
    decision = crm_followup_decision_from_mapping(
        _decision_row(
            _candidate_for(queue, action),
            draft_email="A valid draft",
        )
    )

    with pytest.raises(CrmFollowupDecisionError, match="Duplicate"):
        apply_crm_followup_decisions(
            [decision, decision],
            canonical_queue=queue,
            record_workflow=False,
        )

    unknown_row = _decision_row(_candidate_for(queue, action))
    unknown_row["action_id"] = str(uuid4())
    unknown = crm_followup_decision_from_mapping(unknown_row)
    with pytest.raises(CrmFollowupDecisionError, match="Unknown action_id"):
        apply_crm_followup_decisions(
            [unknown],
            canonical_queue=queue,
            record_workflow=False,
        )

    action.refresh_from_db()
    assert action.draft == ""
    assert action.human_revision == 0


@pytest.mark.django_db
def test_exact_opportunity_and_lead_id_mismatches_are_stale():
    action = _canonical_action("exact")
    queue = _queue()
    candidate = _candidate_for(queue, action)
    wrong_opportunity = _decision_row(candidate, opportunity_id=str(uuid4()))
    wrong_leads = _decision_row(candidate, lead_ids=[candidate["lead_ids"][0] + 1])

    for row, message in (
        (wrong_opportunity, "Stale opportunity_id"),
        (wrong_leads, "Stale lead_ids"),
    ):
        with pytest.raises(CrmFollowupDecisionError, match=message):
            apply_crm_followup_decisions(
                [crm_followup_decision_from_mapping(row)],
                canonical_queue=queue,
                record_workflow=False,
            )

    action.refresh_from_db()
    assert action.draft == ""


@pytest.mark.django_db
def test_loader_rejects_malformed_types_forbidden_fields_and_two_drafts(tmp_path):
    action = _canonical_action("malformed")
    queue = _queue()
    valid = _decision_row(_candidate_for(queue, action))
    malformed_rows = []
    for field, value in (
        ("action_id", 123),
        ("lead_ids", ["1"]),
        ("needs_human_review", "false"),
    ):
        row = dict(valid)
        row[field] = value
        malformed_rows.append(row)
    forbidden = dict(valid)
    forbidden["stage"] = "closed_won"
    malformed_rows.append(forbidden)
    two_drafts = dict(valid)
    two_drafts.update({"draft_email": "Email", "draft_linkedin": "LinkedIn"})
    malformed_rows.append(two_drafts)

    for index, row in enumerate(malformed_rows):
        path = tmp_path / f"malformed-{index}.json"
        path.write_text(json.dumps({"decisions": [row]}), encoding="utf-8")
        with pytest.raises(CrmFollowupDecisionError):
            load_crm_followup_decisions(path)

    action.refresh_from_db()
    assert action.draft == ""
    assert action.human_revision == 0


@pytest.mark.django_db
def test_tampered_canonical_queue_fingerprint_is_rejected():
    action = _canonical_action("queue-tamper")
    queue = _queue()
    decision = crm_followup_decision_from_mapping(
        _decision_row(_candidate_for(queue, action), draft_email="Draft")
    )
    tampered = copy.deepcopy(queue)
    tampered_candidate = _candidate_for(tampered, action)
    tampered_candidate["action"]["description"] = "Tampered description"

    with pytest.raises(CrmFollowupDecisionError, match="invalid or stale"):
        apply_crm_followup_decisions(
            [decision],
            canonical_queue=tampered,
            record_workflow=False,
        )

    action.refresh_from_db()
    assert action.draft == ""
