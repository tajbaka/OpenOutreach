from __future__ import annotations

import base64
from datetime import datetime, timezone

import pytest

from crm.models import Lead, Meeting
from gmail import data_sync


class _Request:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return self.payload


class _Messages:
    def __init__(self, messages):
        self.messages = messages

    def list(self, **kwargs):
        return _Request({
            "messages": [{"id": msg["id"], "threadId": msg.get("threadId", "")}
                         for msg in self.messages],
        })

    def get(self, **kwargs):
        message_id = kwargs["id"]
        return _Request(next(msg for msg in self.messages if msg["id"] == message_id))


class _Users:
    def __init__(self, messages):
        self._messages = _Messages(messages)

    def messages(self):
        return self._messages


class _Service:
    def __init__(self, messages):
        self._users = _Users(messages)

    def users(self):
        return self._users


class _Client:
    account_key = "eddy_boundera"

    def __init__(self, messages):
        self._service = _Service(messages)


def _gmail_note_message(*, id="m1", subject, body, when):
    encoded = base64.urlsafe_b64encode(body.encode("utf-8")).decode("ascii")
    return {
        "id": id,
        "threadId": f"thread-{id}",
        "internalDate": str(int(when.timestamp() * 1000)),
        "payload": {
            "headers": [
                {"name": "Subject", "value": subject},
                {"name": "From", "value": "Gemini <gemini-notes@google.com>"},
                {"name": "Date", "value": when.strftime("%a, %d %b %Y %H:%M:%S +0000")},
            ],
            "mimeType": "text/plain",
            "body": {"data": encoded},
        },
    }


@pytest.fixture
def lead(db):
    return Lead.objects.create(
        first_name="Rene",
        last_name="Jones",
        company_name="Boundera",
        linkedin_url="https://www.linkedin.com/in/rene-jones/",
        email="rene@example.com",
    )


def test_sync_note_email_attaches_to_existing_meeting(lead):
    meeting = Meeting.objects.create(
        source=Meeting.Source.GOOGLE_CALENDAR,
        external_id="cal-1",
        lead=lead,
        start_at=datetime(2026, 6, 29, 16, 0, tzinfo=timezone.utc),
        title="Rene Boundera Intro",
    )
    msg = _gmail_note_message(
        subject="Notes: “Rene Boundera Intro” Jun 29, 2026",
        body="Notes from Rene Boundera Intro Summary discussed FedRAMP next steps.",
        when=datetime(2026, 6, 29, 17, 0, tzinfo=timezone.utc),
    )

    result = data_sync.sync_gmail_note_emails(
        client=_Client([msg]),
        leads=[lead],
        since_days=365,
        dry_run=False,
    )

    meeting.refresh_from_db()
    assert result.note_emails_seen == 1
    assert result.note_emails_matched == 1
    assert result.note_emails_updated_meetings == 1
    assert meeting.gemini_doc_id == "gmail:eddy_boundera:m1"
    assert "FedRAMP next steps" in meeting.gemini_notes_raw


def test_sync_note_email_creates_meeting_for_unique_named_lead(db):
    lead = Lead.objects.create(
        first_name="Michael",
        last_name="Schroeder",
        company_name="Excentium",
        linkedin_url="https://www.linkedin.com/in/michael-schroeder/",
        email="michael@example.com",
    )
    msg = _gmail_note_message(
        subject="Notes: “Michael Schroeder Boundera Catchup” Jun 25, 2026",
        body="Notes from Michael Schroeder Boundera Catchup Summary asked for a Loom.",
        when=datetime(2026, 6, 25, 19, 0, tzinfo=timezone.utc),
    )

    result = data_sync.sync_gmail_note_emails(
        client=_Client([msg]),
        leads=[lead],
        since_days=365,
        dry_run=False,
    )

    meeting = Meeting.objects.get()
    assert result.note_emails_created_meetings == 1
    assert meeting.lead == lead
    assert meeting.external_id == "gmail-note:m1"
    assert meeting.title == "Michael Schroeder Boundera Catchup"
    assert "asked for a Loom" in meeting.gemini_notes_raw


def test_sync_note_email_leaves_generic_title_unmatched(lead):
    msg = _gmail_note_message(
        subject="Notes: Meeting Jul 6, 2026 at 12:39 PM EDT",
        body="Notes from generic meeting Summary no attendee names here.",
        when=datetime(2026, 7, 6, 17, 0, tzinfo=timezone.utc),
    )

    result = data_sync.sync_gmail_note_emails(
        client=_Client([msg]),
        leads=[lead],
        since_days=365,
        dry_run=False,
    )

    assert result.note_emails_unmatched == 1
    assert Meeting.objects.count() == 0
