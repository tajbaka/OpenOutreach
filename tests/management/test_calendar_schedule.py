import io
from unittest.mock import Mock

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from googleapiclient.errors import HttpError
from httplib2 import Response


def test_retry_after_lost_response_recovers_existing_calendar_event(monkeypatch):
    events = Mock()
    events.insert.return_value.execute.side_effect = [
        TimeoutError(),
        HttpError(Response({"status": "409", "reason": "Conflict"}), b"{}"),
    ]
    events.get.return_value.execute.return_value = {
        "status": "confirmed",
        "htmlLink": "https://calendar.google.com/calendar/event?eid=example",
    }
    calendar_events = Mock(return_value=events)
    monkeypatch.setattr("linkedin.management.commands.calendar_schedule.calendar_events", calendar_events)
    options = {
        "account": "arian_boundera",
        "title": "Project check-in",
        "start": "2026-11-12T14:00:00-05:00",
        "end": "2026-11-12T14:30:00-05:00",
        "attendee": ["guest@example.com"],
    }

    call_command("calendar_schedule", **options, stdout=io.StringIO())
    calendar_events.assert_not_called()
    with pytest.raises(CommandError, match="Calendar request interrupted"):
        call_command("calendar_schedule", **options, apply=True, stdout=io.StringIO())
    output = io.StringIO()
    call_command("calendar_schedule", **options, apply=True, stdout=output)

    first_insert, second_insert = events.insert.call_args_list
    assert first_insert == second_insert
    assert first_insert.kwargs["sendUpdates"] == "all"
    assert first_insert.kwargs["body"]["attendees"] == [{"email": "guest@example.com"}]
    events.get.assert_called_once_with(calendarId="primary", eventId=first_insert.kwargs["body"]["id"])
    assert "already scheduled" in output.getvalue()
    assert "https://calendar.google.com/calendar/event?eid=example" in output.getvalue()
