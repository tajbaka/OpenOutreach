from __future__ import annotations

import json
from datetime import timedelta
from uuid import uuid4

import pytest
from django.utils import timezone
from gspread.utils import ValueRenderOption

from crm.models import (
    Account,
    Lead,
    Message,
    Opportunity,
    OpportunityAction,
    OpportunityContact,
    SalesOwner,
)
from linkedin.legacy_followup_migration import migrate_legacy_followup_tab
from linkedin.notifications import crm_sheets, sheets


class LegacyWorksheet:
    title = "Arian - Followups"

    def __init__(self, rows):
        self.rows = rows
        self.render_options = []

    def get_all_values(self, value_render_option=None):
        self.render_options.append(value_render_option)
        return [list(row) for row in self.rows]


def _email_link(email: str) -> str:
    encoded = email.replace("@", "%40").replace(".", "%2E")
    return f'=HYPERLINK("https://mail.google.com/mail/u/0/#search/{encoded}","{email}")'


def _linkedin_link(url: str) -> str:
    return f'=HYPERLINK("{url}","Open in LinkedIn")'


def _row(**values):
    return [str(values.get(header, "")) for header in sheets.FU_HEADERS]


def _worksheet(*rows):
    return LegacyWorksheet([list(sheets.FU_HEADERS), *rows])


def _canonical_action(
    *,
    owner,
    email="",
    linkedin_url="",
    name="Jane",
    draft="",
):
    lead = Lead.objects.create(
        first_name=name,
        email=email,
        linkedin_url=(
            linkedin_url
            or f"https://www.linkedin.com/in/test-{name.casefold()}-"
            f"{(email.split('@', 1)[0] if email else 'lead')}/"
        ),
        company_name=f"{name} Co",
    )
    opportunity = Opportunity.objects.create(
        account=Account.objects.create(name=f"{name} Account {lead.id}"),
        owner=owner,
        stage=Opportunity.Stage.DISCOVERY,
        sales_motion_step=2,
    )
    OpportunityContact.objects.create(
        opportunity=opportunity,
        lead=lead,
        is_primary=True,
    )
    action = OpportunityAction.objects.create(
        opportunity=opportunity,
        kind=OpportunityAction.Kind.FOLLOWUP,
        status=OpportunityAction.Status.OPEN,
        description="Follow up",
        draft=draft,
    )
    return lead, action


@pytest.mark.django_db
def test_exact_email_formula_imports_one_unsent_email_draft_without_touching_tab():
    owner = SalesOwner.objects.get(handle="Arian")
    lead, action = _canonical_action(owner=owner, email="jane@example.com")
    worksheet = _worksheet(_row(**{
        sheets.FU_COL_NAME: "A completely wrong name",
        sheets.FU_COL_EMAIL_LINK: _email_link("jane@example.com"),
        sheets.FU_COL_DRAFT_EMAIL: "Human email draft",
    }))

    report = migrate_legacy_followup_tab(
        worksheet,
        owner="Arian",
        desired_rows=[{
            crm_sheets.COL_ACTION_ID: str(action.id),
            crm_sheets.COL_LEAD_ID: str(lead.id),
            crm_sheets.COL_OWNER: "Arian",
        }],
        dry_run=False,
    )

    action.refresh_from_db()
    lead.refresh_from_db()
    assert report.rows_changed == 1
    assert report.drafts_imported == 1
    assert action.channel == "email"
    assert action.draft == "Human email draft"
    assert action.target_lead_id == lead.id
    assert lead.first_name == "Jane"
    assert worksheet.render_options == [ValueRenderOption.formula]
    assert worksheet.rows[1][0] == "A completely wrong name"


@pytest.mark.django_db
def test_action_must_match_the_explicit_owner_desired_rows_when_provided():
    owner = SalesOwner.objects.get(handle="Arian")
    _lead, action = _canonical_action(owner=owner, email="desired@example.com")
    worksheet = _worksheet(_row(**{
        sheets.FU_COL_EMAIL_LINK: _email_link("desired@example.com"),
        sheets.FU_COL_DRAFT_EMAIL: "Do not attach to an unlisted action",
    }))

    report = migrate_legacy_followup_tab(
        worksheet,
        owner=owner,
        desired_rows=[{
            crm_sheets.COL_ACTION_ID: str(uuid4()),
            crm_sheets.COL_LEAD_ID: str(_lead.id),
            crm_sheets.COL_OWNER: "Arian",
        }],
        dry_run=False,
    )

    action.refresh_from_db()
    counts = report.counts()
    assert counts["skip_reasons"] == {"action_not_in_desired_rows": 1}
    assert counts["material_rows_skipped"] == 1
    assert counts["material_skip_reasons"] == {
        "action_not_in_desired_rows": 1,
    }
    assert action.draft == ""


@pytest.mark.django_db
def test_exact_linkedin_profile_maps_without_using_name():
    owner = SalesOwner.objects.get(handle="Arian")
    _lead, action = _canonical_action(
        owner=owner,
        linkedin_url="https://www.linkedin.com/in/exact-profile/",
    )
    worksheet = _worksheet(_row(**{
        sheets.FU_COL_NAME: "Someone Else",
        sheets.FU_COL_LINKEDIN_MSG_URL: _linkedin_link(
            "https://www.linkedin.com/in/exact-profile/"
        ),
        sheets.FU_COL_DRAFT_LINKEDIN: "Human LinkedIn draft",
    }))

    report = migrate_legacy_followup_tab(worksheet, owner=owner, dry_run=False)

    action.refresh_from_db()
    assert report.drafts_imported == 1
    assert action.channel == "linkedin"
    assert action.draft == "Human LinkedIn draft"


@pytest.mark.django_db
def test_stored_message_thread_url_is_valid_identity_evidence():
    owner = SalesOwner.objects.get(handle="Arian")
    lead, action = _canonical_action(owner=owner)
    thread_url = "https://www.linkedin.com/messaging/thread/thread-123/"
    Message.objects.create(
        lead=lead,
        operator=owner,
        source=Message.Source.LINKEDIN,
        external_id="legacy-thread-message",
        direction=Message.Direction.INBOUND,
        sent_at=timezone.now() - timedelta(days=1),
        raw={"thread_url": thread_url},
    )
    worksheet = _worksheet(_row(**{
        sheets.FU_COL_LINKEDIN_MSG_URL: _linkedin_link(thread_url),
        sheets.FU_COL_DRAFT_LINKEDIN: "Reply on the exact thread",
    }))

    report = migrate_legacy_followup_tab(worksheet, owner="Arian", dry_run=False)

    action.refresh_from_db()
    assert report.rows_changed == 1
    assert action.draft == "Reply on the exact thread"


@pytest.mark.django_db
def test_conflicting_email_and_profile_evidence_is_rejected_atomically():
    owner = SalesOwner.objects.get(handle="Arian")
    _email_lead, email_action = _canonical_action(
        owner=owner,
        email="one@example.com",
        name="Email",
    )
    _profile_lead, profile_action = _canonical_action(
        owner=owner,
        linkedin_url="https://www.linkedin.com/in/profile-two/",
        name="Profile",
    )
    worksheet = _worksheet(_row(**{
        sheets.FU_COL_EMAIL_LINK: _email_link("one@example.com"),
        sheets.FU_COL_LINKEDIN_MSG_URL: _linkedin_link(
            "https://www.linkedin.com/in/profile-two/"
        ),
        sheets.FU_COL_DRAFT_EMAIL: "Do not import",
    }))

    report = migrate_legacy_followup_tab(worksheet, owner=owner, dry_run=False)

    email_action.refresh_from_db()
    profile_action.refresh_from_db()
    assert report.counts()["skip_reasons"] == {"conflicting_identity": 1}
    assert email_action.draft == ""
    assert profile_action.draft == ""


@pytest.mark.django_db
def test_duplicate_email_without_corroborating_evidence_is_ambiguous():
    owner = SalesOwner.objects.get(handle="Arian")
    _canonical_action(owner=owner, email="shared@example.com", name="One")
    _canonical_action(owner=owner, email="shared@example.com", name="Two")
    worksheet = _worksheet(_row(**{
        sheets.FU_COL_EMAIL_LINK: _email_link("shared@example.com"),
        sheets.FU_COL_DRAFT_EMAIL: "Do not choose by name",
    }))

    report = migrate_legacy_followup_tab(worksheet, owner=owner, dry_run=False)

    assert report.counts()["skip_reasons"] == {"ambiguous_identity": 1}
    assert OpportunityAction.objects.exclude(draft="").count() == 0


@pytest.mark.django_db
def test_name_only_row_is_never_identity_evidence():
    owner = SalesOwner.objects.get(handle="Arian")
    _canonical_action(owner=owner, name="Unique Person")
    worksheet = _worksheet(_row(**{
        sheets.FU_COL_NAME: "Unique Person",
        sheets.FU_COL_DRAFT_EMAIL: "Name matching is forbidden",
    }))

    report = migrate_legacy_followup_tab(worksheet, owner=owner, dry_run=False)

    assert report.counts()["skip_reasons"] == {"no_stable_identity": 1}
    assert OpportunityAction.objects.exclude(draft="").count() == 0


@pytest.mark.django_db
def test_wrong_explicit_owner_has_no_current_action_for_row():
    arian = SalesOwner.objects.get(handle="Arian")
    athena = SalesOwner.objects.get(handle="Athena")
    _canonical_action(owner=arian, email="owner@example.com")
    worksheet = _worksheet(_row(**{
        sheets.FU_COL_EMAIL_LINK: _email_link("owner@example.com"),
        sheets.FU_COL_DRAFT_EMAIL: "Do not reroute",
    }))

    report = migrate_legacy_followup_tab(worksheet, owner=athena, dry_run=False)

    assert report.counts()["skip_reasons"] == {"no_current_action": 1}
    assert OpportunityAction.objects.exclude(draft="").count() == 0


@pytest.mark.django_db
def test_two_current_actions_for_one_lead_and_owner_are_ambiguous():
    owner = SalesOwner.objects.get(handle="Arian")
    lead, first_action = _canonical_action(owner=owner, email="two-actions@example.com")
    second_opportunity = Opportunity.objects.create(
        account=Account.objects.create(name="Second account"),
        owner=owner,
        stage=Opportunity.Stage.DISCOVERY,
        sales_motion_step=2,
    )
    OpportunityContact.objects.create(
        opportunity=second_opportunity,
        lead=lead,
        is_primary=True,
    )
    second_action = OpportunityAction.objects.create(
        opportunity=second_opportunity,
        kind=OpportunityAction.Kind.FOLLOWUP,
        status=OpportunityAction.Status.OPEN,
        description="Second current action",
    )
    worksheet = _worksheet(_row(**{
        sheets.FU_COL_EMAIL_LINK: _email_link("two-actions@example.com"),
        sheets.FU_COL_DRAFT_EMAIL: "Do not guess which action",
    }))

    report = migrate_legacy_followup_tab(worksheet, owner=owner, dry_run=False)

    first_action.refresh_from_db()
    second_action.refresh_from_db()
    assert report.counts()["skip_reasons"] == {"ambiguous_current_action": 1}
    assert first_action.draft == ""
    assert second_action.draft == ""


@pytest.mark.django_db
def test_action_targeted_to_different_contact_is_not_reassigned_by_legacy_row():
    owner = SalesOwner.objects.get(handle="Arian")
    row_lead, action = _canonical_action(
        owner=owner,
        email="row-contact@example.com",
        name="Row",
    )
    other_lead = Lead.objects.create(
        first_name="Other",
        email="other-contact@example.com",
        linkedin_url="https://www.linkedin.com/in/other-contact/",
        company_name="Row Co",
    )
    OpportunityContact.objects.create(
        opportunity=action.opportunity,
        lead=other_lead,
        role=OpportunityContact.Role.DECISION_MAKER,
    )
    action.target_lead = other_lead
    action.save(update_fields={"target_lead", "updated_at"})
    worksheet = _worksheet(_row(**{
        sheets.FU_COL_EMAIL_LINK: _email_link("row-contact@example.com"),
        sheets.FU_COL_DRAFT_EMAIL: "Must not retarget the action",
    }))

    report = migrate_legacy_followup_tab(worksheet, owner=owner, dry_run=False)

    action.refresh_from_db()
    assert report.counts()["skip_reasons"] == {"no_current_action": 1}
    assert action.target_lead_id == other_lead.id
    assert action.target_lead_id != row_lead.id
    assert action.draft == ""


@pytest.mark.django_db
def test_existing_draft_is_preserved_and_reported_as_noop():
    owner = SalesOwner.objects.get(handle="Arian")
    _lead, action = _canonical_action(
        owner=owner,
        email="existing@example.com",
        draft="Canonical draft",
    )
    worksheet = _worksheet(_row(**{
        sheets.FU_COL_EMAIL_LINK: _email_link("existing@example.com"),
        sheets.FU_COL_DRAFT_EMAIL: "Legacy draft must not overwrite",
    }))

    report = migrate_legacy_followup_tab(worksheet, owner=owner, dry_run=False)

    action.refresh_from_db()
    assert report.rows_unchanged == 1
    assert report.drafts_preserved == 1
    assert report.counts()["skip_reasons"] == {"existing_draft": 1}
    assert action.draft == "Canonical draft"


@pytest.mark.django_db
def test_existing_different_channel_is_preserved_instead_of_overwritten():
    owner = SalesOwner.objects.get(handle="Arian")
    _lead, action = _canonical_action(owner=owner, email="channel@example.com")
    action.channel = "linkedin"
    action.save(update_fields={"channel", "updated_at"})
    worksheet = _worksheet(_row(**{
        sheets.FU_COL_EMAIL_LINK: _email_link("channel@example.com"),
        sheets.FU_COL_DRAFT_EMAIL: "Email draft conflicts with canonical channel",
    }))

    report = migrate_legacy_followup_tab(worksheet, owner=owner, dry_run=False)

    action.refresh_from_db()
    assert report.counts()["skip_reasons"] == {"existing_channel": 1}
    assert action.channel == "linkedin"
    assert action.draft == ""


@pytest.mark.django_db
def test_explicit_sent_toggle_completes_action_without_fabricating_sent_at():
    owner = SalesOwner.objects.get(handle="Arian")
    _lead, action = _canonical_action(
        owner=owner,
        linkedin_url="https://www.linkedin.com/in/sent-person/",
    )
    worksheet = _worksheet(_row(**{
        sheets.FU_COL_LINKEDIN_MSG_URL: _linkedin_link(
            "https://www.linkedin.com/in/sent-person/"
        ),
        sheets.FU_COL_SENT_LINKEDIN: "Yes",
    }))

    report = migrate_legacy_followup_tab(worksheet, owner=owner, dry_run=False)

    action.refresh_from_db()
    assert report.actions_marked_sent == 1
    assert action.status == OpportunityAction.Status.COMPLETED
    assert action.disposition == OpportunityAction.Disposition.SENT
    assert action.channel == "linkedin"
    assert action.completed_at is not None
    assert action.sent_at is None


@pytest.mark.django_db
def test_both_explicit_sent_toggles_are_rejected_as_ambiguous():
    owner = SalesOwner.objects.get(handle="Arian")
    _lead, action = _canonical_action(owner=owner, email="both@example.com")
    worksheet = _worksheet(_row(**{
        sheets.FU_COL_EMAIL_LINK: _email_link("both@example.com"),
        sheets.FU_COL_SENT_EMAIL: "TRUE",
        sheets.FU_COL_SENT_LINKEDIN: "✓",
    }))

    report = migrate_legacy_followup_tab(worksheet, owner=owner, dry_run=False)

    action.refresh_from_db()
    assert report.counts()["skip_reasons"] == {"multiple_sent_channels": 1}
    assert action.status == OpportunityAction.Status.OPEN
    assert action.channel == ""
    assert action.sent_at is None


@pytest.mark.django_db
def test_disqualify_dry_run_reports_change_but_persists_nothing():
    owner = SalesOwner.objects.get(handle="Arian")
    lead, _action = _canonical_action(owner=owner, email="dnc@example.com")
    worksheet = _worksheet(_row(**{
        sheets.FU_COL_EMAIL_LINK: _email_link("dnc@example.com"),
        sheets.FU_COL_QUALIFY: "Disqualify",
    }))

    report = migrate_legacy_followup_tab(worksheet, owner=owner, dry_run=True)

    lead.refresh_from_db()
    assert report.rows_changed == 1
    assert report.leads_disqualified == 1
    assert lead.disqualified is False


@pytest.mark.django_db
def test_disqualify_apply_sets_lead_flag_for_recalculation():
    owner = SalesOwner.objects.get(handle="Arian")
    lead, _action = _canonical_action(owner=owner, email="dnc-apply@example.com")
    worksheet = _worksheet(_row(**{
        sheets.FU_COL_EMAIL_LINK: _email_link("dnc-apply@example.com"),
        sheets.FU_COL_QUALIFY: "DISQUALIFIED",
    }))

    report = migrate_legacy_followup_tab(worksheet, owner=owner, dry_run=False)

    lead.refresh_from_db()
    assert report.leads_disqualified == 1
    assert lead.disqualified is True


@pytest.mark.django_db
def test_two_unsent_channel_drafts_are_rejected_without_other_row_mutations():
    owner = SalesOwner.objects.get(handle="Arian")
    lead, action = _canonical_action(owner=owner, email="two-drafts@example.com")
    worksheet = _worksheet(_row(**{
        sheets.FU_COL_EMAIL_LINK: _email_link("two-drafts@example.com"),
        sheets.FU_COL_DRAFT_EMAIL: "Email version",
        sheets.FU_COL_DRAFT_LINKEDIN: "LinkedIn version",
        sheets.FU_COL_QUALIFY: "Disqualify",
    }))

    report = migrate_legacy_followup_tab(worksheet, owner=owner, dry_run=False)

    lead.refresh_from_db()
    action.refresh_from_db()
    assert report.counts()["skip_reasons"] == {"multiple_unsent_drafts": 1}
    assert lead.disqualified is False
    assert action.draft == ""


@pytest.mark.django_db
def test_one_invalid_row_does_not_block_a_different_safe_row():
    owner = SalesOwner.objects.get(handle="Arian")
    invalid_lead, invalid_action = _canonical_action(
        owner=owner,
        email="invalid-row@example.com",
        name="Invalid",
    )
    _valid_lead, valid_action = _canonical_action(
        owner=owner,
        email="valid-row@example.com",
        name="Valid",
    )
    worksheet = _worksheet(
        _row(**{
            sheets.FU_COL_EMAIL_LINK: _email_link("invalid-row@example.com"),
            sheets.FU_COL_DRAFT_EMAIL: "Email",
            sheets.FU_COL_DRAFT_LINKEDIN: "LinkedIn",
            sheets.FU_COL_QUALIFY: "Disqualify",
        }),
        _row(**{
            sheets.FU_COL_EMAIL_LINK: _email_link("valid-row@example.com"),
            sheets.FU_COL_DRAFT_EMAIL: "Safe draft",
        }),
    )

    report = migrate_legacy_followup_tab(worksheet, owner=owner, dry_run=False)

    invalid_lead.refresh_from_db()
    invalid_action.refresh_from_db()
    valid_action.refresh_from_db()
    assert report.rows_changed == 1
    assert report.counts()["skip_reasons"] == {"multiple_unsent_drafts": 1}
    assert invalid_lead.disqualified is False
    assert invalid_action.draft == ""
    assert valid_action.draft == "Safe draft"


@pytest.mark.django_db
def test_duplicate_rows_for_one_current_action_are_all_rejected():
    owner = SalesOwner.objects.get(handle="Arian")
    _lead, action = _canonical_action(owner=owner, email="duplicate-row@example.com")
    identity = _email_link("duplicate-row@example.com")
    worksheet = _worksheet(
        _row(**{
            sheets.FU_COL_EMAIL_LINK: identity,
            sheets.FU_COL_DRAFT_EMAIL: "First",
        }),
        _row(**{
            sheets.FU_COL_EMAIL_LINK: identity,
            sheets.FU_COL_DRAFT_EMAIL: "Second",
        }),
    )

    report = migrate_legacy_followup_tab(worksheet, owner=owner, dry_run=False)

    action.refresh_from_db()
    assert report.counts()["skip_reasons"] == {"duplicate_action_rows": 2}
    assert action.draft == ""


@pytest.mark.django_db
def test_report_never_contains_row_pii_or_draft_text():
    owner = SalesOwner.objects.get(handle="Arian")
    _canonical_action(owner=owner, email="private@example.com")
    worksheet = _worksheet(_row(**{
        sheets.FU_COL_NAME: "Private Person",
        sheets.FU_COL_EMAIL_LINK: _email_link("private@example.com"),
        sheets.FU_COL_DRAFT_EMAIL: "Sensitive draft body",
    }))

    report = migrate_legacy_followup_tab(worksheet, owner=owner, dry_run=True)
    rendered = repr(report.counts())

    assert "Private Person" not in rendered
    assert "private@example.com" not in rendered
    assert "Sensitive draft body" not in rendered


@pytest.mark.django_db
def test_material_skip_signal_ignores_context_rows_and_is_json_safe():
    owner = SalesOwner.objects.get(handle="Arian")
    worksheet = _worksheet(
        _row(**{
            sheets.FU_COL_NAME: "Section heading",
            sheets.FU_COL_STATUS: "Needs Reply",
        }),
        _row(**{
            sheets.FU_COL_NAME: "Name-only evidence is forbidden",
            sheets.FU_COL_DRAFT_EMAIL: "Unresolved human draft",
        }),
    )

    report = migrate_legacy_followup_tab(worksheet, owner=owner, dry_run=True)
    counts = report.counts()

    assert counts["skip_reasons"] == {
        "no_explicit_changes": 1,
        "no_stable_identity": 1,
    }
    assert counts["material_rows_skipped"] == 1
    assert counts["material_skip_reasons"] == {"no_stable_identity": 1}
    assert counts["skip_rows"] == [
        {"row": 2, "reason": "no_explicit_changes", "material": False},
        {"row": 3, "reason": "no_stable_identity", "material": True},
    ]
    json.dumps(counts)


@pytest.mark.django_db
def test_inactive_explicit_owner_fails_closed_before_row_import():
    owner = SalesOwner.objects.get(handle="Arian")
    owner.active = False
    owner.save(update_fields={"active", "updated_at"})
    worksheet = _worksheet(_row(**{
        sheets.FU_COL_EMAIL_LINK: _email_link("nobody@example.com"),
        sheets.FU_COL_DRAFT_EMAIL: "Never import",
    }))

    with pytest.raises(ValueError, match="active SalesOwner"):
        migrate_legacy_followup_tab(worksheet, owner="Arian", dry_run=False)
