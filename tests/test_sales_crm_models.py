from datetime import date, timedelta
from uuid import UUID

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from crm.models import (
    Account,
    Lead,
    Meeting,
    MeetingNote,
    MeetingNoteSyncState,
    MeetingParticipant,
    Opportunity,
    OpportunityAction,
    OpportunityContact,
    OpportunitySheetState,
    OpportunityStageEvent,
    SalesOwner,
)


pytestmark = pytest.mark.django_db


def _opportunity(*, name="Ramp", owner_handle="Arian"):
    owner = SalesOwner.objects.get(handle=owner_handle)
    account = Account.objects.create(name=name)
    opportunity = Opportunity.objects.create(
        account=account,
        name=f"{name} primary",
        owner=owner,
    )
    return opportunity


def test_account_and_owner_have_stable_normalized_uuid_identity():
    owner = SalesOwner.objects.get(handle="Arian")
    assert isinstance(owner.pk, UUID)
    assert owner.normalized_handle == "arian"

    account = Account.objects.create(name="  RAMP, Inc.  ", domain="RAMP.COM")
    assert isinstance(account.pk, UUID)
    assert account.name == "RAMP, Inc."
    assert account.normalized_name == "ramp inc"
    assert account.domain == "ramp.com"

    international = Account.objects.create(name="  Société Générale  ")
    assert international.normalized_name == "société générale"

    with pytest.raises(IntegrityError), transaction.atomic():
        SalesOwner.objects.create(handle="ARIAN")


def test_opportunity_stage_transition_is_timestamped_and_audited():
    opportunity = _opportunity()
    owner = opportunity.owner
    initial_entered_at = opportunity.stage_entered_at
    initial = opportunity.stage_events.get()
    assert initial.from_stage == ""
    assert initial.to_stage == Opportunity.Stage.PROSPECTING

    changed = opportunity.transition_to(
        Opportunity.Stage.DISCOVERY,
        sales_motion_step=2,
        source=OpportunityStageEvent.Source.SHEET,
        actor=owner,
        changed_at=initial_entered_at + timedelta(hours=1),
    )

    assert changed is True
    assert opportunity.stage == Opportunity.Stage.DISCOVERY
    assert opportunity.sales_motion_step == 2
    assert opportunity.stage_entered_at == initial_entered_at + timedelta(hours=1)
    event = opportunity.stage_events.order_by("-changed_at").first()
    assert event.from_stage == Opportunity.Stage.PROSPECTING
    assert event.to_stage == Opportunity.Stage.DISCOVERY
    assert event.source == OpportunityStageEvent.Source.SHEET
    assert event.actor == owner
    assert opportunity.transition_to(
        Opportunity.Stage.DISCOVERY,
        sales_motion_step=2,
    ) is False

    event_count = opportunity.stage_events.count()
    entered_at = opportunity.stage_entered_at
    opportunity.name = "Updated without a stage transition"
    opportunity.save(update_fields={"name", "updated_at"})
    opportunity.refresh_from_db()
    assert opportunity.stage_events.count() == event_count
    assert opportunity.stage_entered_at == entered_at


def test_opportunity_enforces_motion_stage_probability_and_closure_constraints():
    account = Account.objects.create(name="Constraint account")
    Opportunity.objects.create(account=account, motion_key="Primary")
    with pytest.raises(IntegrityError), transaction.atomic():
        Opportunity.objects.create(account=account, motion_key="PRIMARY")

    with pytest.raises(IntegrityError), transaction.atomic():
        Opportunity.objects.create(
            account=Account.objects.create(name="Bad stage step"),
            stage=Opportunity.Stage.DISCOVERY,
            sales_motion_step=1,
        )

    with pytest.raises(IntegrityError), transaction.atomic():
        Opportunity.objects.create(
            account=Account.objects.create(name="Bad probability"),
            probability=101,
        )

    lost = Opportunity.objects.create(
        account=Account.objects.create(name="Lost account"),
        stage=Opportunity.Stage.CLOSED_LOST,
        closed_lost_reason="No budget",
    )
    assert lost.closed_lost_at is not None
    assert lost.closed_won_at is None

    with pytest.raises(IntegrityError), transaction.atomic():
        Opportunity.objects.create(
            account=Account.objects.create(name="Lost without reason"),
            stage=Opportunity.Stage.CLOSED_LOST,
        )


def test_opportunity_contacts_use_stable_lead_identity_and_roles():
    opportunity = _opportunity(name="Contacts account")
    lead = Lead.objects.create(
        first_name="Zelia",
        last_name="Pantani",
        linkedin_url="https://www.linkedin.com/in/zelia-sales-crm-test/",
    )
    champion = OpportunityContact.objects.create(
        opportunity=opportunity,
        lead=lead,
        role=OpportunityContact.Role.CHAMPION,
        is_primary=True,
    )
    assert isinstance(champion.pk, UUID)
    OpportunityContact.objects.create(
        opportunity=opportunity,
        lead=lead,
        role=OpportunityContact.Role.STAKEHOLDER,
    )
    assert opportunity.contacts.count() == 2

    with pytest.raises(IntegrityError), transaction.atomic():
        OpportunityContact.objects.create(
            opportunity=opportunity,
            lead=lead,
            role=OpportunityContact.Role.CHAMPION,
        )


def test_only_one_current_action_exists_and_action_state_is_durable():
    opportunity = _opportunity(name="Actions account")
    action = OpportunityAction.objects.create(
        opportunity=opportunity,
        kind=OpportunityAction.Kind.NEEDS_RESPONSE,
        description="Reply to the new inbound message",
        due_on=date.today(),
        idempotency_key="message:123",
    )
    assert isinstance(action.pk, UUID)

    with pytest.raises(IntegrityError), transaction.atomic():
        OpportunityAction.objects.create(
            opportunity=opportunity,
            status=OpportunityAction.Status.WAITING,
            description="Wait for procurement",
            waiting_until=date.today() + timedelta(days=7),
        )

    action.status = OpportunityAction.Status.COMPLETED
    action.disposition = OpportunityAction.Disposition.HANDLED
    action.save(update_fields={"status", "disposition"})
    assert action.completed_at is not None

    waiting = OpportunityAction.objects.create(
        opportunity=opportunity,
        status=OpportunityAction.Status.WAITING,
        description="Wait for procurement",
        waiting_until=date.today() + timedelta(days=7),
    )
    assert waiting.waiting_until is not None

    waiting.status = OpportunityAction.Status.CANCELLED
    waiting.save(update_fields={"status"})
    with pytest.raises(IntegrityError), transaction.atomic():
        OpportunityAction.objects.create(
            opportunity=opportunity,
            status=OpportunityAction.Status.WAITING,
            description="Missing waiting date",
        )


def test_action_target_must_be_a_linked_opportunity_contact():
    opportunity = _opportunity(name="Target constraint account")
    linked = Lead.objects.create(
        first_name="Linked",
        last_name="Contact",
        linkedin_url="https://www.linkedin.com/in/linked-action-target-test/",
    )
    unlinked = Lead.objects.create(
        first_name="Unlinked",
        last_name="Contact",
        linkedin_url="https://www.linkedin.com/in/unlinked-action-target-test/",
    )
    OpportunityContact.objects.create(opportunity=opportunity, lead=linked)

    action = OpportunityAction.objects.create(
        opportunity=opportunity,
        target_lead=linked,
        description="Valid targeted action",
    )
    assert action.target_lead == linked

    action.status = OpportunityAction.Status.COMPLETED
    action.save(update_fields={"status"})
    with pytest.raises(ValidationError, match="must be linked"):
        OpportunityAction.objects.create(
            opportunity=opportunity,
            target_lead=unlinked,
            description="Invalid cross-account target",
        )


def test_meeting_context_supports_multiple_contacts_and_unmatched_notes():
    lead = Lead.objects.create(
        first_name="Lindsey",
        last_name="Lowe",
        linkedin_url="https://www.linkedin.com/in/lindsey-sales-crm-test/",
    )
    meeting = Meeting.objects.create(
        source=Meeting.Source.GOOGLE_CALENDAR,
        external_id="sales-crm-multi-contact-meeting",
        lead=lead,
        start_at="2026-08-20T14:00:00Z",
        title="Ramp sandbox",
    )
    participant_link = MeetingParticipant.objects.create(
        meeting=meeting,
        lead=lead,
        attendee_email="LINDSEY@EXAMPLE.COM",
        match_method=MeetingParticipant.MatchMethod.ATTENDEE_EMAIL,
        is_primary=True,
    )
    assert isinstance(participant_link.pk, UUID)
    assert list(meeting.participants.all()) == [lead]
    participant = meeting.participant_links.get()
    assert participant.attendee_email == "lindsey@example.com"

    note = MeetingNote.objects.create(
        source=MeetingNote.Source.GRANOLA,
        external_id="not_1234567890abcd",
        title="Unmatched Ramp conversation",
        detail_status=MeetingNote.DetailStatus.COMPLETE,
    )
    assert isinstance(note.pk, UUID)
    assert note.match_status == MeetingNote.MatchStatus.UNMATCHED
    assert note.meeting is None

    with pytest.raises(IntegrityError), transaction.atomic():
        MeetingNote.objects.create(
            source=MeetingNote.Source.GRANOLA,
            external_id="not_1234567890abce",
            match_status=MeetingNote.MatchStatus.MATCHED,
        )


def test_opportunity_sheet_state_is_one_to_one_and_keeps_action_uuid():
    opportunity = _opportunity(name="Sheet state account")
    action = OpportunityAction.objects.create(
        opportunity=opportunity,
        description="Confirm the decision maker",
    )
    state = OpportunitySheetState.objects.create(
        opportunity=opportunity,
        published_human_snapshot={"stage": "prospecting"},
        published_revision=3,
        published_action_id=action.pk,
    )
    assert isinstance(state.pk, UUID)
    assert opportunity.sheet_state == state
    assert state.published_action_id == action.pk

    with pytest.raises(IntegrityError), transaction.atomic():
        OpportunitySheetState.objects.create(opportunity=opportunity)


def test_stage_events_and_note_sync_state_use_uuid_identity():
    account = Account.objects.create(name="UUID identity account")
    opportunity = Opportunity.objects.create(
        account=account,
        source=Opportunity.Source.BOOTSTRAP,
    )
    initial_event = opportunity.stage_events.get()
    assert isinstance(initial_event.pk, UUID)
    assert initial_event.source == OpportunityStageEvent.Source.BOOTSTRAP

    state = MeetingNoteSyncState.objects.create(source=MeetingNote.Source.GRANOLA)
    assert isinstance(state.pk, UUID)
