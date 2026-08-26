from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from django.contrib.auth.models import User

from crm.models import (
    Account,
    Deal,
    Lead,
    MeetingNote,
    Message,
    Opportunity,
    OpportunityAction,
    OpportunityContact,
    SalesOwner,
)
from linkedin.crm_followup_analysis import serialize_crm_followup_queue
from linkedin.enums import ProfileState
from linkedin.models import Campaign


NOW = datetime(2026, 8, 26, 15, 0, tzinfo=UTC)


def _lead(suffix: str, *, company: str = "Ramp") -> Lead:
    return Lead.objects.create(
        first_name=f"Lead{suffix}",
        last_name="Person",
        company_name=company,
        email=f"lead-{suffix}@example.com",
        linkedin_url=f"https://www.linkedin.com/in/crm-followup-{suffix}/",
        description="Security assurance stakeholder",
    )


def _opportunity(
    suffix: str,
    *,
    owner: SalesOwner | None,
    stage: str = Opportunity.Stage.DISCOVERY,
    step: int = 2,
) -> Opportunity:
    return Opportunity.objects.create(
        account=Account.objects.create(name=f"Account {suffix}"),
        name=f"Opportunity {suffix}",
        owner=owner,
        stage=stage,
        sales_motion_step=step,
    )


def _contact(opportunity: Opportunity, lead: Lead, *, primary: bool = True) -> None:
    OpportunityContact.objects.create(
        opportunity=opportunity,
        lead=lead,
        role=OpportunityContact.Role.CHAMPION,
        is_primary=primary,
    )


def _message(
    lead: Lead,
    *,
    direction: str,
    sent_at: datetime,
    operator: SalesOwner | None = None,
    body: str = "Can we review the sandbox workflow?",
) -> Message:
    return Message.objects.create(
        lead=lead,
        operator=operator,
        source=Message.Source.LINKEDIN,
        external_id=f"crm-followup-{lead.id}-{direction}-{sent_at.timestamp()}",
        direction=direction,
        sender=operator.handle if operator else "Prospect",
        body=body,
        sent_at=sent_at,
    )


@pytest.mark.django_db
def test_queue_exports_only_persisted_daily_action_with_explicit_owner_and_ids():
    arian = SalesOwner.objects.get(handle="Arian")
    chuka = SalesOwner.objects.get(handle="Chuka")
    lead = _lead("daily")
    opportunity = _opportunity("daily", owner=arian)
    _contact(opportunity, lead)
    _message(
        lead,
        direction=Message.Direction.OUTBOUND,
        sent_at=NOW - timedelta(hours=2),
        operator=chuka,
        body="Here is the sandbox.",
    )
    inbound = _message(
        lead,
        direction=Message.Direction.INBOUND,
        sent_at=NOW - timedelta(hours=1),
        body="  Yes,   let us test the evidence workflow.  ",
    )
    action = OpportunityAction.objects.create(
        opportunity=opportunity,
        target_lead=lead,
        kind=OpportunityAction.Kind.NEEDS_RESPONSE,
        description="Reply to the latest inbound message",
        due_on=NOW.date(),
        trigger_message=inbound,
    )
    stage_entered_at = opportunity.stage_entered_at
    action_updated_at = action.updated_at

    payload = serialize_crm_followup_queue(now=NOW)

    assert payload["candidate_count"] == 1
    assert payload["counts_by_owner"] == {"Arian": 1}
    candidate = payload["candidates"][0]
    assert candidate["action_id"] == str(action.id)
    assert candidate["opportunity_id"] == str(opportunity.id)
    assert candidate["lead_ids"] == [lead.id]
    assert candidate["owner"] == {"id": str(arian.id), "handle": "Arian"}
    assert candidate["evaluation"]["surface"] == "daily"
    assert candidate["recent_messages"][-1]["body"] == (
        "Yes, let us test the evidence workflow."
    )
    assert candidate["recent_messages"][0]["operator"] == "Chuka"
    assert len(candidate["context_fingerprint"]) == 64
    assert "never key by name" in payload["schema"]["decisions"][0]["lead_ids"]
    assert "Do not send messages" in payload["instructions"]

    opportunity.refresh_from_db()
    action.refresh_from_db()
    assert opportunity.stage_entered_at == stage_entered_at
    assert action.updated_at == action_updated_at
    assert OpportunityAction.objects.count() == 1


@pytest.mark.django_db
def test_queue_excludes_waiting_unowned_and_unpersisted_proposed_actions():
    owner = SalesOwner.objects.get(handle="Arian")

    waiting_lead = _lead("waiting")
    waiting = _opportunity("waiting", owner=owner)
    _contact(waiting, waiting_lead)
    _message(
        waiting_lead,
        direction=Message.Direction.INBOUND,
        sent_at=NOW - timedelta(hours=1),
    )
    OpportunityAction.objects.create(
        opportunity=waiting,
        target_lead=waiting_lead,
        status=OpportunityAction.Status.WAITING,
        description="Wait for their internal review",
        waiting_until=NOW.date() + timedelta(days=2),
    )

    unowned_lead = _lead("unowned")
    unowned = _opportunity("unowned", owner=None)
    _contact(unowned, unowned_lead)
    _message(
        unowned_lead,
        direction=Message.Direction.INBOUND,
        sent_at=NOW - timedelta(hours=1),
    )
    OpportunityAction.objects.create(
        opportunity=unowned,
        target_lead=unowned_lead,
        description="Reply today",
        due_on=NOW.date(),
    )

    proposed_lead = _lead("proposed")
    proposed = _opportunity("proposed", owner=owner)
    _contact(proposed, proposed_lead)
    _message(
        proposed_lead,
        direction=Message.Direction.INBOUND,
        sent_at=NOW - timedelta(hours=1),
    )

    payload = serialize_crm_followup_queue(now=NOW)

    assert payload["candidates"] == []
    assert payload["unowned_daily_count"] == 1
    assert OpportunityAction.objects.count() == 2


@pytest.mark.django_db
def test_resolved_meeting_context_is_included_but_cannot_widen_eligibility():
    owner = SalesOwner.objects.get(handle="Arian")
    lead = _lead("context")
    opportunity = _opportunity(
        "context",
        owner=owner,
        stage=Opportunity.Stage.EVALUATION,
        step=5,
    )
    _contact(opportunity, lead)
    old_message = _message(
        lead,
        direction=Message.Direction.OUTBOUND,
        sent_at=NOW - timedelta(days=90),
        operator=owner,
    )
    action = OpportunityAction.objects.create(
        opportunity=opportunity,
        target_lead=lead,
        kind=OpportunityAction.Kind.FOLLOWUP,
        description="Follow up when appropriate",
        trigger_message=old_message,
    )
    MeetingNote.objects.create(
        source=MeetingNote.Source.GRANOLA,
        external_id="not_crmfollowupctx1",
        opportunity=opportunity,
        title="Ramp working session",
        content="Granola context must not make an old action eligible.",
        detail_status=MeetingNote.DetailStatus.COMPLETE,
        match_status=MeetingNote.MatchStatus.MATCHED,
        match_method=MeetingNote.MatchMethod.MANUAL,
    )

    assert serialize_crm_followup_queue(now=NOW)["candidates"] == []

    action.due_on = NOW.date()
    action.save(update_fields={"due_on", "updated_at"})
    candidate = serialize_crm_followup_queue(now=NOW)["candidates"][0]
    assert candidate["meeting_context"]["source"] == MeetingNote.Source.GRANOLA
    assert candidate["meeting_context"]["external_id"] == "not_crmfollowupctx1"
    assert candidate["meeting_context"]["content"] == (
        "Granola context must not make an old action eligible."
    )


@pytest.mark.django_db
def test_fingerprint_is_stable_within_date_and_changes_on_evaluation_date():
    owner = SalesOwner.objects.get(handle="Arian")
    lead = _lead("fingerprint")
    opportunity = _opportunity("fingerprint", owner=owner)
    _contact(opportunity, lead)
    user = User.objects.create(username="crm-followup-fingerprint")
    campaign = Campaign.objects.create(name="CRM followup fingerprint", user=user)
    Deal.objects.create(
        lead=lead,
        campaign=campaign,
        state=ProfileState.PENDING,
    )
    OpportunityAction.objects.create(
        opportunity=opportunity,
        target_lead=lead,
        description="Review the canonical next step",
        due_on=(NOW - timedelta(days=2)).date(),
    )

    morning = serialize_crm_followup_queue(
        now=NOW.replace(hour=13),
    )["candidates"][0]
    evening = serialize_crm_followup_queue(
        now=NOW.replace(hour=20),
    )["candidates"][0]
    tomorrow = serialize_crm_followup_queue(
        now=NOW + timedelta(days=1),
    )["candidates"][0]

    assert morning["context_fingerprint"] == evening["context_fingerprint"]
    assert morning["evaluation"]["date"] == "2026-08-26"
    assert tomorrow["evaluation"]["date"] == "2026-08-27"
    assert morning["context_fingerprint"] != tomorrow["context_fingerprint"]


def test_serializer_rejects_naive_evaluation_time():
    with pytest.raises(ValueError, match="timezone-aware"):
        serialize_crm_followup_queue(now=datetime(2026, 8, 26, 15, 0))


@pytest.mark.django_db
def test_queue_uses_only_persisted_target_contact_context_on_multi_contact_account():
    owner = SalesOwner.objects.get(handle="Arian")
    champion = _lead("champion")
    recipient = _lead("recipient")
    opportunity = _opportunity("targeted", owner=owner)
    _contact(opportunity, champion, primary=True)
    OpportunityContact.objects.create(
        opportunity=opportunity,
        lead=recipient,
        role=OpportunityContact.Role.DECISION_MAKER,
    )
    _message(
        champion,
        direction=Message.Direction.INBOUND,
        sent_at=NOW - timedelta(hours=2),
        body="Champion-only context must not leak into this action.",
    )
    recipient_message = _message(
        recipient,
        direction=Message.Direction.INBOUND,
        sent_at=NOW - timedelta(hours=1),
        body="Please send the target-specific follow-up.",
    )
    action = OpportunityAction.objects.create(
        opportunity=opportunity,
        target_lead=recipient,
        kind=OpportunityAction.Kind.NEEDS_RESPONSE,
        description="Reply to the decision maker",
        due_on=NOW.date(),
        trigger_message=recipient_message,
    )

    candidate = serialize_crm_followup_queue(now=NOW)["candidates"][0]

    assert candidate["action_id"] == str(action.id)
    assert candidate["lead_ids"] == [recipient.id]
    assert candidate["action"]["target_lead_id"] == recipient.id
    assert [item["lead_id"] for item in candidate["recent_messages"]] == [
        recipient.id,
    ]
    contacts = {item["lead_id"]: item for item in candidate["contacts"]}
    assert contacts[recipient.id]["is_action_target"] is True
    assert contacts[champion.id]["is_action_target"] is False
