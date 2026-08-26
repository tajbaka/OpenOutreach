from datetime import UTC, datetime, timedelta

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from crm.models import (
    Account,
    Deal,
    Lead,
    Meeting,
    MeetingNote,
    Message,
    Opportunity,
    OpportunityAction,
    OpportunityContact,
    SalesOwner,
)
from linkedin.crm_service import bootstrap_opportunities, recalculate_actions
from linkedin import crm_service
from linkedin.enums import ProfileState
from linkedin.models import Campaign


def _lead(suffix: str, *, company: str = "Ramp") -> Lead:
    return Lead.objects.create(
        first_name=f"Lead{suffix}",
        last_name="Person",
        company_name=company,
        linkedin_url=f"https://www.linkedin.com/in/lead-{suffix}",
        email=f"lead-{suffix}@example.com",
    )


def _message(
    lead: Lead,
    *,
    direction: str,
    at,
    sender: str = "",
    body: str | None = None,
) -> Message:
    return Message.objects.create(
        lead=lead,
        source=Message.Source.LINKEDIN,
        external_id=f"{lead.id}-{direction}-{at.timestamp()}",
        direction=direction,
        sender=sender,
        body=(
            body
            if body is not None
            else "Could we take a closer look?"
            if direction == Message.Direction.INBOUND
            else "Hello"
        ),
        sent_at=at,
    )


@pytest.mark.django_db
def test_bootstrap_is_conservative_but_links_same_account_contacts():
    now = timezone.now()
    engaged = _lead("engaged")
    stakeholder = _lead("stakeholder")
    _lead("unrelated", company="No Engagement Inc")
    _message(
        engaged,
        direction=Message.Direction.OUTBOUND,
        at=now - timedelta(days=2),
        sender="Arian",
    )
    _message(
        engaged,
        direction=Message.Direction.INBOUND,
        at=now - timedelta(days=1),
    )

    report = bootstrap_opportunities(apply=True, now=now)

    assert report.opportunities_created == 1
    opportunity = Opportunity.objects.get()
    assert opportunity.account.name == "Ramp"
    assert opportunity.owner.handle == "Arian"
    assert set(opportunity.contacts.values_list("lead_id", flat=True)) == {
        engaged.id,
        stakeholder.id,
    }
    assert not Account.objects.filter(name="No Engagement Inc").exists()


@pytest.mark.django_db
def test_bootstrap_reports_ambiguous_owner_instead_of_using_message_volume():
    now = timezone.now()
    lead = _lead("ambiguous-owner")
    for offset in range(3):
        _message(
            lead,
            direction=Message.Direction.OUTBOUND,
            at=now - timedelta(days=5 - offset),
            sender="Arian",
        )
    _message(
        lead,
        direction=Message.Direction.OUTBOUND,
        at=now - timedelta(days=1),
        sender="Chuka",
    )
    _message(
        lead,
        direction=Message.Direction.INBOUND,
        at=now,
    )

    report = bootstrap_opportunities(apply=True, now=now)

    assert len(report.ambiguous_owners) == 1
    assert Opportunity.objects.get().owner is None


@pytest.mark.django_db
def test_bootstrap_skips_duplicate_account_identity():
    now = timezone.now()
    Account.objects.create(name="Ramp")
    Account.objects.create(name="RAMP")
    lead = _lead("ambiguous-account")
    _message(lead, direction=Message.Direction.INBOUND, at=now)

    report = bootstrap_opportunities(apply=True, now=now)

    assert len(report.ambiguous_accounts) == 1
    assert Opportunity.objects.count() == 0


@pytest.mark.django_db
def test_completed_outreach_deal_is_not_closed_won():
    now = timezone.now()
    user = User.objects.create(username="arian")
    campaign = Campaign.objects.create(name="test", user=user)
    lead = _lead("completed")
    Deal.objects.create(lead=lead, campaign=campaign, state=ProfileState.COMPLETED)
    _message(lead, direction=Message.Direction.INBOUND, at=now)

    bootstrap_opportunities(apply=True, now=now)

    opportunity = Opportunity.objects.get()
    assert opportunity.stage == Opportunity.Stage.DISCOVERY
    assert opportunity.closed_won_at is None


@pytest.mark.django_db
def test_explicit_stable_lead_bootstrap_is_conservative_and_reported():
    now = timezone.now()
    explicit = _lead("explicit-bootstrap", company="Explicit Bootstrap Account")
    disqualified = _lead(
        "explicit-bootstrap-disqualified",
        company="Disqualified Explicit Account",
    )
    disqualified.disqualified = True
    disqualified.save(update_fields={"disqualified"})

    first = bootstrap_opportunities(
        apply=True,
        now=now,
        explicit_lead_ids={explicit.id, disqualified.id, 999999999},
    )
    second = bootstrap_opportunities(
        apply=True,
        now=now,
        explicit_lead_ids={explicit.id},
    )

    opportunity = Opportunity.objects.get()
    assert first.explicit_candidates == 1
    assert first.explicit_missing_or_disqualified == 2
    assert first.opportunities_created == 1
    assert second.opportunities_created == 0
    assert second.opportunities_existing == 1
    assert opportunity.stage == Opportunity.Stage.DISCOVERY
    assert opportunity.sales_motion_step == 2
    assert opportunity.closed_won_at is None


@pytest.mark.django_db
def test_action_refresh_is_idempotent_and_routes_to_explicit_owner():
    now = timezone.now()
    owner = SalesOwner.objects.get(handle="Arian")
    account = Account.objects.create(name="Ramp")
    opportunity = Opportunity.objects.create(
        account=account,
        owner=owner,
        stage=Opportunity.Stage.DISCOVERY,
        sales_motion_step=2,
    )
    lead = _lead("action")
    OpportunityContact.objects.create(opportunity=opportunity, lead=lead)
    _message(
        lead,
        direction=Message.Direction.OUTBOUND,
        at=now - timedelta(hours=2),
        sender="Chuka",
    )
    _message(
        lead,
        direction=Message.Direction.INBOUND,
        at=now - timedelta(hours=1),
    )

    first = recalculate_actions(apply=True, now=now)
    second = recalculate_actions(apply=True, now=now)

    assert OpportunityAction.objects.count() == 1
    action = OpportunityAction.objects.get()
    assert action.target_lead_id == lead.id
    assert first.actions_created == 1
    assert second.actions_created == 0
    assert second.daily_by_owner == {"Arian": 1}
    assert second.evaluations[0].target_lead_id == lead.id


@pytest.mark.django_db
def test_outbound_response_completes_needs_response_without_creating_busywork():
    now = timezone.now()
    owner = SalesOwner.objects.get(handle="Arian")
    opportunity = Opportunity.objects.create(
        account=Account.objects.create(name="Response Account"),
        owner=owner,
        stage=Opportunity.Stage.DISCOVERY,
        sales_motion_step=2,
    )
    lead = _lead("responded", company="Response Account")
    OpportunityContact.objects.create(opportunity=opportunity, lead=lead)
    inbound = _message(
        lead,
        direction=Message.Direction.INBOUND,
        at=now - timedelta(hours=2),
    )
    action = OpportunityAction.objects.create(
        opportunity=opportunity,
        kind=OpportunityAction.Kind.NEEDS_RESPONSE,
        description="Respond",
        trigger_message=inbound,
    )
    outbound = _message(
        lead,
        direction=Message.Direction.OUTBOUND,
        at=now - timedelta(hours=1),
        sender="Arian",
    )

    report = recalculate_actions(apply=True, now=now)

    action.refresh_from_db()
    assert report.actions_completed == 1
    assert report.actions_created == 0
    assert action.status == OpportunityAction.Status.COMPLETED
    assert action.disposition == OpportunityAction.Disposition.SENT
    assert action.sent_at == outbound.sent_at
    assert report.evaluations[0].placement.surface == "none"


@pytest.mark.django_db
def test_human_completed_trigger_does_not_resurface_the_same_inbound():
    now = timezone.now()
    owner = SalesOwner.objects.get(handle="Arian")
    opportunity = Opportunity.objects.create(
        account=Account.objects.create(name="Handled Account"),
        owner=owner,
        stage=Opportunity.Stage.DISCOVERY,
        sales_motion_step=2,
    )
    lead = _lead("handled-trigger", company="Handled Account")
    OpportunityContact.objects.create(opportunity=opportunity, lead=lead)
    inbound = _message(
        lead,
        direction=Message.Direction.INBOUND,
        at=now - timedelta(hours=1),
    )
    OpportunityAction.objects.create(
        opportunity=opportunity,
        kind=OpportunityAction.Kind.NEEDS_RESPONSE,
        status=OpportunityAction.Status.COMPLETED,
        description="Handled outside the stored thread",
        disposition=OpportunityAction.Disposition.SENT,
        completed_at=now,
        trigger_message=inbound,
        idempotency_key=f"system:needs_response:message:{inbound.id}",
    )

    report = recalculate_actions(apply=True, now=now)

    assert report.actions_created == 0
    assert report.evaluations[0].placement.surface == "none"
    assert not OpportunityAction.objects.filter(
        status__in=[OpportunityAction.Status.OPEN, OpportunityAction.Status.WAITING],
    ).exists()


@pytest.mark.django_db
def test_completed_legacy_send_without_trigger_does_not_create_immediate_busywork():
    now = timezone.now()
    owner = SalesOwner.objects.get(handle="Arian")
    opportunity = Opportunity.objects.create(
        account=Account.objects.create(name="Legacy Sent Account"),
        owner=owner,
        stage=Opportunity.Stage.DISCOVERY,
        sales_motion_step=2,
    )
    lead = _lead("legacy-sent", company="Legacy Sent Account")
    OpportunityContact.objects.create(opportunity=opportunity, lead=lead)
    _message(
        lead,
        direction=Message.Direction.INBOUND,
        at=now - timedelta(days=2),
    )
    OpportunityAction.objects.create(
        opportunity=opportunity,
        kind=OpportunityAction.Kind.FOLLOWUP,
        status=OpportunityAction.Status.COMPLETED,
        description="Legacy follow-up was marked sent",
        disposition=OpportunityAction.Disposition.SENT,
        completed_at=now,
        idempotency_key="legacy:sent:no-trigger",
    )

    report = recalculate_actions(apply=True, now=now)

    assert report.actions_created == 0
    assert report.evaluations[0].placement.surface == "none"


@pytest.mark.django_db
def test_elapsed_meeting_prep_becomes_a_real_post_meeting_next_step():
    now = timezone.now()
    owner = SalesOwner.objects.get(handle="Arian")
    opportunity = Opportunity.objects.create(
        account=Account.objects.create(name="Meeting Account"),
        owner=owner,
        stage=Opportunity.Stage.EVALUATION,
        sales_motion_step=5,
    )
    lead = _lead("meeting-finished", company="Meeting Account")
    OpportunityContact.objects.create(opportunity=opportunity, lead=lead)
    meeting = Meeting.objects.create(
        source=Meeting.Source.GOOGLE_CALENDAR,
        external_id="meeting-finished",
        lead=lead,
        opportunity=opportunity,
        start_at=now - timedelta(hours=1),
        title="Working session",
    )
    prep = OpportunityAction.objects.create(
        opportunity=opportunity,
        kind=OpportunityAction.Kind.MEETING_PREP,
        description="Prepare",
        trigger_meeting=meeting,
    )

    report = recalculate_actions(apply=True, now=now)

    prep.refresh_from_db()
    current = OpportunityAction.objects.get(status=OpportunityAction.Status.OPEN)
    assert report.actions_completed == 1
    assert report.actions_created == 1
    assert prep.status == OpportunityAction.Status.COMPLETED
    assert current.kind == OpportunityAction.Kind.NEXT_STEP
    assert report.evaluations[0].placement.surface == "daily"


@pytest.mark.django_db
def test_granola_context_is_primary_and_does_not_widen_old_eligibility():
    now = timezone.now()
    owner = SalesOwner.objects.get(handle="Arian")
    account = Account.objects.create(name="Ramp")
    opportunity = Opportunity.objects.create(
        account=account,
        owner=owner,
        stage=Opportunity.Stage.EVALUATION,
        sales_motion_step=5,
    )
    lead = _lead("context")
    OpportunityContact.objects.create(opportunity=opportunity, lead=lead)
    meeting = Meeting.objects.create(
        source=Meeting.Source.GOOGLE_CALENDAR,
        external_id="old-ramp-call",
        lead=lead,
        opportunity=opportunity,
        start_at=now - timedelta(days=90),
        title="Ramp working session",
        gemini_doc_id="gemini-1",
        gemini_notes_raw="Gemini fallback",
    )
    MeetingNote.objects.create(
        source=MeetingNote.Source.GRANOLA,
        external_id="granola-1",
        meeting=meeting,
        opportunity=opportunity,
        title="Ramp working session",
        content="Primary summary",
        summary_text="Primary summary",
        detail_status=MeetingNote.DetailStatus.COMPLETE,
        match_status=MeetingNote.MatchStatus.MATCHED,
        match_method=MeetingNote.MatchMethod.ATTENDEE_EMAIL,
    )

    report = recalculate_actions(apply=True, now=now)

    evaluation = report.evaluations[0]
    assert evaluation.context_source == "granola"
    assert evaluation.placement.surface == "archive"
    assert report.actions_created == 0


@pytest.mark.django_db
def test_dont_send_contact_is_excluded_from_daily_queue():
    now = timezone.now()
    owner = SalesOwner.objects.get(handle="Arian")
    opportunity = Opportunity.objects.create(
        account=Account.objects.create(name="Ramp"),
        owner=owner,
        stage=Opportunity.Stage.DISCOVERY,
        sales_motion_step=2,
    )
    lead = _lead("dnc")
    OpportunityContact.objects.create(opportunity=opportunity, lead=lead)
    _message(lead, direction=Message.Direction.INBOUND, at=now)

    report = recalculate_actions(
        apply=True,
        now=now,
        dont_send_lead_ids={lead.id},
    )

    assert report.daily_by_owner == {}
    assert report.evaluations[0].placement.surface == "excluded"
    assert report.evaluations[0].placement.category == "dont_send"


@pytest.mark.django_db
def test_waiting_action_uses_configured_business_date(monkeypatch):
    monkeypatch.setattr(crm_service, "ACTIVE_TIMEZONE", "America/Toronto")
    owner = SalesOwner.objects.get(handle="Arian")
    opportunity = Opportunity.objects.create(
        account=Account.objects.create(name="Timezone Account"),
        owner=owner,
        stage=Opportunity.Stage.DISCOVERY,
        sales_motion_step=2,
    )
    lead = _lead("timezone", company="Timezone Account")
    OpportunityContact.objects.create(opportunity=opportunity, lead=lead)
    OpportunityAction.objects.create(
        opportunity=opportunity,
        target_lead=lead,
        kind=OpportunityAction.Kind.NEXT_STEP,
        status=OpportunityAction.Status.WAITING,
        description="Call tomorrow",
        due_on=datetime(2026, 8, 27, tzinfo=UTC).date(),
        waiting_until=datetime(2026, 8, 27, tzinfo=UTC).date(),
    )

    before_local_midnight = recalculate_actions(
        apply=False,
        now=datetime(2026, 8, 27, 2, 0, tzinfo=UTC),
    )
    after_local_midnight = recalculate_actions(
        apply=False,
        now=datetime(2026, 8, 27, 5, 0, tzinfo=UTC),
    )

    assert before_local_midnight.evaluations[0].placement.surface == "waiting"
    assert after_local_midnight.evaluations[0].placement.surface == "daily"


@pytest.mark.django_db
def test_existing_unowned_opportunity_gets_only_unambiguous_owner_evidence():
    now = timezone.now()
    account = Account.objects.create(name="Owner Fill Account")
    opportunity = Opportunity.objects.create(
        account=account,
        stage=Opportunity.Stage.DISCOVERY,
        sales_motion_step=2,
    )
    lead = _lead("owner-fill", company="Owner Fill Account")
    _message(
        lead,
        direction=Message.Direction.OUTBOUND,
        at=now - timedelta(hours=2),
        sender="Arian",
    )
    _message(lead, direction=Message.Direction.INBOUND, at=now - timedelta(hours=1))

    report = bootstrap_opportunities(apply=True, now=now)

    opportunity.refresh_from_db()
    assert report.owners_assigned == 1
    assert opportunity.owner.handle == "Arian"


@pytest.mark.django_db
def test_explicit_opportunity_owner_is_never_overwritten_by_inference():
    now = timezone.now()
    explicit_owner = SalesOwner.objects.get(handle="Chuka")
    account = Account.objects.create(name="Explicit Owner Account")
    opportunity = Opportunity.objects.create(
        account=account,
        owner=explicit_owner,
        stage=Opportunity.Stage.DISCOVERY,
        sales_motion_step=2,
    )
    lead = _lead("explicit-owner", company="Explicit Owner Account")
    _message(
        lead,
        direction=Message.Direction.OUTBOUND,
        at=now - timedelta(hours=2),
        sender="Arian",
    )
    _message(lead, direction=Message.Direction.INBOUND, at=now - timedelta(hours=1))

    report = bootstrap_opportunities(apply=True, now=now)

    opportunity.refresh_from_db()
    assert report.owners_assigned == 0
    assert opportunity.owner == explicit_owner


@pytest.mark.django_db
def test_outbound_to_other_contact_cannot_complete_targeted_response_action():
    now = timezone.now()
    opportunity = Opportunity.objects.create(
        account=Account.objects.create(name="Cross Contact Account"),
        owner=SalesOwner.objects.get(handle="Arian"),
        stage=Opportunity.Stage.DISCOVERY,
        sales_motion_step=2,
    )
    target = _lead("cross-target", company="Cross Contact Account")
    other = _lead("cross-other", company="Cross Contact Account")
    OpportunityContact.objects.create(opportunity=opportunity, lead=target)
    OpportunityContact.objects.create(opportunity=opportunity, lead=other)
    inbound = _message(
        target,
        direction=Message.Direction.INBOUND,
        at=now - timedelta(hours=2),
    )
    action = OpportunityAction.objects.create(
        opportunity=opportunity,
        target_lead=target,
        kind=OpportunityAction.Kind.NEEDS_RESPONSE,
        description="Reply to target",
        trigger_message=inbound,
    )
    _message(
        other,
        direction=Message.Direction.OUTBOUND,
        at=now - timedelta(hours=1),
        sender="Arian",
    )

    report = recalculate_actions(apply=True, now=now)

    action.refresh_from_db()
    assert action.status == OpportunityAction.Status.OPEN
    assert report.actions_completed == 0
    assert report.evaluations[0].target_lead_id == target.id


@pytest.mark.django_db
def test_response_completion_then_routes_next_contact_inbound_separately():
    now = timezone.now()
    opportunity = Opportunity.objects.create(
        account=Account.objects.create(name="Two Replies Account"),
        owner=SalesOwner.objects.get(handle="Arian"),
        stage=Opportunity.Stage.DISCOVERY,
        sales_motion_step=2,
    )
    first_lead = _lead("reply-first", company="Two Replies Account")
    second_lead = _lead("reply-second", company="Two Replies Account")
    OpportunityContact.objects.create(opportunity=opportunity, lead=first_lead)
    OpportunityContact.objects.create(opportunity=opportunity, lead=second_lead)
    first_inbound = _message(
        first_lead,
        direction=Message.Direction.INBOUND,
        at=now - timedelta(hours=4),
    )
    first_action = OpportunityAction.objects.create(
        opportunity=opportunity,
        target_lead=first_lead,
        kind=OpportunityAction.Kind.NEEDS_RESPONSE,
        description="Reply to first contact",
        trigger_message=first_inbound,
    )
    _message(
        first_lead,
        direction=Message.Direction.OUTBOUND,
        at=now - timedelta(hours=2),
        sender="Arian",
    )
    second_inbound = _message(
        second_lead,
        direction=Message.Direction.INBOUND,
        at=now - timedelta(hours=1),
    )

    report = recalculate_actions(apply=True, now=now)

    first_action.refresh_from_db()
    current = OpportunityAction.objects.get(status=OpportunityAction.Status.OPEN)
    assert report.actions_completed == 1
    assert report.actions_created == 1
    assert first_action.status == OpportunityAction.Status.COMPLETED
    assert current.target_lead_id == second_lead.id
    assert current.trigger_message_id == second_inbound.id
    assert report.evaluations[0].target_lead_id == second_lead.id


@pytest.mark.django_db
def test_fresh_inbound_supersedes_only_untouched_system_action():
    now = timezone.now()
    owner = SalesOwner.objects.get(handle="Arian")

    def make_case(suffix: str, *, human_revision: int):
        opportunity = Opportunity.objects.create(
            account=Account.objects.create(name=f"Supersede {suffix}"),
            owner=owner,
            stage=Opportunity.Stage.DISCOVERY,
            sales_motion_step=2,
        )
        lead = _lead(f"supersede-{suffix}", company=f"Supersede {suffix}")
        OpportunityContact.objects.create(opportunity=opportunity, lead=lead)
        action = OpportunityAction.objects.create(
            opportunity=opportunity,
            target_lead=lead,
            kind=OpportunityAction.Kind.NEXT_STEP,
            description="Existing next step",
            human_revision=human_revision,
            idempotency_key=f"system:next_step:test:{suffix}",
        )
        inbound = _message(
            lead,
            direction=Message.Direction.INBOUND,
            at=now - timedelta(minutes=5),
        )
        return opportunity, lead, action, inbound

    _system_opp, system_lead, system_action, system_inbound = make_case(
        "system",
        human_revision=0,
    )
    _human_opp, human_lead, human_action, _human_inbound = make_case(
        "human",
        human_revision=1,
    )

    report = recalculate_actions(apply=True, now=now)

    system_action.refresh_from_db()
    human_action.refresh_from_db()
    system_current = OpportunityAction.objects.get(
        opportunity=system_action.opportunity,
        status=OpportunityAction.Status.OPEN,
    )
    assert system_action.status == OpportunityAction.Status.CANCELLED
    assert system_current.kind == OpportunityAction.Kind.NEEDS_RESPONSE
    assert system_current.target_lead_id == system_lead.id
    assert system_current.trigger_message_id == system_inbound.id
    assert human_action.status == OpportunityAction.Status.OPEN
    assert human_action.target_lead_id == human_lead.id
    assert report.actions_superseded == 1


@pytest.mark.django_db
def test_terminal_dnc_and_disqualified_opportunities_create_no_actions():
    now = timezone.now()
    owner = SalesOwner.objects.get(handle="Arian")

    terminal = Opportunity.objects.create(
        account=Account.objects.create(name="Terminal Suppression"),
        owner=owner,
        stage=Opportunity.Stage.CLOSED_WON,
        manual_pin=True,
    )
    terminal_lead = _lead("terminal-suppression", company="Terminal Suppression")
    OpportunityContact.objects.create(opportunity=terminal, lead=terminal_lead)

    dnc = Opportunity.objects.create(
        account=Account.objects.create(name="DNC Suppression"),
        owner=owner,
        stage=Opportunity.Stage.DISCOVERY,
        sales_motion_step=2,
        manual_pin=True,
    )
    dnc_lead = _lead("dnc-suppression", company="DNC Suppression")
    OpportunityContact.objects.create(opportunity=dnc, lead=dnc_lead)

    disqualified = Opportunity.objects.create(
        account=Account.objects.create(name="Disqualified Suppression"),
        owner=owner,
        stage=Opportunity.Stage.DISCOVERY,
        sales_motion_step=2,
        manual_pin=True,
    )
    disqualified_lead = _lead(
        "disqualified-suppression",
        company="Disqualified Suppression",
    )
    disqualified_lead.disqualified = True
    disqualified_lead.save(update_fields={"disqualified"})
    OpportunityContact.objects.create(
        opportunity=disqualified,
        lead=disqualified_lead,
    )

    report = recalculate_actions(
        apply=True,
        now=now,
        dont_send_lead_ids={dnc_lead.id},
    )

    assert OpportunityAction.objects.count() == 0
    surfaces = {
        evaluation.account: evaluation.placement.category
        for evaluation in report.evaluations
    }
    assert surfaces == {
        "DNC Suppression": "dont_send",
        "Disqualified Suppression": "disqualified",
        "Terminal Suppression": "terminal",
    }


@pytest.mark.django_db
def test_non_actionable_failed_opportunity_cannot_be_revived_by_manual_pin():
    now = timezone.now()
    user = User.objects.create(username="failed-owner")
    campaign = Campaign.objects.create(name="failed-campaign", user=user)
    lead = _lead("failed-suppression", company="Failed Suppression")
    Deal.objects.create(lead=lead, campaign=campaign, state=ProfileState.FAILED)
    opportunity = Opportunity.objects.create(
        account=Account.objects.create(name="Failed Suppression"),
        owner=SalesOwner.objects.get(handle="Arian"),
        stage=Opportunity.Stage.DISCOVERY,
        sales_motion_step=2,
        manual_pin=True,
    )
    OpportunityContact.objects.create(opportunity=opportunity, lead=lead)

    report = recalculate_actions(apply=True, now=now)

    assert OpportunityAction.objects.count() == 0
    assert report.evaluations[0].placement.category == "failed"


@pytest.mark.django_db
def test_manual_pin_creates_one_stable_targeted_action_past_archive_age():
    now = timezone.now()
    opportunity = Opportunity.objects.create(
        account=Account.objects.create(name="Pinned Account"),
        owner=SalesOwner.objects.get(handle="Arian"),
        stage=Opportunity.Stage.DISCOVERY,
        sales_motion_step=2,
        manual_pin=True,
    )
    lead = _lead("manual-pin", company="Pinned Account")
    OpportunityContact.objects.create(
        opportunity=opportunity,
        lead=lead,
        role=OpportunityContact.Role.CHAMPION,
    )
    _message(
        lead,
        direction=Message.Direction.OUTBOUND,
        at=now - timedelta(days=120),
        sender="Arian",
    )

    first = recalculate_actions(apply=True, now=now)
    second = recalculate_actions(apply=True, now=now)

    action = OpportunityAction.objects.get()
    assert action.target_lead_id == lead.id
    assert first.actions_created == 1
    assert second.actions_created == 0
    assert second.evaluations[0].action_id == str(action.id)
    assert second.evaluations[0].placement.category == "manual_pin"


@pytest.mark.django_db
def test_meeting_prep_completes_at_end_or_start_fallback():
    now = timezone.now()
    opportunity = Opportunity.objects.create(
        account=Account.objects.create(name="Meeting End Account"),
        owner=SalesOwner.objects.get(handle="Arian"),
        stage=Opportunity.Stage.EVALUATION,
        sales_motion_step=5,
    )
    lead = _lead("meeting-end", company="Meeting End Account")
    OpportunityContact.objects.create(opportunity=opportunity, lead=lead)
    meeting = Meeting.objects.create(
        source=Meeting.Source.GOOGLE_CALENDAR,
        external_id="meeting-with-end",
        lead=lead,
        opportunity=opportunity,
        start_at=now - timedelta(minutes=15),
        end_at=now + timedelta(minutes=15),
        title="Live working session",
    )
    action = OpportunityAction.objects.create(
        opportunity=opportunity,
        target_lead=lead,
        kind=OpportunityAction.Kind.MEETING_PREP,
        description="Prepare",
        trigger_meeting=meeting,
    )

    during = recalculate_actions(apply=True, now=now)
    action.refresh_from_db()
    assert during.actions_completed == 0
    assert action.status == OpportunityAction.Status.OPEN

    after = recalculate_actions(apply=True, now=now + timedelta(minutes=16))
    action.refresh_from_db()
    assert after.actions_completed == 1
    assert action.status == OpportunityAction.Status.COMPLETED


@pytest.mark.django_db
def test_meeting_prep_without_real_trigger_is_not_a_daily_action():
    now = timezone.now()
    opportunity = Opportunity.objects.create(
        account=Account.objects.create(name="Invalid Prep Account"),
        owner=SalesOwner.objects.get(handle="Arian"),
        stage=Opportunity.Stage.EVALUATION,
        sales_motion_step=5,
    )
    lead = _lead("invalid-prep", company="Invalid Prep Account")
    OpportunityContact.objects.create(opportunity=opportunity, lead=lead)
    action = OpportunityAction.objects.create(
        opportunity=opportunity,
        target_lead=lead,
        kind=OpportunityAction.Kind.MEETING_PREP,
        description="Legacy Meeting Booked label",
        human_revision=1,
    )

    report = recalculate_actions(apply=True, now=now)

    action.refresh_from_db()
    assert action.status == OpportunityAction.Status.OPEN
    assert report.evaluations[0].placement.surface == "excluded"
    assert report.evaluations[0].placement.reason == "meeting_prep_without_real_meeting"


@pytest.mark.django_db
def test_targetless_due_action_is_manual_recovery_not_sender_work():
    now = timezone.now()
    opportunity = Opportunity.objects.create(
        account=Account.objects.create(name="Targetless Action Account"),
        owner=SalesOwner.objects.get(handle="Arian"),
        stage=Opportunity.Stage.DISCOVERY,
        sales_motion_step=2,
        last_meaningful_activity_at=now,
    )
    for suffix in ("one", "two"):
        lead = _lead(f"targetless-{suffix}", company="Targetless Action Account")
        OpportunityContact.objects.create(opportunity=opportunity, lead=lead)
    action = OpportunityAction.objects.create(
        opportunity=opportunity,
        kind=OpportunityAction.Kind.NEXT_STEP,
        description="Assign a recipient",
        due_on=crm_service._business_date(now),
    )

    report = recalculate_actions(apply=True, now=now)

    action.refresh_from_db()
    evaluation = report.evaluations[0]
    assert action.status == OpportunityAction.Status.OPEN
    assert evaluation.target_lead_id is None
    assert evaluation.placement.surface == "recovery"
    assert evaluation.placement.reason == "unresolved_action_target"
    assert report.daily_by_owner == {}


@pytest.mark.django_db
def test_cross_contact_inbound_conflict_preserves_human_action_in_recovery():
    now = timezone.now()
    opportunity = Opportunity.objects.create(
        account=Account.objects.create(name="Human Routing Conflict"),
        owner=SalesOwner.objects.get(handle="Arian"),
        stage=Opportunity.Stage.DISCOVERY,
        sales_motion_step=2,
    )
    action_target = _lead("human-action-target", company="Human Routing Conflict")
    inbound_contact = _lead("human-inbound-contact", company="Human Routing Conflict")
    OpportunityContact.objects.create(opportunity=opportunity, lead=action_target)
    OpportunityContact.objects.create(opportunity=opportunity, lead=inbound_contact)
    action = OpportunityAction.objects.create(
        opportunity=opportunity,
        target_lead=action_target,
        kind=OpportunityAction.Kind.NEXT_STEP,
        description="Human-authored action for A",
        human_revision=2,
        idempotency_key="system:next_step:human-revised",
    )
    _message(
        inbound_contact,
        direction=Message.Direction.INBOUND,
        at=now - timedelta(minutes=10),
    )

    report = recalculate_actions(apply=True, now=now)

    action.refresh_from_db()
    evaluation = report.evaluations[0]
    assert action.status == OpportunityAction.Status.OPEN
    assert action.target_lead_id == action_target.id
    assert evaluation.target_lead_id == action_target.id
    assert evaluation.placement.surface == "recovery"
    assert evaluation.placement.reason == (
        "inbound_target_conflicts_with_current_action"
    )
    assert report.daily_by_owner == {}


@pytest.mark.django_db
def test_persisted_target_wins_over_conflicting_trigger_for_completion():
    now = timezone.now()
    opportunity = Opportunity.objects.create(
        account=Account.objects.create(name="Target Trigger Conflict"),
        owner=SalesOwner.objects.get(handle="Arian"),
        stage=Opportunity.Stage.DISCOVERY,
        sales_motion_step=2,
    )
    trigger_lead = _lead("conflict-trigger", company="Target Trigger Conflict")
    target_lead = _lead("conflict-target", company="Target Trigger Conflict")
    OpportunityContact.objects.create(opportunity=opportunity, lead=trigger_lead)
    OpportunityContact.objects.create(opportunity=opportunity, lead=target_lead)
    inbound = _message(
        trigger_lead,
        direction=Message.Direction.INBOUND,
        at=now - timedelta(hours=2),
    )
    action = OpportunityAction.objects.create(
        opportunity=opportunity,
        target_lead=target_lead,
        kind=OpportunityAction.Kind.NEEDS_RESPONSE,
        description="Contradictory human action",
        trigger_message=inbound,
        human_revision=1,
    )
    _message(
        target_lead,
        direction=Message.Direction.OUTBOUND,
        at=now - timedelta(hours=1),
        sender="Arian",
    )

    report = recalculate_actions(apply=True, now=now)

    action.refresh_from_db()
    assert report.actions_completed == 0
    assert action.status == OpportunityAction.Status.OPEN
    assert report.evaluations[0].target_lead_id == target_lead.id
    assert report.evaluations[0].placement.surface == "recovery"


@pytest.mark.django_db
def test_completed_conflicting_trigger_does_not_handle_another_contact():
    now = timezone.now()
    opportunity = Opportunity.objects.create(
        account=Account.objects.create(name="Completed Target Conflict"),
        owner=SalesOwner.objects.get(handle="Arian"),
        stage=Opportunity.Stage.DISCOVERY,
        sales_motion_step=2,
    )
    trigger_lead = _lead(
        "completed-conflict-trigger",
        company="Completed Target Conflict",
    )
    target_lead = _lead(
        "completed-conflict-target",
        company="Completed Target Conflict",
    )
    OpportunityContact.objects.create(opportunity=opportunity, lead=trigger_lead)
    OpportunityContact.objects.create(opportunity=opportunity, lead=target_lead)
    inbound = _message(
        trigger_lead,
        direction=Message.Direction.INBOUND,
        at=now - timedelta(hours=1),
    )
    OpportunityAction.objects.create(
        opportunity=opportunity,
        target_lead=target_lead,
        kind=OpportunityAction.Kind.NEEDS_RESPONSE,
        status=OpportunityAction.Status.COMPLETED,
        description="Completed for the other contact",
        disposition=OpportunityAction.Disposition.HANDLED,
        completed_at=now,
        trigger_message=inbound,
    )

    report = recalculate_actions(apply=True, now=now)

    current = OpportunityAction.objects.get(status=OpportunityAction.Status.OPEN)
    assert report.actions_created == 1
    assert current.target_lead_id == trigger_lead.id
    assert current.trigger_message_id == inbound.id


@pytest.mark.django_db
def test_ambiguous_manual_pin_stays_visible_in_recovery_past_archive_age():
    now = timezone.now()
    opportunity = Opportunity.objects.create(
        account=Account.objects.create(name="Ambiguous Pinned Account"),
        owner=SalesOwner.objects.get(handle="Arian"),
        stage=Opportunity.Stage.DISCOVERY,
        sales_motion_step=2,
        manual_pin=True,
        last_meaningful_activity_at=now - timedelta(days=400),
    )
    for suffix in ("one", "two"):
        lead = _lead(f"ambiguous-pin-{suffix}", company="Ambiguous Pinned Account")
        OpportunityContact.objects.create(opportunity=opportunity, lead=lead)

    report = recalculate_actions(apply=True, now=now)

    evaluation = report.evaluations[0]
    assert OpportunityAction.objects.count() == 0
    assert evaluation.target_lead_id is None
    assert evaluation.placement.surface == "recovery"
    assert evaluation.placement.category == "manual_review"
    assert evaluation.placement.reason == "manual_pin_needs_target"
    assert report.daily_by_owner == {}
