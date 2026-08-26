from datetime import datetime, timedelta, timezone

import pytest

from crm.models import (
    Account,
    Lead,
    Meeting,
    Message,
    Opportunity,
    OpportunityAction,
    OpportunityContact,
    SalesOwner,
)
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


def test_identity_invalid_synthetic_gmail_note_meeting_is_quarantined():
    wrong = Lead.objects.create(
        first_name="John",
        last_name="S.",
        company_name="Cloudflare",
        email="john.s@cloudflare.example",
    )
    Meeting.objects.create(
        lead=wrong,
        source=Meeting.Source.GOOGLE_CALENDAR,
        external_id="gmail-note:john-allison",
        start_at=NOW - timedelta(days=3, hours=1),
        end_at=NOW - timedelta(days=3),
        title="John Allison Catchup",
        raw={
            "source": "gmail_note_email",
            "subject": "Notes: John Allison Catchup Jul 20, 2026",
        },
    )

    row = next(
        item for item in collect_account_evidence(now=NOW)
        if item.account_name == "Cloudflare"
    )

    assert row.decision.admitted is False
    assert row.trigger_meeting_id is None


def test_identity_valid_synthetic_gmail_note_meeting_remains_evidence():
    lead = Lead.objects.create(
        first_name="John",
        last_name="Allison",
        company_name="Mind Anvil",
        email="john@mindanvil.example",
    )
    meeting = Meeting.objects.create(
        lead=lead,
        source=Meeting.Source.GOOGLE_CALENDAR,
        external_id="gmail-note:john-allison-valid",
        start_at=NOW - timedelta(days=3, hours=1),
        end_at=NOW - timedelta(days=3),
        title="John Allison Catchup",
        raw={
            "source": "gmail_note_email",
            "subject": "Notes: John Allison Catchup Jul 20, 2026",
        },
    )

    row = next(
        item for item in collect_account_evidence(now=NOW)
        if item.account_name == "Mind Anvil"
    )

    assert row.decision.admitted is True
    assert row.trigger_meeting_id == meeting.id


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
