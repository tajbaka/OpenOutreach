from __future__ import annotations

import json
from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from crm.models import Account, Lead, MeetingNote, Message, Opportunity, OpportunityContact
from linkedin.crm_lock import crm_refresh_lock
from linkedin.notifications import active_account_people as people
from linkedin.notifications import manual_active_accounts as active


def _active_values(*rows):
    return [list(active.HEADERS), *[list(row) for row in rows]]


def _active_row(
    *, company="", stage="", main_contact="", opportunity_id="", account_id=""
):
    values = {header: "" for header in active.HEADERS}
    values.update({
        active.COL_COMPANY: company,
        active.COL_STAGE: stage,
        active.COL_MAIN_POINT_OF_CONTACT: main_contact,
        active.COL_OPPORTUNITY_ID: opportunity_id,
        active.COL_ACCOUNT_ID: account_id,
    })
    return [values[header] for header in active.HEADERS]


@pytest.mark.django_db
def test_people_scope_is_exact_company_match_and_keeps_manual_notes():
    scope = people.read_account_scope(_active_values(
        _active_row(company="Acme, Inc.", stage="Discovery"),
    ))
    included = Lead.objects.create(
        first_name="Ada",
        last_name="Lovelace",
        company_name="Acme Inc",
        linkedin_url="https://linkedin.com/in/ada/?trk=old",
        email="ada@example.com",
        role_tag="Technology/Engineering Executive",
        description=json.dumps({
            "positions": [{"title": "CTO"}],
            "location_name": "Toronto",
        }),
    )
    Lead.objects.create(
        first_name="Grace",
        last_name="Hopper",
        company_name="Acme Cloud",
        linkedin_url="https://linkedin.com/in/grace/",
    )
    cold_match = Lead.objects.create(
        first_name="Cold",
        last_name="Prospect",
        company_name="Acme Inc",
        linkedin_url="https://linkedin.com/in/cold-prospect/",
    )
    Message.objects.create(
        lead=included,
        source=Message.Source.GMAIL,
        external_id="gmail-inbound-ada",
        direction=Message.Direction.INBOUND,
        sent_at=timezone.now(),
    )
    headers = [*people.HEADERS, "Apollo Email"]
    key = people._identity(included.linkedin_url, included.email)
    rows, counts = people.build_people_rows(
        scope,
        preserved={key: {
            people.COL_NOTES: "Call Tuesday",
            people.COL_PRIORITY: "High",
            "Apollo Email": "operator@example.com",
        }},
        output_headers=headers,
    )

    assert counts["selected_leads"] == 1
    assert counts["published_rows"] == 1
    assert counts["excluded_without_relationship_evidence"] == 1
    assert counts["gmail_contacts"] == 1
    row = dict(zip(headers, rows[0], strict=True))
    assert row[people.COL_NAME] == "Ada Lovelace"
    assert row[people.COL_TITLE] == "CTO"
    assert row[people.COL_PRIMARY_LOCATION] == "Toronto"
    assert row[people.COL_NOTES] == "Call Tuesday"
    assert row[people.COL_PRIORITY] == "High"
    assert row["Apollo Email"] == "operator@example.com"
    assert "Lead ID" not in row
    assert "AI Notes" not in row
    assert "Outreach status" not in row
    assert "Stage" not in row


@pytest.mark.django_db
def test_stable_account_contact_is_in_scope_without_company_name_match():
    account = Account.objects.create(name="Canonical Co")
    opportunity = Opportunity.objects.create(account=account)
    lead = Lead.objects.create(
        first_name="Email",
        last_name="Contact",
        company_name="Division Brand",
        email="contact@example.com",
    )
    OpportunityContact.objects.create(
        opportunity=opportunity,
        lead=lead,
        is_primary=True,
    )
    scope = people.read_account_scope(_active_values(
        _active_row(
            company=account.name,
            stage="Evaluation",
            opportunity_id=str(opportunity.id),
            account_id=str(account.id),
        ),
    ))

    rows, counts = people.build_people_rows(
        scope,
        preserved={},
        output_headers=list(people.HEADERS),
    )

    assert counts["published_rows"] == 1
    assert counts["curated_opportunity_contacts"] == 1
    row = dict(zip(people.HEADERS, rows[0], strict=True))
    assert row[people.COL_COMPANY] == "Canonical Co"
    assert row[people.COL_EMAILS] == "contact@example.com"


@pytest.mark.django_db
def test_exact_same_name_account_rows_merge_nonconflicting_linkedin_and_email():
    account = Account.objects.create(name="stackArmor")
    opportunity = Opportunity.objects.create(account=account)
    linkedin_lead = Lead.objects.create(
        first_name="Tony",
        last_name="Steiner",
        company_name="stackArmor",
        linkedin_url="https://www.linkedin.com/in/steinmine/",
    )
    email_lead = Lead.objects.create(
        first_name="Tony",
        last_name="Steiner",
        company_name="Stackarmor",
        email="asteiner@stackarmor.com",
    )
    OpportunityContact.objects.create(
        opportunity=opportunity,
        lead=linkedin_lead,
        is_primary=True,
    )
    OpportunityContact.objects.create(
        opportunity=opportunity,
        lead=email_lead,
        notes="Meeting attendee",
    )
    scope = people.read_account_scope(_active_values(
        _active_row(
            company="stackArmor",
            account_id=str(account.id),
            opportunity_id=str(opportunity.id),
        ),
    ))

    rows, counts = people.build_people_rows(
        scope,
        preserved={},
        output_headers=list(people.HEADERS),
    )

    assert counts["selected_leads"] == 2
    assert counts["published_rows"] == 1
    assert counts["deduplicated_rows"] == 1
    row = dict(zip(people.HEADERS, rows[0], strict=True))
    assert row[people.COL_COMPANY] == "stackArmor"
    assert row[people.COL_LINKEDIN_URL] == "https://www.linkedin.com/in/steinmine/"
    assert row[people.COL_EMAILS] == "asteiner@stackarmor.com"


@pytest.mark.django_db
def test_matched_granola_attendee_is_in_scope_and_cold_company_lead_is_not():
    account = Account.objects.create(name="Ramp")
    opportunity = Opportunity.objects.create(account=account)
    attendee = Lead.objects.create(
        first_name="Meeting",
        last_name="Person",
        company_name="Ramp",
        email="meeting@ramp.com",
    )
    Lead.objects.create(
        first_name="Cold",
        last_name="Person",
        company_name="Ramp",
        email="cold@ramp.com",
    )
    MeetingNote.objects.create(
        source=MeetingNote.Source.GRANOLA,
        external_id="granola-ramp",
        opportunity=opportunity,
        attendees=[{"name": "Meeting Person", "email": "meeting@ramp.com"}],
        match_status=MeetingNote.MatchStatus.MATCHED,
    )
    scope = people.read_account_scope(_active_values(
        _active_row(
            company="Ramp",
            account_id=str(account.id),
            opportunity_id=str(opportunity.id),
        ),
    ))

    rows, counts = people.build_people_rows(
        scope,
        preserved={},
        output_headers=list(people.HEADERS),
    )

    assert counts["published_rows"] == 1
    assert counts["meeting_contacts"] == 1
    assert counts["excluded_without_relationship_evidence"] == 1
    row = dict(zip(people.HEADERS, rows[0], strict=True))
    assert row[people.COL_NAME] == "Meeting Person"


@pytest.mark.django_db
def test_people_groups_companies_by_latest_contact_and_people_by_close_relevance():
    now = timezone.now()
    fresh_account = Account.objects.create(name="Fresh Co")
    fresh_opportunity = Opportunity.objects.create(account=fresh_account)
    old_account = Account.objects.create(name="Aardvark Co")
    old_opportunity = Opportunity.objects.create(account=old_account)

    decision_maker = Lead.objects.create(
        first_name="Decision",
        last_name="Maker",
        company_name=fresh_account.name,
        email="decision@fresh.example",
        role_tag="Founder/CEO",
    )
    recent_practitioner = Lead.objects.create(
        first_name="Recent",
        last_name="Practitioner",
        company_name=fresh_account.name,
        email="recent@fresh.example",
        role_tag="GRC Analyst/Practitioner",
    )
    old_contact = Lead.objects.create(
        first_name="Old",
        last_name="Contact",
        company_name=old_account.name,
        email="old@aardvark.example",
    )
    OpportunityContact.objects.create(
        opportunity=fresh_opportunity,
        lead=decision_maker,
        role=OpportunityContact.Role.DECISION_MAKER,
    )
    OpportunityContact.objects.create(
        opportunity=fresh_opportunity,
        lead=recent_practitioner,
        notes="Active evaluator",
    )
    OpportunityContact.objects.create(
        opportunity=old_opportunity,
        lead=old_contact,
        is_primary=True,
    )
    for lead, sent_at in (
        (decision_maker, now - timedelta(days=3)),
        (recent_practitioner, now - timedelta(hours=1)),
        (old_contact, now - timedelta(days=10)),
    ):
        Message.objects.create(
            lead=lead,
            source=Message.Source.GMAIL,
            external_id=f"gmail-{lead.pk}",
            direction=Message.Direction.INBOUND,
            sent_at=sent_at,
        )

    scope = people.read_account_scope(_active_values(
        _active_row(
            company=fresh_account.name,
            opportunity_id=str(fresh_opportunity.id),
            account_id=str(fresh_account.id),
        ),
        _active_row(
            company=old_account.name,
            opportunity_id=str(old_opportunity.id),
            account_id=str(old_account.id),
        ),
    ))
    rows, _ = people.build_people_rows(
        scope,
        preserved={},
        output_headers=list(people.HEADERS),
    )
    published = [dict(zip(people.HEADERS, row, strict=True)) for row in rows]

    assert [row[people.COL_COMPANY] for row in published] == [
        "Fresh Co",
        "Fresh Co",
        "Aardvark Co",
    ]
    assert [row[people.COL_NAME] for row in published[:2]] == [
        "Decision Maker",
        "Recent Practitioner",
    ]


def test_company_cohort_formatting_bands_contiguous_groups():
    headers = [people.COL_NAME, people.COL_COMPANY, people.COL_EMAILS]
    rows = [
        ["A One", "Alpha", "a1@example.com"],
        ["A Two", "Alpha", "a2@example.com"],
        ["B One", "Beta", "b1@example.com"],
        ["C One", "Charlie", "c1@example.com"],
        ["C Two", "Charlie", "c2@example.com"],
    ]

    requests, groups = people._company_cohort_format_requests(
        sheet_id=123,
        headers=headers,
        rows=rows,
    )

    assert groups == 3
    assert len(requests) == 6
    fills = [requests[index]["repeatCell"] for index in (0, 2, 4)]
    assert [(item["range"]["startRowIndex"], item["range"]["endRowIndex"]) for item in fills] == [
        (1, 3),
        (3, 4),
        (4, 6),
    ]
    assert fills[0]["cell"]["userEnteredFormat"]["backgroundColor"] == people._COHORT_FILLS[0]
    assert fills[1]["cell"]["userEnteredFormat"]["backgroundColor"] == people._COHORT_FILLS[1]
    borders = [requests[index]["repeatCell"] for index in (1, 3, 5)]
    assert [item["range"]["startRowIndex"] for item in borders] == [1, 3, 4]
    assert all(
        item["cell"]["userEnteredFormat"]["borders"]["top"]["style"] == "SOLID_MEDIUM"
        for item in borders
    )


@pytest.mark.django_db
def test_reset_discards_rows_and_removes_retired_columns():
    old_headers = [
        *people.HEADERS[:7],
        "Outreach status",
        "Stage",
        *people.HEADERS[7:9],
        "Apollo Email",
        "AI Notes",
        people.COL_NOTES,
        people.COL_CREATED_AT,
        people.COL_LAST_SYNCED,
        "Lead ID",
        people.COL_ROLE_TAG,
    ]
    old_row = ["old"] * len(old_headers)

    headers, preserved = people._existing_manual_values(
        [old_headers, old_row],
        reset=True,
    )

    assert "AI Notes" not in headers
    assert "Lead ID" not in headers
    assert "Outreach status" not in headers
    assert "Stage" not in headers
    assert "Apollo Email" in headers
    assert people.COL_NOTES in headers
    assert preserved == {}


def test_command_shares_crm_refresh_lock():
    with crm_refresh_lock():
        with pytest.raises(CommandError, match="already running"):
            call_command("sync_active_account_people", stdout=StringIO())


def test_command_defaults_to_preview(monkeypatch):
    captured = {}

    def publish(**kwargs):
        captured.update(kwargs)
        return {"status": "planned"}

    monkeypatch.setattr(
        people,
        "sync_people_from_active_accounts",
        publish,
    )
    output = StringIO()
    call_command("sync_active_account_people", stdout=output)

    assert captured["dry_run"] is True
    assert captured["reset"] is False
    assert '"status": "planned"' in output.getvalue()
