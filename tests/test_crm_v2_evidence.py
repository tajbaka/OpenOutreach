from datetime import datetime, timedelta, timezone

import pytest

from crm.models import (
    Account,
    Lead,
    Meeting,
    MeetingNote,
    Message,
    Opportunity,
    OpportunityAction,
    OpportunityContact,
    SalesOwner,
)
from linkedin.crm_v2_actions import apply_action_reconciliation
from linkedin.crm_v2_evidence import (
    collect_account_evidence,
    conversation_evidence,
    email_domain,
)


NOW = datetime(2026, 8, 26, 12, tzinfo=timezone.utc)
pytestmark = pytest.mark.django_db


def _message(*, identifier, direction, body, offset, thread="thread-1", raw=None):
    return Message(
        id=identifier,
        lead_id=1,
        source=Message.Source.LINKEDIN,
        external_id=str(identifier),
        thread_external_id=thread,
        direction=direction,
        body=body,
        raw=raw or {},
        sent_at=NOW + timedelta(minutes=offset),
    )


def test_one_long_linkedin_reply_without_sales_intent_is_not_enough():
    evidence = conversation_evidence([
        _message(
            identifier=1,
            direction=Message.Direction.OUTBOUND,
            body="Would love to connect.",
            offset=0,
        ),
        _message(
            identifier=2,
            direction=Message.Direction.INBOUND,
            body="Appreciate you reaching out and sharing all of that context.",
            offset=1,
        ),
    ], source=Message.Source.LINKEDIN)

    assert evidence.substantive_inbound_count == 0
    assert not evidence.is_substantive_bidirectional


def test_real_multi_turn_linkedin_exchange_qualifies_substantive_inbound():
    evidence = conversation_evidence([
        _message(identifier=1, direction="outbound", body="First note", offset=0),
        _message(
            identifier=2,
            direction="inbound",
            body="We are actively looking at this workflow with our compliance team.",
            offset=1,
        ),
        _message(identifier=3, direction="outbound", body="Useful context", offset=2),
        _message(
            identifier=4,
            direction="inbound",
            body="The evidence collection portion is where our team is getting stuck.",
            offset=3,
        ),
    ], source=Message.Source.LINKEDIN)

    assert evidence.substantive_inbound_count == 2
    assert evidence.is_substantive_bidirectional


def test_explicit_linkedin_meeting_intent_qualifies_in_one_exchange():
    evidence = conversation_evidence([
        _message(identifier=1, direction="outbound", body="First note", offset=0),
        _message(
            identifier=2,
            direction="inbound",
            body="I would be interested in scheduling a demo next week.",
            offset=1,
        ),
    ], source=Message.Source.LINKEDIN)

    assert evidence.substantive_inbound_count == 1


def test_business_email_domain_is_identity_but_consumer_domain_is_not():
    assert email_domain("person@Ramp.com") == "ramp.com"
    assert email_domain("person@gmail.com") == ""


def test_old_list_mail_headers_cannot_admit_an_account():
    message = _message(
        identifier=8,
        direction=Message.Direction.INBOUND,
        body="Here is the latest product update for your account.",
        offset=0,
        raw={
            "headers": [
                {"name": "List-Id", "value": "updates.example.com"},
                {"name": "Precedence", "value": "bulk"},
            ],
        },
    )
    message.source = Message.Source.GMAIL

    evidence = conversation_evidence([message], source=Message.Source.GMAIL)

    assert evidence.human_inbound_count == 0
    assert evidence.automated_inbound_count == 1


def test_gmail_substantive_count_requires_high_intent_language():
    courtesy = _message(
        identifier=9,
        direction=Message.Direction.INBOUND,
        body="Thanks for the thoughtful note. I will keep it on file.",
        offset=0,
    )
    courtesy.source = Message.Source.GMAIL
    high_intent = _message(
        identifier=10,
        direction=Message.Direction.INBOUND,
        body="Could you send pricing and schedule a demo?",
        offset=1,
    )
    high_intent.source = Message.Source.GMAIL

    low = conversation_evidence([courtesy], source=Message.Source.GMAIL)
    high = conversation_evidence([high_intent], source=Message.Source.GMAIL)

    assert low.real_human_inbound_count == 1
    assert low.substantive_inbound_count == 0
    assert high.substantive_inbound_count == 1


def test_account_reminder_keeps_the_exact_gmail_contact_and_message():
    owner = SalesOwner.objects.get(normalized_handle="arian")
    older = Lead.objects.create(
        first_name="Older",
        company_name="Exact Account",
        email="older@exact.example",
    )
    target = Lead.objects.create(
        first_name="Target",
        company_name="Exact Account",
        email="target@exact.example",
    )
    Message.objects.create(
        lead=older,
        operator=owner,
        source=Message.Source.GMAIL,
        external_id="older-outbound",
        thread_external_id="older-thread",
        direction=Message.Direction.OUTBOUND,
        body="Earlier follow-up",
        sent_at=NOW - timedelta(days=2),
    )
    inbound = Message.objects.create(
        lead=target,
        source=Message.Source.GMAIL,
        external_id="target-inbound",
        thread_external_id="target-thread",
        direction=Message.Direction.INBOUND,
        body="Can you send the sandbox details?",
        sent_at=NOW - timedelta(hours=1),
    )

    row = next(
        item for item in collect_account_evidence(now=NOW)
        if item.account_name == "Exact Account"
    )

    assert row.decision.reminder.state.value == "needs_response"
    assert row.reminder_target_lead_id == target.id
    assert row.trigger_message_id == inbound.id
    assert row.owner == "Arian"


def test_newer_unrelated_outbound_cannot_hide_an_exact_thread_inbound():
    owner = SalesOwner.objects.get(normalized_handle="arian")
    waiting_target = Lead.objects.create(
        first_name="Zelia",
        company_name="Thread Exact",
        email="zelia@thread-exact.example",
    )
    unrelated = Lead.objects.create(
        first_name="Lindsey",
        company_name="Thread Exact",
        email="lindsey@thread-exact.example",
    )
    Message.objects.create(
        lead=waiting_target,
        operator=owner,
        source=Message.Source.GMAIL,
        external_id="target-outbound",
        thread_external_id="target-thread",
        direction=Message.Direction.OUTBOUND,
        body="Earlier context",
        sent_at=NOW - timedelta(days=1),
    )
    inbound = Message.objects.create(
        lead=waiting_target,
        source=Message.Source.GMAIL,
        external_id="target-inbound-exact",
        thread_external_id="target-thread",
        direction=Message.Direction.INBOUND,
        body="Can we review the sandbox setup?",
        sent_at=NOW - timedelta(hours=1),
    )
    Message.objects.create(
        lead=unrelated,
        operator=owner,
        source=Message.Source.GMAIL,
        external_id="unrelated-newer-outbound",
        thread_external_id="unrelated-thread",
        direction=Message.Direction.OUTBOUND,
        body="Unrelated introduction",
        sent_at=NOW - timedelta(minutes=10),
    )

    row = next(
        item for item in collect_account_evidence(now=NOW)
        if item.account_name == "Thread Exact"
    )

    assert row.decision.reminder.state.value == "needs_response"
    assert row.reminder_target_lead_id == waiting_target.id
    assert row.trigger_message_id == inbound.id


def test_unrelated_contact_outbound_does_not_fulfil_meeting_followup():
    meeting_contact = Lead.objects.create(
        first_name="Meeting",
        company_name="Meeting Thread Exact",
        email="meeting@meeting-thread.example",
    )
    unrelated = Lead.objects.create(
        first_name="Other",
        company_name="Meeting Thread Exact",
        email="other@meeting-thread.example",
    )
    meeting = Meeting.objects.create(
        lead=meeting_contact,
        source=Meeting.Source.GOOGLE_CALENDAR,
        external_id="meeting-thread-exact",
        start_at=NOW - timedelta(days=1, hours=1),
        end_at=NOW - timedelta(days=1),
        title="Working session",
    )
    Message.objects.create(
        lead=unrelated,
        source=Message.Source.GMAIL,
        external_id="unrelated-after-meeting",
        thread_external_id="unrelated-after-meeting-thread",
        direction=Message.Direction.OUTBOUND,
        body="Unrelated note",
        sent_at=NOW - timedelta(hours=1),
    )

    row = next(
        item for item in collect_account_evidence(now=NOW)
        if item.account_name == "Meeting Thread Exact"
    )

    assert row.facts.post_meeting_followup_required is True
    assert row.decision.reminder.state.value == "post_meeting_followup"
    assert row.reminder_target_lead_id == meeting_contact.id
    assert row.trigger_meeting_id == meeting.id


def test_mixed_account_exact_dont_send_target_stops_only_the_reminder():
    target = Lead.objects.create(
        first_name="Stopped",
        company_name="Mixed Outreach",
        email="stopped@mixed-outreach.example",
    )
    Lead.objects.create(
        first_name="Allowed",
        company_name="Mixed Outreach",
        email="allowed@mixed-outreach.example",
    )
    inbound = Message.objects.create(
        lead=target,
        source=Message.Source.GMAIL,
        external_id="mixed-target-inbound",
        thread_external_id="mixed-target-thread",
        direction=Message.Direction.INBOUND,
        body="Please send the details.",
        sent_at=NOW - timedelta(hours=1),
    )

    row = next(
        item for item in collect_account_evidence(
            now=NOW,
            dont_send_lead_ids={target.id},
        )
        if item.account_name == "Mixed Outreach"
    )

    assert row.decision.admitted is True
    assert row.facts.do_not_outreach is False
    assert row.reminder_target_lead_id == target.id
    assert row.trigger_message_id == inbound.id
    assert row.reminder_do_not_outreach is True


def test_same_company_name_with_two_business_domains_stays_two_accounts():
    for index, domain in enumerate(("acme-one.example", "acme-two.example"), start=1):
        lead = Lead.objects.create(
            first_name=f"Contact {index}",
            company_name="Acme",
            email=f"contact@{domain}",
        )
        Message.objects.create(
            lead=lead,
            source=Message.Source.GMAIL,
            external_id=f"acme-inbound-{index}",
            thread_external_id=f"acme-thread-{index}",
            direction=Message.Direction.INBOUND,
            body="Can we discuss this?",
            sent_at=NOW - timedelta(hours=index),
        )

    rows = [
        item for item in collect_account_evidence(now=NOW)
        if item.account_name == "Acme"
    ]

    assert len(rows) == 2
    assert {row.account_key for row in rows} == {
        "acme-one.example",
        "acme-two.example",
    }


def test_account_owner_is_not_guessed_when_multiple_recent_senders_exist():
    lead = Lead.objects.create(
        first_name="Shared",
        company_name="Shared Account",
        email="shared@shared.example",
    )
    for index, handle in enumerate(("Arian", "Athena"), start=1):
        owner = SalesOwner.objects.get(normalized_handle=handle.casefold())
        Message.objects.create(
            lead=lead,
            operator=owner,
            source=Message.Source.GMAIL,
            external_id=f"shared-outbound-{index}",
            thread_external_id="shared-thread",
            direction=Message.Direction.OUTBOUND,
            body="Follow-up",
            sent_at=NOW - timedelta(days=index),
        )
    Message.objects.create(
        lead=lead,
        source=Message.Source.GMAIL,
        external_id="shared-inbound",
        thread_external_id="shared-thread",
        direction=Message.Direction.INBOUND,
        body="Yes, let's discuss the workflow.",
        sent_at=NOW - timedelta(hours=1),
    )

    row = next(
        item for item in collect_account_evidence(now=NOW)
        if item.account_name == "Shared Account"
    )

    assert row.owner == ""


def test_completed_external_meeting_counts_without_granola_or_gemini_notes():
    lead = Lead.objects.create(
        first_name="Meeting",
        company_name="Calendar Account",
        email="meeting@calendar.example",
    )
    Meeting.objects.create(
        lead=lead,
        source=Meeting.Source.GOOGLE_CALENDAR,
        external_id="calendar-no-recorder",
        start_at=NOW - timedelta(days=3, hours=1),
        end_at=NOW - timedelta(days=3),
        title="Customer working session",
    )

    row = next(
        item for item in collect_account_evidence(now=NOW)
        if item.account_name == "Calendar Account"
    )

    assert row.decision.admitted is True
    assert row.decision.primary_reason_code.value == "recent_completed_external_meeting"
    assert row.trigger_meeting_id is not None


def test_note_owned_commitments_create_durable_action_until_explicitly_handled():
    owner = SalesOwner.objects.get(handle="Arian")
    account = Account.objects.create(name="Prescient Security")
    opportunity = Opportunity.objects.create(
        account=account,
        name="Prescient Security",
        owner=owner,
        source=Opportunity.Source.SHEET,
        stage=Opportunity.Stage.SANDBOX_PILOT,
        sales_motion_step=7,
    )
    lead = Lead.objects.create(
        first_name="Sammy",
        company_name="Prescient Security",
        email="sammy@prescient.example",
    )
    OpportunityContact.objects.create(opportunity=opportunity, lead=lead)
    note = MeetingNote.objects.create(
        source=MeetingNote.Source.GRANOLA,
        external_id="prescient-pricing-call",
        opportunity=opportunity,
        title="Arian and Sammy @ Prescient Security",
        scheduled_start_at=NOW - timedelta(days=1),
        summary_markdown=(
            "# Next Steps\n"
            "- **Send partner and white-label pricing to Sammy** (Arian)\n"
            "- **Send Sammy a proposal for Casillion's Class C journey** (Arian)\n"
            "- **Model white-label pricing for Class A** (Arian)\n"
            "- **Send customer data** (Sammy)"
        ),
        detail_status=MeetingNote.DetailStatus.COMPLETE,
        match_status=MeetingNote.MatchStatus.MATCHED,
        match_method=MeetingNote.MatchMethod.MANUAL,
    )
    # This later outbound is unrelated to the explicit meeting commitments
    # and therefore must not clear them.
    Message.objects.create(
        lead=lead,
        operator=owner,
        source=Message.Source.GMAIL,
        external_id="prescient-unrelated-outbound",
        thread_external_id="prescient-thread",
        direction=Message.Direction.OUTBOUND,
        body="Here are the partnership documents from our other discussion.",
        sent_at=NOW - timedelta(hours=12),
    )

    row = next(
        item for item in collect_account_evidence(now=NOW)
        if item.opportunity_id == str(opportunity.id)
    )

    assert row.decision.reminder.state.value == "post_meeting_followup"
    assert row.trigger_meeting_note_id == str(note.id)
    assert row.trigger_meeting_id is None
    assert row.trigger_message_id is None
    assert row.reminder_target_lead_id is None
    assert row.commitment_description == (
        "Send partner and white-label pricing to Sammy; "
        "Send Sammy a proposal for Casillion's Class C journey; "
        "Model white-label pricing for Class A"
    )
    assert "customer data" not in row.commitment_description

    report = apply_action_reconciliation([row], evaluated_at=NOW)
    assert report.actions_created == 1
    action = OpportunityAction.objects.get(opportunity=opportunity)
    assert action.kind == OpportunityAction.Kind.POST_MEETING_COMMITMENT
    assert action.description == row.commitment_description
    assert action.idempotency_key == f"v2:{opportunity.id}:meeting-note:{note.id}"

    due_row = next(
        item for item in collect_account_evidence(now=NOW)
        if item.opportunity_id == str(opportunity.id)
    )
    assert due_row.decision.reminder.state.value == "post_meeting_followup"
    assert due_row.trigger_meeting_note_id == str(note.id)
    due_report = apply_action_reconciliation([due_row], evaluated_at=NOW)
    assert due_report.actions_unchanged == 1
    action.refresh_from_db()
    assert action.idempotency_key == f"v2:{opportunity.id}:meeting-note:{note.id}"
    assert action.description == row.commitment_description

    action.status = OpportunityAction.Status.COMPLETED
    action.disposition = OpportunityAction.Disposition.HANDLED
    action.handled_at = NOW
    action.completed_at = NOW
    action.save(update_fields={
        "status",
        "disposition",
        "handled_at",
        "completed_at",
        "updated_at",
    })

    handled_row = next(
        item for item in collect_account_evidence(now=NOW)
        if item.opportunity_id == str(opportunity.id)
    )
    assert handled_row.trigger_meeting_note_id is None
    handled_report = apply_action_reconciliation([handled_row], evaluated_at=NOW)
    assert handled_report.actions_created == 0
    assert OpportunityAction.objects.filter(opportunity=opportunity).count() == 1


def test_newest_recording_wins_and_granola_breaks_same_time_ties():
    owner = SalesOwner.objects.get(handle="Arian")
    opportunity = Opportunity.objects.create(
        account=Account.objects.create(name="Recorder Priority"),
        owner=owner,
        source=Opportunity.Source.SHEET,
    )
    older = NOW - timedelta(days=2)
    newer = NOW - timedelta(days=1)
    common = {
        "opportunity": opportunity,
        "detail_status": MeetingNote.DetailStatus.COMPLETE,
        "match_status": MeetingNote.MatchStatus.MATCHED,
        "match_method": MeetingNote.MatchMethod.MANUAL,
    }
    MeetingNote.objects.create(
        source=MeetingNote.Source.GRANOLA,
        external_id="older-granola",
        scheduled_start_at=older,
        summary_markdown="- Older Granola task (Arian)",
        **common,
    )
    MeetingNote.objects.create(
        source=MeetingNote.Source.GEMINI,
        external_id="newer-gemini",
        scheduled_start_at=newer,
        summary_markdown="- Newer Gemini task (Arian)",
        **common,
    )

    gemini_row = next(
        item for item in collect_account_evidence(now=NOW)
        if item.opportunity_id == str(opportunity.id)
    )
    assert gemini_row.commitment_description == "Newer Gemini task"

    granola = MeetingNote.objects.create(
        source=MeetingNote.Source.GRANOLA,
        external_id="newer-granola",
        scheduled_start_at=newer,
        summary_markdown="- Same-time Granola task (Arian)",
        **common,
    )

    granola_row = next(
        item for item in collect_account_evidence(now=NOW)
        if item.opportunity_id == str(opportunity.id)
    )
    assert granola_row.trigger_meeting_note_id == str(granola.id)
    assert granola_row.commitment_description == "Same-time Granola task"


def test_new_inbound_outranks_an_old_replaceable_v2_due_action():
    account = Account.objects.create(name="Retarget Account")
    opportunity = Opportunity.objects.create(
        account=account,
        manual_pin=True,
        source=Opportunity.Source.SYSTEM,
    )
    old_target = Lead.objects.create(
        first_name="Old",
        company_name="Retarget Account",
        email="old@retarget.example",
    )
    new_target = Lead.objects.create(
        first_name="New",
        company_name="Retarget Account",
        email="new@retarget.example",
    )
    OpportunityContact.objects.create(opportunity=opportunity, lead=old_target)
    OpportunityContact.objects.create(opportunity=opportunity, lead=new_target)
    OpportunityAction.objects.create(
        opportunity=opportunity,
        target_lead=old_target,
        kind=OpportunityAction.Kind.NEXT_STEP,
        description="Old generated task",
        due_on=NOW.date(),
        idempotency_key="v2:define-next-step",
    )
    inbound = Message.objects.create(
        lead=new_target,
        source=Message.Source.GMAIL,
        external_id="newer-inbound",
        thread_external_id="newer-thread",
        direction=Message.Direction.INBOUND,
        body="Can you send the updated proposal?",
        sent_at=NOW - timedelta(minutes=10),
    )

    row = next(
        item for item in collect_account_evidence(now=NOW)
        if item.account_name == "Retarget Account"
    )

    assert row.decision.reminder.state.value == "needs_response"
    assert row.reminder_target_lead_id == new_target.id
    assert row.trigger_message_id == inbound.id


@pytest.mark.parametrize("source", [Opportunity.Source.MANUAL, Opportunity.Source.SHEET])
def test_nonterminal_human_managed_opportunity_is_authoritative(source):
    account = Account.objects.create(name=f"Human Managed {source}")
    opportunity = Opportunity.objects.create(
        account=account,
        source=source,
        stage=Opportunity.Stage.DISCOVERY,
        sales_motion_step=2,
    )

    row = next(
        item for item in collect_account_evidence(now=NOW)
        if item.opportunity_id == str(opportunity.id)
    )

    assert row.decision.admitted is True
    assert row.decision.primary_reason_code.value == "human_managed_opportunity"


def test_closed_lost_human_opportunity_is_not_kept_active_by_source():
    account = Account.objects.create(name="Closed Human Managed")
    opportunity = Opportunity.objects.create(
        account=account,
        source=Opportunity.Source.MANUAL,
        stage=Opportunity.Stage.CLOSED_LOST,
        sales_motion_step=None,
        closed_lost_at=NOW,
        closed_lost_reason="No current opportunity",
    )

    row = next(
        item for item in collect_account_evidence(now=NOW)
        if item.opportunity_id == str(opportunity.id)
    )

    assert row.decision.admitted is False


def test_open_human_action_is_authoritative_without_channel_evidence():
    account = Account.objects.create(name="Human Action Account")
    opportunity = Opportunity.objects.create(
        account=account,
        source=Opportunity.Source.BOOTSTRAP,
    )
    OpportunityAction.objects.create(
        opportunity=opportunity,
        kind=OpportunityAction.Kind.NEXT_STEP,
        description="Human next step",
        human_revision=1,
    )

    row = next(
        item for item in collect_account_evidence(now=NOW)
        if item.opportunity_id == str(opportunity.id)
    )

    assert row.decision.admitted is True
    assert row.decision.primary_reason_code.value == "human_current_action"


def test_unedited_legacy_system_action_does_not_admit_an_account():
    account = Account.objects.create(name="Legacy Generated Clutter")
    opportunity = Opportunity.objects.create(
        account=account,
        source=Opportunity.Source.BOOTSTRAP,
    )
    OpportunityAction.objects.create(
        opportunity=opportunity,
        kind=OpportunityAction.Kind.NEXT_STEP,
        description="Old generated task",
        idempotency_key="system:legacy-generated",
    )

    row = next(
        item for item in collect_account_evidence(now=NOW)
        if item.opportunity_id == str(opportunity.id)
    )

    assert row.decision.admitted is False
    assert row.facts.human_current_action is False
