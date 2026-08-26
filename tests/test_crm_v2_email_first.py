from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from crm.models import Lead
from linkedin.crm_v2_email_first import (
    apply_email_first_leads,
    dry_run_email_first_leads,
)


NOW = datetime(2026, 8, 26, 16, 0, tzinfo=timezone.utc)


def _candidate(
    *,
    email: str = "zelia@ramp.example",
    display_name: str = "Zelia Pantani",
    domain: str | None = None,
    last_inbound_at: datetime | str | None = None,
    account_key: str = "arian_boundera",
    latest_thread_id: str | None = None,
    thread_count: int = 1,
):
    resolved_domain = domain if domain is not None else email.rsplit("@", 1)[-1]
    return {
        "account_key": account_key,
        "email": email,
        "display_name": display_name,
        "domain": resolved_domain,
        "last_inbound_at": (
            last_inbound_at
            if last_inbound_at is not None
            else (NOW - timedelta(days=7)).isoformat()
        ),
        "latest_thread_id": (
            latest_thread_id
            if latest_thread_id is not None
            else f"{account_key}:thread-ramp"
        ),
        "thread_count": thread_count,
    }


def test_ramp_like_corporate_contact_creates_email_first_lead(db):
    report = apply_email_first_leads([_candidate()], evaluated_at=NOW)

    assert report.counts() == {
        "created": 1,
        "existing": 0,
        "review_only": 0,
        "rejected": 0,
    }
    assert len(report.created_lead_ids) == 1
    lead = Lead.objects.get()
    assert (lead.first_name, lead.last_name) == ("Zelia", "Pantani")
    assert lead.company_name == "Ramp"
    assert lead.email == "zelia@ramp.example"
    assert lead.linkedin_url == ""
    assert report.outcomes[0].derived_account_key == "domain:ramp.example"


@pytest.mark.parametrize("domain", ["gmail.com", "outlook.com", "proton.me"])
def test_public_mailbox_domains_remain_review_only(db, domain):
    report = apply_email_first_leads(
        [_candidate(email=f"zelia@{domain}", domain=domain)],
        evaluated_at=NOW,
    )

    assert report.counts()["review_only"] == 1
    assert report.outcomes[0].issue_code == "public_mailbox_domain"
    assert Lead.objects.count() == 0


@pytest.mark.parametrize(
    ("email", "issue_code"),
    [
        ("newsletter@ramp.example", "automated_local_part"),
        ("no-reply@ramp.example", "automated_local_part"),
        ("support@ramp.example", "role_mailbox_local_part"),
        ("arian@getboundera.com", "internal_boundera_domain"),
    ],
)
def test_automated_role_and_internal_addresses_are_rejected(db, email, issue_code):
    report = apply_email_first_leads(
        [_candidate(email=email)],
        evaluated_at=NOW,
    )

    assert report.counts()["rejected"] == 1
    assert report.outcomes[0].issue_code == issue_code
    assert Lead.objects.count() == 0


def test_stale_candidate_is_review_only(db):
    report = apply_email_first_leads(
        [_candidate(last_inbound_at=NOW - timedelta(days=121))],
        evaluated_at=NOW,
    )

    assert report.counts()["review_only"] == 1
    assert report.outcomes[0].issue_code == "stale_candidate"
    assert Lead.objects.count() == 0


def test_exact_120_day_boundary_is_recent(db):
    report = apply_email_first_leads(
        [_candidate(last_inbound_at=NOW - timedelta(days=120))],
        evaluated_at=NOW,
    )

    assert report.counts()["created"] == 1


@pytest.mark.parametrize(
    ("overrides", "issue_code"),
    [
        ({"latest_thread_id": "thread-ramp"}, "invalid_thread_identity"),
        ({"latest_thread_id": "arian_boundera:"}, "invalid_thread_identity"),
        ({"thread_count": 0}, "invalid_thread_count"),
        ({"last_inbound_at": "2026-08-26 12:00:00"}, "invalid_last_inbound_at"),
        ({"last_inbound_at": NOW + timedelta(seconds=1)}, "future_last_inbound_at"),
        ({"domain": "other.example"}, "email_domain_mismatch"),
    ],
)
def test_malformed_or_unproven_candidates_fail_closed(db, overrides, issue_code):
    report = apply_email_first_leads(
        [_candidate(**overrides)],
        evaluated_at=NOW,
    )

    assert report.counts()["rejected"] == 1
    assert report.outcomes[0].issue_code == issue_code
    assert Lead.objects.count() == 0


def test_ambiguous_case_insensitive_existing_identity_is_rejected(db):
    Lead.objects.create(email="Zelia@Ramp.example", linkedin_url="")
    Lead.objects.create(email="zelia@ramp.example", linkedin_url="")

    report = apply_email_first_leads([_candidate()], evaluated_at=NOW)

    assert report.counts()["rejected"] == 1
    assert report.outcomes[0].issue_code == "ambiguous_existing_email"
    assert Lead.objects.count() == 2


def test_conflicting_duplicate_candidates_are_rejected_as_one_identity(db):
    first = _candidate()
    second = _candidate(latest_thread_id="arian_boundera:other-thread")

    report = apply_email_first_leads([first, second], evaluated_at=NOW)

    assert report.input_candidates == 2
    assert report.counts()["rejected"] == 1
    assert report.outcomes[0].input_indexes == (0, 1)
    assert report.outcomes[0].issue_code == "conflicting_duplicate_candidate"
    assert Lead.objects.count() == 0


def test_two_people_at_same_domain_create_separate_leads_in_one_account_group(db):
    first = _candidate(
        email="maddie@steel-patriot.example",
        display_name="Maddie Advisor",
        latest_thread_id="arian_boundera:thread-maddie",
    )
    second = _candidate(
        email="casey@steel-patriot.example",
        display_name="Casey Assessor",
        latest_thread_id="arian_boundera:thread-casey",
    )

    report = apply_email_first_leads([first, second], evaluated_at=NOW)

    assert report.counts()["created"] == 2
    assert Lead.objects.count() == 2
    assert set(Lead.objects.values_list("company_name", flat=True)) == {
        "Steel Patriot"
    }
    assert {outcome.derived_account_key for outcome in report.outcomes} == {
        "domain:steel-patriot.example"
    }


def test_dry_run_apply_and_repeat_are_exact_atomic_and_idempotent(db):
    candidate = _candidate()

    preview = dry_run_email_first_leads([candidate], evaluated_at=NOW)
    assert preview.applied is False
    assert preview.counts()["created"] == 1
    assert preview.created_lead_ids == ()
    assert preview.outcomes[0].lead_id is None
    assert Lead.objects.count() == 0

    applied = apply_email_first_leads([candidate], evaluated_at=NOW)
    assert applied.applied is True
    assert applied.counts()["created"] == 1
    assert len(applied.created_lead_ids) == 1
    assert Lead.objects.count() == 1

    repeated = apply_email_first_leads([candidate], evaluated_at=NOW)
    assert repeated.counts() == {
        "created": 0,
        "existing": 1,
        "review_only": 0,
        "rejected": 0,
    }
    assert repeated.outcomes[0].lead_id == applied.created_lead_ids[0]
    assert Lead.objects.count() == 1


def test_existing_lead_is_never_overwritten(db):
    existing = Lead.objects.create(
        first_name="Human",
        last_name="Edited",
        company_name="Chosen Company",
        linkedin_url="https://www.linkedin.com/in/human-edited/",
        email="ZELIA@RAMP.EXAMPLE",
        disqualified=True,
    )

    report = apply_email_first_leads([_candidate()], evaluated_at=NOW)

    assert report.counts()["existing"] == 1
    existing.refresh_from_db()
    assert existing.first_name == "Human"
    assert existing.last_name == "Edited"
    assert existing.company_name == "Chosen Company"
    assert existing.linkedin_url == "https://www.linkedin.com/in/human-edited/"
    assert existing.email == "ZELIA@RAMP.EXAMPLE"
    assert existing.disqualified is True


def test_counts_and_issue_counts_do_not_expose_candidate_pii(db):
    candidate = _candidate(email="private.person@ramp.example")

    report = apply_email_first_leads([candidate], evaluated_at=NOW)
    rendered = repr((report.counts(), report.issue_counts()))

    assert "private.person" not in rendered
    assert "ramp.example" not in rendered
    assert report.issue_counts() == {"email_first_lead_created": 1}
