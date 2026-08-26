from datetime import timedelta

import pytest
from django.utils import timezone

from crm.models import (
    Account,
    Lead,
    Opportunity,
    OpportunityAction,
    OpportunityContact,
    OpportunitySheetState,
    OpportunityStageEvent,
    SalesOwner,
)
from linkedin.crm_sheet_import import (
    apply_followup_imports,
    apply_opportunity_imports,
    baseline_by_opportunity_id,
    commit_followup_baselines,
    commit_sheet_baselines,
    read_people_dont_send_lead_ids,
)
from linkedin.crm_publish import followup_imports_from_sheet_rows
from linkedin.notifications import crm_sheets
from linkedin.exceptions import SheetsError


def _opportunity():
    return Opportunity.objects.create(
        account=Account.objects.create(name="Ramp"),
        stage=Opportunity.Stage.DISCOVERY,
        sales_motion_step=2,
    )


def _imports(opportunity, values):
    return [
        {
            "stable_id": str(opportunity.id),
            "field": field,
            "value": value,
        }
        for field, value in values.items()
    ]


@pytest.mark.django_db
def test_valid_human_fields_apply_atomically_with_sheet_stage_event():
    opportunity = _opportunity()
    lead = Lead.objects.create(
        first_name="Zelia",
        company_name="Ramp",
        linkedin_url="https://linkedin.com/in/ramp-champion",
    )
    OpportunityContact.objects.create(
        opportunity=opportunity,
        lead=lead,
        role=OpportunityContact.Role.CHAMPION,
    )
    due = (timezone.localdate() + timedelta(days=2)).isoformat()

    report = apply_opportunity_imports(
        _imports(opportunity, {
            crm_sheets.COL_OWNER: "Arian",
            crm_sheets.COL_STAGE: "Evaluation",
            crm_sheets.COL_SALES_MOTION_STEP: "6",
            crm_sheets.COL_NEXT_ACTION: "Prepare a tailored sandbox",
            crm_sheets.COL_NEXT_ACTION_DUE: due,
            crm_sheets.COL_MANUAL_PIN: "TRUE",
            crm_sheets.COL_VALUE: "25000",
            crm_sheets.COL_PROBABILITY: "40",
        }),
        dry_run=False,
    )

    opportunity.refresh_from_db()
    action = opportunity.actions.get()
    assert report.invalid == []
    assert opportunity.owner.handle == "Arian"
    assert opportunity.stage == Opportunity.Stage.EVALUATION
    assert opportunity.sales_motion_step == 6
    assert opportunity.manual_pin is True
    assert str(opportunity.value) == "25000.00"
    assert action.description == "Prepare a tailored sandbox"
    assert action.target_lead_id == lead.id
    assert action.due_on.isoformat() == due
    event = OpportunityStageEvent.objects.filter(opportunity=opportunity).latest("changed_at")
    assert event.source == OpportunityStageEvent.Source.SHEET


@pytest.mark.django_db
def test_invalid_owner_is_reported_without_partial_changes():
    opportunity = _opportunity()

    report = apply_opportunity_imports(
        _imports(opportunity, {
            crm_sheets.COL_OWNER: "Arain",
            crm_sheets.COL_MANUAL_PIN: "TRUE",
        }),
        dry_run=False,
    )

    opportunity.refresh_from_db()
    assert len(report.invalid) == 1
    assert opportunity.owner is None
    assert opportunity.manual_pin is False
    assert opportunity.human_revision == 0


@pytest.mark.django_db
def test_contact_roles_use_stable_lead_ids_not_names():
    opportunity = _opportunity()
    first = Lead.objects.create(
        first_name="Alex",
        last_name="Smith",
        company_name="Ramp",
        linkedin_url="https://linkedin.com/in/alex-one",
    )
    second = Lead.objects.create(
        first_name="Alex",
        last_name="Smith",
        company_name="Ramp",
        linkedin_url="https://linkedin.com/in/alex-two",
    )

    report = apply_opportunity_imports(
        _imports(opportunity, {
            crm_sheets.COL_CHAMPION: str(first.id),
            crm_sheets.COL_DECISION_MAKER: str(second.id),
        }),
        dry_run=False,
    )

    assert report.invalid == []
    assert OpportunityContact.objects.get(
        opportunity=opportunity,
        role=OpportunityContact.Role.CHAMPION,
    ).lead_id == first.id
    assert OpportunityContact.objects.get(
        opportunity=opportunity,
        role=OpportunityContact.Role.DECISION_MAKER,
    ).lead_id == second.id


@pytest.mark.django_db
def test_closed_lost_without_reason_is_skipped():
    opportunity = _opportunity()

    report = apply_opportunity_imports(
        _imports(opportunity, {crm_sheets.COL_STAGE: "Closed Lost"}),
        dry_run=False,
    )

    opportunity.refresh_from_db()
    assert len(report.invalid) == 1
    assert opportunity.stage == Opportunity.Stage.DISCOVERY


@pytest.mark.django_db
def test_dry_run_validates_without_writing():
    opportunity = _opportunity()

    report = apply_opportunity_imports(
        _imports(opportunity, {crm_sheets.COL_OWNER: "Arian"}),
        dry_run=True,
    )

    opportunity.refresh_from_db()
    assert report.opportunities_updated == 1
    assert opportunity.owner is None


@pytest.mark.django_db
def test_baseline_advances_only_when_explicitly_committed():
    opportunity = _opportunity()
    baseline = {
        field: ""
        for field in crm_sheets.OPPORTUNITY_HUMAN_FIELDS
    }
    baseline[crm_sheets.COL_OWNER] = "Arian"

    assert baseline_by_opportunity_id() == {}
    count = commit_sheet_baselines([
        {"stable_id": str(opportunity.id), "values": baseline},
    ])

    assert count == 1
    state = OpportunitySheetState.objects.get(opportunity=opportunity)
    assert state.published_human_snapshot[crm_sheets.COL_OWNER] == "Arian"


@pytest.mark.django_db
def test_followup_draft_and_handled_state_are_durable_by_action_id():
    opportunity = _opportunity()
    action = OpportunityAction.objects.create(
        opportunity=opportunity,
        kind=OpportunityAction.Kind.NEEDS_RESPONSE,
        description="Reply",
    )

    report = apply_followup_imports([
        {"stable_id": str(action.id), "field": crm_sheets.COL_CHANNEL, "value": "Email"},
        {"stable_id": str(action.id), "field": crm_sheets.COL_DRAFT, "value": "Thanks — yes."},
        {"stable_id": str(action.id), "field": crm_sheets.COL_HANDLED, "value": "TRUE"},
        {"stable_id": str(action.id), "field": crm_sheets.COL_DISPOSITION, "value": "Sent"},
    ], dry_run=False)

    action.refresh_from_db()
    assert report.invalid == []
    assert action.channel == "Email"
    assert action.draft == "Thanks — yes."
    assert action.status == OpportunityAction.Status.COMPLETED
    assert action.sent_at is not None


@pytest.mark.django_db
def test_followup_waiting_date_is_durable_by_action_id():
    opportunity = _opportunity()
    action = OpportunityAction.objects.create(
        opportunity=opportunity,
        kind=OpportunityAction.Kind.NEXT_STEP,
        description="Follow up later",
    )
    waiting_until = (timezone.localdate() + timedelta(days=3)).isoformat()

    report = apply_followup_imports([
        {
            "stable_id": str(action.id),
            "field": crm_sheets.COL_WAITING_UNTIL,
            "value": waiting_until,
        },
    ], dry_run=False)

    action.refresh_from_db()
    assert report.invalid == []
    assert action.waiting_until.isoformat() == waiting_until
    assert action.status == OpportunityAction.Status.WAITING


@pytest.mark.django_db
def test_followup_sheet_diff_includes_waiting_until_without_key_error():
    opportunity = _opportunity()
    action = OpportunityAction.objects.create(
        opportunity=opportunity,
        kind=OpportunityAction.Kind.NEXT_STEP,
        description="Follow up later",
    )
    waiting_until = (timezone.localdate() + timedelta(days=4)).isoformat()

    imports = followup_imports_from_sheet_rows([{
        crm_sheets.COL_ACTION_ID: str(action.id),
        crm_sheets.COL_WAITING_UNTIL: waiting_until,
    }])

    assert imports == [{
        "stable_id": str(action.id),
        "field": crm_sheets.COL_WAITING_UNTIL,
        "value": waiting_until,
    }]


@pytest.mark.django_db
def test_followup_baseline_commits_only_after_publish():
    opportunity = _opportunity()
    action = OpportunityAction.objects.create(
        opportunity=opportunity,
        kind=OpportunityAction.Kind.NEXT_STEP,
        description="Follow up",
    )
    baseline = {
        field: ""
        for field in crm_sheets.FOLLOWUP_HUMAN_FIELDS
    }
    baseline[crm_sheets.COL_DRAFT] = "Saved draft"

    commit_followup_baselines([
        {"stable_id": str(action.id), "values": baseline},
    ])

    action.refresh_from_db()
    assert action.sheet_human_snapshot[crm_sheets.COL_DRAFT] == "Saved draft"
    assert action.sheet_published_at is not None


@pytest.mark.django_db
def test_conflicting_manual_pin_edits_across_action_history_fail_closed():
    opportunity = _opportunity()
    current = OpportunityAction.objects.create(
        opportunity=opportunity,
        kind=OpportunityAction.Kind.NEXT_STEP,
        description="Current action",
    )
    history = OpportunityAction.objects.create(
        opportunity=opportunity,
        kind=OpportunityAction.Kind.FOLLOWUP,
        status=OpportunityAction.Status.COMPLETED,
        description="Historical action",
    )

    report = apply_followup_imports([
        {
            "stable_id": str(current.id),
            "field": crm_sheets.COL_MANUAL_PIN,
            "value": "TRUE",
        },
        {
            "stable_id": str(history.id),
            "field": crm_sheets.COL_MANUAL_PIN,
            "value": "FALSE",
        },
    ], dry_run=False)

    opportunity.refresh_from_db()
    assert len(report.invalid) == 2
    assert all("conflicting Manual pin" in item.reason for item in report.invalid)
    assert opportunity.manual_pin is False


@pytest.mark.django_db
def test_people_dont_send_uses_stable_id_then_exact_linkedin_url():
    by_id = Lead.objects.create(
        first_name="Stable",
        last_name="ID",
        linkedin_url="https://linkedin.com/in/stable-id",
    )
    by_url = Lead.objects.create(
        first_name="Stable",
        last_name="URL",
        linkedin_url="https://www.linkedin.com/in/stable-url/",
    )
    headers = ["LinkedIn URL", "Outreach status", "Lead ID"]
    values = [
        headers,
        [by_id.linkedin_url, "Don't send", str(by_id.id)],
        ["https://linkedin.com/in/stable-url?trk=sheet", "DON'T SEND", ""],
        ["https://linkedin.com/in/ignored", "Replied", ""],
    ]

    class Worksheet:
        def get_all_values(self):
            return values

    class Spreadsheet:
        def worksheet(self, _title):
            return Worksheet()

    assert read_people_dont_send_lead_ids(Spreadsheet()) == {by_id.id, by_url.id}


@pytest.mark.django_db
def test_people_dont_send_rejects_conflicting_stable_id_and_url():
    lead = Lead.objects.create(
        first_name="Stable",
        last_name="Conflict",
        linkedin_url="https://linkedin.com/in/right-person",
    )

    class Worksheet:
        def get_all_values(self):
            return [
                ["LinkedIn URL", "Outreach status", "Lead ID"],
                ["https://linkedin.com/in/wrong-person", "Don't send", str(lead.id)],
            ]

    class Spreadsheet:
        def worksheet(self, _title):
            return Worksheet()

    with pytest.raises(SheetsError, match="identity conflict"):
        read_people_dont_send_lead_ids(Spreadsheet())


@pytest.mark.django_db
def test_people_dont_send_rejects_ambiguous_canonical_linkedin_identity():
    for suffix in ("", "?trk=duplicate"):
        Lead.objects.create(
            first_name="Duplicate",
            linkedin_url=f"https://www.linkedin.com/in/duplicate-dnc/{suffix}",
        )

    class Worksheet:
        def get_all_values(self):
            return [
                ["LinkedIn URL", "Outreach status", "Lead ID"],
                ["https://linkedin.com/in/duplicate-dnc", "Don't send", ""],
            ]

    class Spreadsheet:
        def worksheet(self, _title):
            return Worksheet()

    with pytest.raises(SheetsError, match="ambiguous"):
        read_people_dont_send_lead_ids(Spreadsheet())


@pytest.mark.django_db
@pytest.mark.parametrize(
    "row, message",
    [
        (["", "Don't send", ""], "no resolvable stable identity"),
        (["https://linkedin.com/in/missing-dnc", "Don't send", ""], "does not match"),
        (["", "Don't send", "999999999"], "unknown Lead ID"),
    ],
)
def test_people_dont_send_rejects_unresolved_opt_out_rows(row, message):
    class Worksheet:
        def get_all_values(self):
            return [["LinkedIn URL", "Outreach status", "Lead ID"], row]

    class Spreadsheet:
        def worksheet(self, _title):
            return Worksheet()

    with pytest.raises(SheetsError, match=message):
        read_people_dont_send_lead_ids(Spreadsheet())


@pytest.mark.django_db
def test_people_dont_send_rejects_valid_id_with_another_leads_url():
    blank_url = Lead.objects.create(first_name="Blank URL", linkedin_url="")
    other = Lead.objects.create(
        first_name="Other",
        linkedin_url="https://linkedin.com/in/other-dnc-identity",
    )

    class Worksheet:
        def get_all_values(self):
            return [
                ["LinkedIn URL", "Outreach status", "Lead ID"],
                [other.linkedin_url, "Don't send", str(blank_url.id)],
            ]

    class Spreadsheet:
        def worksheet(self, _title):
            return Worksheet()

    with pytest.raises(SheetsError, match="identity conflict"):
        read_people_dont_send_lead_ids(Spreadsheet())


@pytest.mark.django_db
@pytest.mark.parametrize(
    "values, message",
    [
        ([], "People is empty"),
        ([['LinkedIn URL']], "safety-critical"),
        ([['Outreach status']], "safety-critical"),
    ],
)
def test_people_dont_send_fails_closed_on_empty_or_partial_schema(values, message):
    class Worksheet:
        def get_all_values(self):
            return values

    class Spreadsheet:
        def worksheet(self, _title):
            return Worksheet()

    with pytest.raises(SheetsError, match=message):
        read_people_dont_send_lead_ids(Spreadsheet())


@pytest.mark.django_db
def test_sheet_action_edit_rejects_ambiguous_target_contacts():
    opportunity = _opportunity()
    for suffix in ("one", "two"):
        lead = Lead.objects.create(
            first_name=suffix.title(),
            company_name="Ramp",
            linkedin_url=f"https://linkedin.com/in/ambiguous-{suffix}",
        )
        OpportunityContact.objects.create(
            opportunity=opportunity,
            lead=lead,
            role=OpportunityContact.Role.STAKEHOLDER,
        )

    report = apply_opportunity_imports(
        _imports(opportunity, {
            crm_sheets.COL_NEXT_ACTION: "Send the stakeholder follow-up",
        }),
        dry_run=False,
    )

    assert len(report.invalid) == 1
    assert "choose one Champion" in report.invalid[0].reason
    assert not opportunity.actions.exists()


@pytest.mark.django_db
def test_contact_role_move_preserves_non_sheet_metadata():
    opportunity = _opportunity()
    lead = Lead.objects.create(
        first_name="Role",
        last_name="Move",
        company_name="Ramp",
        linkedin_url="https://linkedin.com/in/role-move",
    )
    link = OpportunityContact.objects.create(
        opportunity=opportunity,
        lead=lead,
        role=OpportunityContact.Role.CHAMPION,
        is_primary=True,
        notes="Introduced the security team",
    )
    original_id = link.id
    original_created_at = link.created_at

    report = apply_opportunity_imports(
        _imports(opportunity, {
            crm_sheets.COL_CHAMPION: "",
            crm_sheets.COL_DECISION_MAKER: str(lead.id),
        }),
        dry_run=False,
    )

    link.refresh_from_db()
    assert report.invalid == []
    assert report.contact_roles_updated == 1
    assert opportunity.contacts.count() == 1
    assert link.id == original_id
    assert link.role == OpportunityContact.Role.DECISION_MAKER
    assert link.notes == "Introduced the security team"
    assert link.is_primary is True
    assert link.created_at == original_created_at


@pytest.mark.django_db
def test_contact_role_clear_demotes_in_place_and_preserves_action_target():
    opportunity = _opportunity()
    target = Lead.objects.create(
        first_name="Target",
        company_name="Ramp",
        linkedin_url="https://linkedin.com/in/current-action-target",
    )
    link = OpportunityContact.objects.create(
        opportunity=opportunity,
        lead=target,
        role=OpportunityContact.Role.CHAMPION,
        is_primary=True,
        notes="Current recipient context",
    )
    original_id = link.id
    original_created_at = link.created_at
    action = OpportunityAction.objects.create(
        opportunity=opportunity,
        target_lead=target,
        kind=OpportunityAction.Kind.NEXT_STEP,
        description="Follow up with Target",
    )

    report = apply_opportunity_imports(
        _imports(opportunity, {crm_sheets.COL_CHAMPION: ""}),
        dry_run=False,
    )

    link.refresh_from_db()
    action.refresh_from_db()
    assert report.invalid == []
    assert report.contact_roles_updated == 1
    assert opportunity.contacts.count() == 1
    assert link.id == original_id
    assert link.role == OpportunityContact.Role.OTHER
    assert link.notes == "Current recipient context"
    assert link.is_primary is True
    assert link.created_at == original_created_at
    assert action.target_lead_id == target.id


@pytest.mark.django_db
def test_role_only_edit_resolves_targetless_action_from_explicit_champion():
    opportunity = _opportunity()
    champion = Lead.objects.create(
        first_name="Champion",
        company_name="Ramp",
        linkedin_url="https://linkedin.com/in/resolve-action-target",
    )
    other = Lead.objects.create(
        first_name="Other",
        company_name="Ramp",
        linkedin_url="https://linkedin.com/in/other-action-contact",
    )
    for lead in (champion, other):
        OpportunityContact.objects.create(
            opportunity=opportunity,
            lead=lead,
            role=OpportunityContact.Role.STAKEHOLDER,
        )
    action = OpportunityAction.objects.create(
        opportunity=opportunity,
        kind=OpportunityAction.Kind.NEXT_STEP,
        description="Choose the right recipient",
    )

    report = apply_opportunity_imports(
        _imports(opportunity, {crm_sheets.COL_CHAMPION: str(champion.id)}),
        dry_run=False,
    )

    action.refresh_from_db()
    assert report.invalid == []
    assert report.actions_updated == 1
    assert action.target_lead_id == champion.id
