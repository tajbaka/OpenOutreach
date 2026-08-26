import importlib

import pytest
from django.apps import apps

from crm.models import (
    Account,
    Lead,
    Meeting,
    MeetingNote,
    MeetingParticipant,
    Message,
    Opportunity,
    OpportunityAction,
    OpportunityContact,
    SalesOwner,
)


pytestmark = pytest.mark.django_db


def test_canonical_sales_migration_backfill_is_additive_and_idempotent():
    lead = Lead.objects.create(
        first_name="Legacy",
        last_name="Contact",
        company_name="Legacy Co",
        linkedin_url="https://www.linkedin.com/in/legacy-sales-crm-migration/",
        email="LEGACY@EXAMPLE.COM",
    )
    meeting = Meeting.objects.create(
        source=Meeting.Source.GOOGLE_CALENDAR,
        external_id="legacy-calendar-event",
        lead=lead,
        start_at="2026-08-01T10:00:00Z",
        title="Legacy meeting",
        attendees=[{"email": "legacy@example.com", "name": "Legacy Contact"}],
        gemini_doc_id="legacy-gemini-doc",
        gemini_doc_title="Legacy Gemini notes",
        gemini_notes_raw="Exact legacy note content.",
    )
    migration = importlib.import_module("crm.migrations.0016_canonical_sales_crm")

    migration.seed_sales_owners_and_meeting_context(apps, None)
    migration.seed_sales_owners_and_meeting_context(apps, None)

    assert set(SalesOwner.objects.values_list("handle", flat=True)) >= {
        "Arian",
        "Chuka",
        "Athena",
        "Leili",
    }
    participant = MeetingParticipant.objects.get(meeting=meeting, lead=lead)
    assert participant.is_primary is True
    assert participant.attendee_email == "legacy@example.com"
    assert participant.match_method == MeetingParticipant.MatchMethod.LEGACY_PRIMARY
    note = MeetingNote.objects.get(source=MeetingNote.Source.GEMINI, meeting=meeting)
    assert note.external_id == "legacy-gemini-doc"
    assert note.content == "Exact legacy note content."
    assert note.detail_status == MeetingNote.DetailStatus.COMPLETE
    assert note.match_status == MeetingNote.MatchStatus.MATCHED
    assert note.match_method == MeetingNote.MatchMethod.LEGACY_PRIMARY
    assert MeetingParticipant.objects.filter(meeting=meeting, lead=lead).count() == 1
    assert MeetingNote.objects.filter(source=MeetingNote.Source.GEMINI, meeting=meeting).count() == 1


def test_action_target_migration_backfills_only_contact_proven_routes():
    linked = Lead.objects.create(
        first_name="Linked",
        last_name="Migration",
        linkedin_url="https://www.linkedin.com/in/linked-target-migration/",
    )
    unlinked = Lead.objects.create(
        first_name="Unlinked",
        last_name="Migration",
        linkedin_url="https://www.linkedin.com/in/unlinked-target-migration/",
    )
    opportunity = Opportunity.objects.create(
        account=Account.objects.create(name="Target Migration Account"),
    )
    OpportunityContact.objects.create(opportunity=opportunity, lead=linked)
    trigger = Message.objects.create(
        lead=linked,
        source=Message.Source.LINKEDIN,
        external_id="linked-target-migration-message",
        direction=Message.Direction.INBOUND,
        body="Hello",
        sent_at="2026-08-20T10:00:00Z",
    )
    action = OpportunityAction.objects.create(
        opportunity=opportunity,
        description="Route the linked trigger",
        trigger_message=trigger,
    )

    contradictory_opportunity = Opportunity.objects.create(
        account=Account.objects.create(name="Contradictory Target Migration"),
    )
    OpportunityContact.objects.create(
        opportunity=contradictory_opportunity,
        lead=linked,
    )
    contradictory_trigger = Message.objects.create(
        lead=unlinked,
        source=Message.Source.LINKEDIN,
        external_id="unlinked-target-migration-message",
        direction=Message.Direction.INBOUND,
        body="Wrong account",
        sent_at="2026-08-20T11:00:00Z",
    )
    contradictory_action = OpportunityAction.objects.create(
        opportunity=contradictory_opportunity,
        description="Must remain unrouted",
        trigger_message=contradictory_trigger,
    )
    migration = importlib.import_module(
        "crm.migrations.0018_opportunity_action_target_lead",
    )

    migration.backfill_action_targets(apps, None)
    migration.backfill_action_targets(apps, None)

    action.refresh_from_db()
    contradictory_action.refresh_from_db()
    assert action.target_lead_id == linked.id
    assert contradictory_action.target_lead_id is None
