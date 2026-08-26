from __future__ import annotations

import io
import json
from unittest.mock import patch

import pytest
from django.core.management import CommandError, call_command
from django.utils import timezone

from crm.models import Lead, Meeting


@patch("linkedin.management.commands.granola_notes.GRANOLA_API_KEY", "grn_test")
@patch("linkedin.management.commands.granola_notes.GranolaClient")
def test_search_fetches_matching_note_details(client_class):
    client = client_class.return_value
    client.iter_notes.return_value = iter(
        [
            {"id": "not_cccccccccccccc", "title": "FedRAMP market briefing"},
            {"id": "not_aaaaaaaaaaaaaa", "title": "Ramp sandbox working session"},
            {"id": "not_bbbbbbbbbbbbbb", "title": "Unrelated account"},
        ]
    )
    client.get_note.return_value = {
        "id": "not_aaaaaaaaaaaaaa",
        "object": "note",
        "title": "Ramp sandbox working session",
        "summary_text": "Ramp wants to validate the evidence workflow.",
    }
    stdout = io.StringIO()

    call_command("granola_notes", search="Ramp", stdout=stdout)

    payload = json.loads(stdout.getvalue())
    assert payload["mode"] == "search"
    assert payload["count"] == 1
    assert payload["scanned"] == 3
    assert payload["notes"][0]["summary_text"].startswith("Ramp wants")
    client.get_note.assert_called_once_with("not_aaaaaaaaaaaaaa")


@patch("linkedin.management.commands.granola_notes.GRANOLA_API_KEY", "")
def test_missing_api_key_is_a_command_error():
    with pytest.raises(CommandError, match="GRANOLA_API_KEY"):
        call_command("granola_notes")


@patch("linkedin.management.commands.granola_notes.GRANOLA_API_KEY", "grn_test")
@patch("linkedin.management.commands.granola_notes.GranolaClient")
def test_exact_note_can_include_paginated_transcript(client_class):
    client = client_class.return_value
    client.get_note.return_value = {
        "id": "not_aaaaaaaaaaaaaa",
        "object": "note",
        "title": "Ramp",
    }
    client.get_transcript.return_value = [{"text": "hello"}]
    stdout = io.StringIO()

    call_command(
        "granola_notes",
        note_id="not_aaaaaaaaaaaaaa",
        include_transcript=True,
        stdout=stdout,
    )

    payload = json.loads(stdout.getvalue())
    assert payload["notes"][0]["transcript"] == [{"text": "hello"}]


@pytest.mark.django_db
@patch("linkedin.management.commands.granola_notes.GRANOLA_API_KEY", "grn_test")
@patch("linkedin.management.commands.granola_notes.GranolaClient")
def test_search_uses_linked_gemini_note_when_granola_has_no_match(client_class):
    client_class.return_value.iter_notes.return_value = iter([])
    lead = Lead.objects.create(
        first_name="Zelia",
        last_name="Pantani",
        company_name="Ramp",
        email="zelia.pantani@ramp.com",
        linkedin_url="https://www.linkedin.com/in/zeliapantani/",
    )
    Meeting.objects.create(
        source=Meeting.Source.GOOGLE_CALENDAR,
        external_id="ramp-gemini-note",
        lead=lead,
        start_at=timezone.now(),
        title="Boundera working session",
        gemini_doc_title="Boundera working session",
        gemini_notes_raw="Zelia described the sandbox evidence workflow.",
    )
    stdout = io.StringIO()

    call_command("granola_notes", search="Ramp", stdout=stdout)

    payload = json.loads(stdout.getvalue())
    assert payload["source"] == "gemini"
    assert payload["sources_checked"] == ["granola", "gemini"]
    assert payload["fallback_used"] is True
    assert payload["count"] == 1
    assert payload["notes"][0]["lead"]["name"] == "Zelia Pantani"
    assert payload["notes"][0]["notes"].startswith("Zelia described")


@pytest.mark.django_db
@patch("linkedin.management.commands.granola_notes.GRANOLA_API_KEY", "grn_test")
@patch("linkedin.management.commands.granola_notes.GranolaClient")
def test_gemini_fallback_does_not_search_loose_note_body(client_class):
    client_class.return_value.iter_notes.return_value = iter([])
    lead = Lead.objects.create(
        first_name="Other",
        last_name="Buyer",
        company_name="Different Company",
        linkedin_url="https://www.linkedin.com/in/different-buyer/",
    )
    Meeting.objects.create(
        source=Meeting.Source.GOOGLE_CALENDAR,
        external_id="unrelated-fedramp-note",
        lead=lead,
        start_at=timezone.now(),
        title="Unrelated discovery call",
        gemini_notes_raw="The buyer asked about the Fed Ramp process.",
    )
    stdout = io.StringIO()

    call_command("granola_notes", search="Ramp", stdout=stdout)

    payload = json.loads(stdout.getvalue())
    assert payload["sources_checked"] == ["granola", "gemini"]
    assert payload["source"] is None
    assert payload["count"] == 0
