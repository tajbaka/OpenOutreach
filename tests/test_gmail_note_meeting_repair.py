from datetime import UTC, datetime

import pytest

from crm.models import (
    Account,
    Lead,
    Meeting,
    MeetingParticipant,
    Opportunity,
)
from gmail.meeting_identity_repair import repair_gmail_note_meeting_identities


pytestmark = pytest.mark.django_db
NOW = datetime(2026, 7, 20, 15, tzinfo=UTC)


def _bad_meeting(*, opportunity=None):
    wrong = Lead.objects.create(
        first_name="John",
        last_name="S.",
        company_name="Cloudflare",
        email="john.s@cloudflare.example",
    )
    correct = Lead.objects.create(
        first_name="John",
        last_name="Allison",
        company_name="Mind Anvil",
        email="john@mindanvil.example",
    )
    meeting = Meeting.objects.create(
        source=Meeting.Source.GOOGLE_CALENDAR,
        external_id="gmail-note:john-allison",
        lead=wrong,
        opportunity=opportunity,
        start_at=NOW,
        title="John Allison Catchup",
        raw={
            "source": "gmail_note_email",
            "subject": "Notes: John Allison Catchup Jul 20, 2026",
        },
    )
    MeetingParticipant.objects.create(
        meeting=meeting,
        lead=wrong,
        match_method=MeetingParticipant.MatchMethod.LEGACY_PRIMARY,
        is_primary=True,
    )
    return meeting, wrong, correct


def test_repair_dry_run_reports_but_rolls_back():
    meeting, wrong, correct = _bad_meeting()

    report = repair_gmail_note_meeting_identities(apply=False)

    meeting.refresh_from_db()
    assert report.repaired == 1
    assert meeting.lead_id == wrong.id
    assert meeting.lead_id != correct.id


def test_repair_apply_reassigns_exact_identity_without_deleting_meeting():
    meeting, wrong, correct = _bad_meeting()

    report = repair_gmail_note_meeting_identities(apply=True)

    meeting.refresh_from_db()
    assert report.repaired == 1
    assert meeting.lead_id == correct.id
    assert meeting.opportunity_id is None
    assert Meeting.objects.filter(pk=meeting.id).exists()
    assert list(meeting.participants.values_list("id", flat=True)) == [correct.id]


def test_repair_blocks_human_managed_opportunity():
    account = Account.objects.create(name="Cloudflare")
    opportunity = Opportunity.objects.create(
        account=account,
        name="Cloudflare",
        source=Opportunity.Source.MANUAL,
    )
    meeting, wrong, _correct = _bad_meeting(opportunity=opportunity)

    report = repair_gmail_note_meeting_identities(apply=True)

    meeting.refresh_from_db()
    assert report.blocked == 1
    assert report.repaired == 0
    assert meeting.lead_id == wrong.id
