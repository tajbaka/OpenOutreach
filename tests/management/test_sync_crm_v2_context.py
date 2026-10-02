from __future__ import annotations

import json
from datetime import timedelta

import pytest
from django.core.management import call_command
from django.utils import timezone

from crm.models import Lead, Meeting, MeetingNote, MeetingNoteSyncState
from linkedin.management.commands import sync_crm_v2_context as command_module


pytestmark = pytest.mark.django_db


def _candidate():
    return {
        "account_key": "arian_boundera",
        "email": "person@safe-company.example",
        "display_name": "Safe Person",
        "domain": "safe-company.example",
        "last_inbound_at": (timezone.now() - timedelta(days=1)).isoformat(),
        "latest_thread_id": "arian_boundera:thread-safe-company",
        "thread_count": 1,
    }


def test_default_context_command_is_no_write(monkeypatch, capsys):
    monkeypatch.setattr(
        command_module,
        "_private_discovery_candidates",
        lambda: [_candidate()],
    )

    call_command(
        "sync_crm_v2_context",
        "--skip-gmail-refresh",
        "--skip-granola",
    )

    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["mode"] == "dry-run"
    assert payload["email_first"]["counts"]["created"] == 1
    assert Lead.objects.count() == 0


def test_apply_relinks_gmail_only_when_a_safe_lead_is_created(
    monkeypatch,
    capsys,
):
    monkeypatch.setattr(
        command_module,
        "_private_discovery_candidates",
        lambda: [_candidate()],
    )
    calls = []

    def fake_gmail(**kwargs):
        calls.append(kwargs)
        return {"status": "refreshed"}

    monkeypatch.setattr(command_module, "_run_gmail_context", fake_gmail)

    call_command(
        "sync_crm_v2_context",
        "--apply",
        "--skip-gmail-refresh",
        "--skip-granola",
    )

    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["email_first"]["counts"]["created"] == 1
    assert Lead.objects.count() == 1
    assert len(calls) == 1
    assert calls[0]["skip_unmapped_discovery"] is True
    assert calls[0]["skip_notes"] is True


def test_repeat_is_idempotent_and_skips_second_gmail_pass(monkeypatch, capsys):
    candidate = _candidate()
    monkeypatch.setattr(
        command_module,
        "_private_discovery_candidates",
        lambda: [candidate],
    )
    call_command(
        "sync_crm_v2_context",
        "--apply",
        "--skip-gmail-refresh",
        "--skip-granola",
    )
    capsys.readouterr()
    calls = []
    monkeypatch.setattr(
        command_module,
        "_run_gmail_context",
        lambda **kwargs: calls.append(kwargs) or {"status": "refreshed"},
    )

    call_command(
        "sync_crm_v2_context",
        "--apply",
        "--skip-gmail-refresh",
        "--skip-granola",
    )

    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["email_first"]["counts"]["existing"] == 1
    assert calls == []
    assert Lead.objects.count() == 1


def test_successful_apply_note_scan_records_fresh_gemini_state(monkeypatch):
    monkeypatch.setattr(command_module, "call_command", lambda *args, **kwargs: None)

    result = command_module._run_gmail_context(
        apply=True,
        since_days=365,
        skip_unmapped_discovery=False,
        skip_notes=False,
    )

    state = MeetingNoteSyncState.objects.get(source=MeetingNote.Source.GEMINI)
    assert result["status"] == "refreshed"
    assert state.status == MeetingNoteSyncState.Status.SUCCESS
    assert state.last_attempt_at is not None
    assert state.last_success_at is not None
    assert state.last_error_kind == ""
    assert state.last_error_message == ""


def test_relink_without_note_scan_does_not_claim_gemini_freshness(monkeypatch):
    monkeypatch.setattr(command_module, "call_command", lambda *args, **kwargs: None)

    command_module._run_gmail_context(
        apply=True,
        since_days=365,
        skip_unmapped_discovery=True,
        skip_notes=True,
    )

    assert not MeetingNoteSyncState.objects.filter(
        source=MeetingNote.Source.GEMINI,
    ).exists()


@pytest.mark.parametrize("apply", [False, True])
def test_scheduled_context_skips_gemini_and_preserves_historical_notes(
    monkeypatch, apply,
):
    old_time = timezone.now() - timedelta(days=30)
    lead = Lead.objects.create(
        linkedin_url="https://www.linkedin.com/in/historical-note-test/",
    )
    meeting = Meeting.objects.create(
        source=Meeting.Source.GOOGLE_CALENDAR,
        external_id="historical-event",
        lead=lead,
        start_at=old_time,
        gemini_doc_id="historical-doc",
        gemini_notes_raw="Previously imported Gemini notes",
        gemini_notes_fetched_at=old_time,
    )
    note = MeetingNote.objects.create(
        source=MeetingNote.Source.GEMINI,
        external_id="historical-doc",
        meeting=meeting,
        content="Previously imported Gemini notes",
        detail_status=MeetingNote.DetailStatus.COMPLETE,
    )
    state = MeetingNoteSyncState.objects.create(
        source=MeetingNote.Source.GEMINI,
        status=MeetingNoteSyncState.Status.SUCCESS,
        last_success_at=old_time,
    )
    before = [
        model.objects.filter(pk=row.pk).values().get()
        for model, row in ((Meeting, meeting), (MeetingNote, note), (MeetingNoteSyncState, state))
    ]
    gmail_calls = []
    granola_calls = []
    monkeypatch.setattr(command_module, "_private_discovery_candidates", lambda: [])
    monkeypatch.setattr(
        command_module, "call_command",
        lambda *args, **kwargs: gmail_calls.append((args, kwargs)),
    )
    monkeypatch.setattr(
        command_module, "_sync_granola",
        lambda **kwargs: granola_calls.append(kwargs) or {"status": "success"},
    )

    call_command("sync_crm_v2_context", apply=apply)

    assert len(gmail_calls) == 1
    args, kwargs = gmail_calls[0]
    assert args == ("sync_gmail_context",)
    assert kwargs["skip_notes"] is True
    assert kwargs.get("skip_threads", False) is False
    assert kwargs["skip_unmapped_discovery"] is False
    assert kwargs["dry_run"] is not apply
    assert len(granola_calls) == 1
    assert granola_calls[0]["apply"] is apply
    after = [
        model.objects.filter(pk=row.pk).values().get()
        for model, row in ((Meeting, meeting), (MeetingNote, note), (MeetingNoteSyncState, state))
    ]
    assert after == before
