from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from crm.models import (
    Account,
    Lead,
    Meeting,
    MeetingNote,
    MeetingNoteSyncState,
    Opportunity,
    OpportunityContact,
)
from linkedin.exceptions import (
    GranolaError,
    GranolaNotFoundError,
    GranolaPayloadTooLargeError,
    GranolaRequestError,
    GranolaResponseError,
    GranolaTransientError,
)
from linkedin.granola_sync import (
    rematch_cached_granola_notes,
    resolve_meeting_context,
    sync_granola_meeting_notes,
)


NOW = datetime(2026, 8, 26, 15, 0, tzinfo=timezone.utc)
MEETING_AT = datetime(2026, 8, 20, 18, 0, tzinfo=timezone.utc)


class FakeGranolaClient:
    def __init__(self, metadata=(), details=None, transcripts=None, list_error=None):
        self.metadata = list(metadata)
        self.details = details or {}
        self.transcripts = transcripts or {}
        self.list_error = list_error
        self.iter_calls: list[dict] = []
        self.detail_calls: list[str] = []
        self.transcript_calls: list[str] = []

    def iter_notes(self, **kwargs):
        self.iter_calls.append(kwargs)
        if self.list_error:
            raise self.list_error
        return iter(self.metadata)

    def get_note(self, note_id):
        self.detail_calls.append(note_id)
        value = self.details[note_id]
        if isinstance(value, Exception):
            raise value
        return value

    def get_transcript(self, note_id):
        self.transcript_calls.append(note_id)
        value = self.transcripts[note_id]
        if isinstance(value, Exception):
            raise value
        return value


def _lead(*, name="Zelia Pantani", email="zelia@ramp.com", company="Ramp", suffix=""):
    first, last = name.split(" ", 1)
    return Lead.objects.create(
        first_name=first,
        last_name=last,
        company_name=company,
        email=email,
        linkedin_url=f"https://www.linkedin.com/in/{first.lower()}-{last.lower()}{suffix}/",
    )


def _opportunity(lead, *, account_name="Ramp", domain="ramp.com", motion="primary"):
    account = Account.objects.create(name=account_name, domain=domain)
    opportunity = Opportunity.objects.create(
        account=account,
        motion_key=motion,
        name=f"{account_name} opportunity",
        stage=Opportunity.Stage.PROSPECTING,
        sales_motion_step=1,
    )
    OpportunityContact.objects.create(
        opportunity=opportunity,
        lead=lead,
        role=OpportunityContact.Role.CHAMPION,
    )
    return opportunity


def _meeting(lead, opportunity, *, event_id="event-ramp", title="Ramp sandbox"):
    return Meeting.objects.create(
        source=Meeting.Source.GOOGLE_CALENDAR,
        external_id=event_id,
        lead=lead,
        opportunity=opportunity,
        start_at=MEETING_AT,
        end_at=MEETING_AT + timedelta(minutes=30),
        title=title,
    )


def _metadata(note_id="not_aaaaaaaaaaaaaa", *, updated_at="2026-08-20T19:00:00Z"):
    return {
        "id": note_id,
        "object": "note",
        "title": "Ramp sandbox",
        "owner": {"name": "Arian Taj", "email": "ariant@getboundera.com"},
        "created_at": "2026-08-20T18:00:00Z",
        "updated_at": updated_at,
    }


def _detail(
    note_id="not_aaaaaaaaaaaaaa",
    *,
    attendee_email="zelia@ramp.com",
    attendee_name="Zelia Pantani",
    event_id="event-ramp",
    event_title="Ramp sandbox",
    summary="Ramp wants to validate the sandbox evidence workflow.",
):
    attendees = [
        {"name": "Arian Taj", "email": "ariant@getboundera.com"},
    ]
    if attendee_email or attendee_name:
        attendees.append({"name": attendee_name, "email": attendee_email})
    return {
        **_metadata(note_id),
        "web_url": f"https://notes.granola.ai/d/{note_id}",
        "calendar_event": {
            "event_title": event_title,
            "invitees": [{"email": attendee_email}] if attendee_email else [],
            "organiser": "ariant@getboundera.com",
            "calendar_event_id": event_id,
            "scheduled_start_time": "2026-08-20T18:00:00Z",
            "scheduled_end_time": "2026-08-20T18:30:00Z",
        },
        "attendees": attendees,
        "folder_membership": [],
        "summary_text": summary,
        "summary_markdown": f"## Summary\n\n{summary}",
        "transcript": None,
    }


@pytest.mark.django_db
def test_sync_batches_once_matches_exact_attendee_email_and_caches_detail():
    lead = _lead()
    opportunity = _opportunity(lead)
    meeting = _meeting(lead, opportunity)
    metadata = _metadata()
    client = FakeGranolaClient(
        [metadata],
        details={metadata["id"]: _detail()},
    )

    result = sync_granola_meeting_notes(client=client, now=NOW)

    note = MeetingNote.objects.get(source=MeetingNote.Source.GRANOLA)
    assert result.status == MeetingNoteSyncState.Status.SUCCESS
    assert result.matched_by_method == {MeetingNote.MatchMethod.ATTENDEE_EMAIL: 1}
    assert client.detail_calls == [metadata["id"]]
    assert note.meeting == meeting
    assert note.opportunity == opportunity
    assert note.match_status == MeetingNote.MatchStatus.MATCHED
    participant = meeting.participant_links.get(lead=lead)
    assert participant.attendee_email == "zelia@ramp.com"

    second_client = FakeGranolaClient(
        [metadata],
        details={metadata["id"]: AssertionError("unchanged detail was refetched")},
    )
    second = sync_granola_meeting_notes(client=second_client, now=NOW + timedelta(days=1))

    assert second.unchanged == 1
    assert second_client.detail_calls == []
    assert second_client.iter_calls[0]["updated_after"].endswith("Z")


@pytest.mark.django_db
def test_sync_fetches_active_transcript_once_and_reuses_cache():
    lead = _lead()
    opportunity = _opportunity(lead)
    _meeting(lead, opportunity)
    metadata = _metadata()
    transcript = [{"text": "Ramp needs a security review.", "speaker": {}}]
    client = FakeGranolaClient(
        [metadata],
        details={metadata["id"]: _detail()},
        transcripts={metadata["id"]: transcript},
    )

    first = sync_granola_meeting_notes(
        client=client,
        now=NOW,
        active_opportunity_ids=[opportunity.id],
    )

    note = MeetingNote.objects.get(source=MeetingNote.Source.GRANOLA)
    assert first.transcripts_fetched == 1
    assert client.transcript_calls == [metadata["id"]]
    assert note.transcript == transcript
    assert note.transcript_fetched_at == NOW

    second_client = FakeGranolaClient(
        [metadata],
        details={metadata["id"]: AssertionError("detail cache missed")},
    )
    second = sync_granola_meeting_notes(
        client=second_client,
        now=NOW + timedelta(days=1),
        active_opportunity_ids=[opportunity.id],
    )

    assert second.transcripts_fetched == 0
    assert second_client.transcript_calls == []

    updated_metadata = _metadata(updated_at="2026-08-27T16:00:00Z")
    updated_detail = _detail()
    updated_detail["updated_at"] = updated_metadata["updated_at"]
    updated_transcript = [{"text": "The security review is complete.", "speaker": {}}]
    third_client = FakeGranolaClient(
        [updated_metadata],
        details={updated_metadata["id"]: updated_detail},
        transcripts={updated_metadata["id"]: updated_transcript},
    )

    third = sync_granola_meeting_notes(
        client=third_client,
        now=NOW + timedelta(days=2),
        active_opportunity_ids=[opportunity.id],
    )

    note.refresh_from_db()
    assert third.details_fetched == 1
    assert third.transcripts_fetched == 1
    assert note.transcript == updated_transcript


@pytest.mark.django_db
def test_matcher_never_uses_summary_or_substring_account_text():
    lead = _lead()
    _opportunity(lead)
    metadata = _metadata()
    detail = _detail(
        attendee_email="",
        attendee_name="",
        event_id="",
        event_title="FedRAMP market briefing",
        summary="Ramp is mentioned repeatedly in this loose note body.",
    )
    client = FakeGranolaClient([metadata], details={metadata["id"]: detail})

    result = sync_granola_meeting_notes(client=client, now=NOW)

    note = MeetingNote.objects.get(source=MeetingNote.Source.GRANOLA)
    assert result.unmatched == 1
    assert note.match_status == MeetingNote.MatchStatus.UNMATCHED
    assert note.meeting_id is None
    assert note.opportunity_id is None


@pytest.mark.django_db
def test_duplicate_attendee_email_across_opportunities_is_ambiguous():
    first = _lead(suffix="-one")
    second = _lead(name="Zelia Duplicate", suffix="-two")
    _opportunity(first, account_name="Ramp One", domain="ramp-one.com", motion="one")
    _opportunity(second, account_name="Ramp Two", domain="ramp-two.com", motion="two")
    metadata = _metadata()
    client = FakeGranolaClient([metadata], details={metadata["id"]: _detail()})

    result = sync_granola_meeting_notes(client=client, now=NOW)

    note = MeetingNote.objects.get(source=MeetingNote.Source.GRANOLA)
    assert result.ambiguous == 1
    assert note.match_status == MeetingNote.MatchStatus.AMBIGUOUS
    assert note.match_evidence["reason"] == "attendee email resolves to duplicate CRM contacts"
    assert note.meeting_id is None
    assert note.opportunity_id is None


@pytest.mark.django_db
def test_cached_unmatched_note_can_match_after_opportunity_contact_is_added():
    lead = _lead()
    metadata = _metadata()
    client = FakeGranolaClient([metadata], details={metadata["id"]: _detail()})
    first = sync_granola_meeting_notes(client=client, now=NOW)
    assert first.unmatched == 1

    opportunity = _opportunity(lead)
    counts = rematch_cached_granola_notes()

    note = MeetingNote.objects.get(source=MeetingNote.Source.GRANOLA)
    assert counts == {MeetingNote.MatchStatus.MATCHED: 1}
    assert note.opportunity == opportunity
    assert note.match_method == MeetingNote.MatchMethod.ATTENDEE_EMAIL


@pytest.mark.django_db
def test_resolver_prefers_granola_and_falls_back_to_gemini_on_source_failure():
    lead = _lead()
    opportunity = _opportunity(lead)
    meeting = _meeting(lead, opportunity)
    common = {
        "meeting": meeting,
        "opportunity": opportunity,
        "detail_status": MeetingNote.DetailStatus.COMPLETE,
        "match_status": MeetingNote.MatchStatus.MATCHED,
        "match_method": MeetingNote.MatchMethod.MANUAL,
        "scheduled_start_at": MEETING_AT,
        "source_updated_at": MEETING_AT,
        "fetched_at": NOW,
    }
    MeetingNote.objects.create(
        source=MeetingNote.Source.GEMINI,
        external_id="gemini-doc-1",
        content="Gemini fallback context",
        title="Ramp sandbox",
        **common,
    )
    MeetingNote.objects.create(
        source=MeetingNote.Source.GRANOLA,
        external_id="not_aaaaaaaaaaaaaa",
        content="Granola primary context",
        title="Ramp sandbox",
        **common,
    )

    primary = resolve_meeting_context(opportunity=opportunity)
    fallback = resolve_meeting_context(
        opportunity=opportunity,
        granola_available=False,
    )

    assert primary is not None and primary.source == MeetingNote.Source.GRANOLA
    assert primary.content == "Granola primary context"
    assert fallback is not None and fallback.source == MeetingNote.Source.GEMINI
    assert fallback.content == "Gemini fallback context"


@pytest.mark.django_db
def test_resolver_falls_back_to_current_meeting_gemini_fields_without_mirror():
    lead = _lead()
    opportunity = _opportunity(lead)
    meeting = _meeting(lead, opportunity)
    meeting.gemini_doc_id = "gemini-live-doc"
    meeting.gemini_doc_title = "Ramp notes by Gemini"
    meeting.gemini_notes_raw = "Fresh Gemini notes not mirrored into MeetingNote yet."
    meeting.gemini_notes_fetched_at = NOW
    meeting.save(update_fields={
        "gemini_doc_id",
        "gemini_doc_title",
        "gemini_notes_raw",
        "gemini_notes_fetched_at",
        "update_date",
    })

    context = resolve_meeting_context(opportunity=opportunity)

    assert context is not None
    assert context.source == MeetingNote.Source.GEMINI
    assert context.external_id == "gemini-live-doc"
    assert context.meeting_id == meeting.id
    assert context.opportunity_id == str(opportunity.id)
    assert context.content == "Fresh Gemini notes not mirrored into MeetingNote yet."


@pytest.mark.django_db
def test_list_failure_records_failure_without_mutating_cached_context():
    lead = _lead()
    opportunity = _opportunity(lead)
    cached = MeetingNote.objects.create(
        source=MeetingNote.Source.GRANOLA,
        external_id="not_aaaaaaaaaaaaaa",
        opportunity=opportunity,
        title="Cached",
        content="Do not erase",
        source_created_at=MEETING_AT,
        source_updated_at=MEETING_AT,
        fetched_at=NOW,
        detail_status=MeetingNote.DetailStatus.COMPLETE,
        match_status=MeetingNote.MatchStatus.MATCHED,
        match_method=MeetingNote.MatchMethod.MANUAL,
    )
    client = FakeGranolaClient(
        list_error=GranolaTransientError("temporary outage", status_code=503),
    )

    result = sync_granola_meeting_notes(client=client, now=NOW)

    cached.refresh_from_db()
    state = MeetingNoteSyncState.objects.get(source=MeetingNote.Source.GRANOLA)
    assert result.status == MeetingNoteSyncState.Status.FAILED
    assert result.source_available is False
    assert state.last_error_kind == "GranolaTransientError"
    assert cached.content == "Do not erase"
    assert cached.match_status == MeetingNote.MatchStatus.MATCHED


@pytest.mark.django_db
def test_detail_failure_is_retryable_and_does_not_erase_previous_content():
    lead = _lead()
    opportunity = _opportunity(lead)
    MeetingNote.objects.create(
        source=MeetingNote.Source.GRANOLA,
        external_id="not_aaaaaaaaaaaaaa",
        opportunity=opportunity,
        title="Cached",
        content="Previous complete context",
        source_created_at=MEETING_AT,
        source_updated_at=MEETING_AT,
        fetched_at=NOW - timedelta(days=1),
        detail_status=MeetingNote.DetailStatus.COMPLETE,
        match_status=MeetingNote.MatchStatus.MATCHED,
        match_method=MeetingNote.MatchMethod.MANUAL,
    )
    metadata = _metadata(updated_at="2026-08-26T14:00:00Z")
    client = FakeGranolaClient(
        [metadata],
        details={metadata["id"]: GranolaTransientError("timeout")},
    )

    result = sync_granola_meeting_notes(client=client, now=NOW)

    note = MeetingNote.objects.get(source=MeetingNote.Source.GRANOLA)
    assert result.status == MeetingNoteSyncState.Status.PARTIAL
    assert result.detail_retries == 1
    assert result.watermark_after == NOW
    assert note.detail_status == MeetingNote.DetailStatus.RETRY
    assert note.content == "Previous complete context"
    state = MeetingNoteSyncState.objects.get(source=MeetingNote.Source.GRANOLA)
    assert state.successful_watermark == NOW

    recovered_detail = _detail()
    recovered_detail["updated_at"] = metadata["updated_at"]
    recovery_client = FakeGranolaClient(
        [],
        details={metadata["id"]: recovered_detail},
    )
    recovered = sync_granola_meeting_notes(
        client=recovery_client,
        now=NOW + timedelta(days=1),
    )

    note.refresh_from_db()
    assert recovery_client.detail_calls == [metadata["id"]]
    assert recovered.details_fetched == 1
    assert note.detail_status == MeetingNote.DetailStatus.COMPLETE


@pytest.mark.django_db
def test_nonretryable_detail_api_error_warns_instead_of_aborting_refresh():
    metadata = _metadata()
    client = FakeGranolaClient(
        [metadata],
        details={metadata["id"]: GranolaError("Granola API returned HTTP 400.")},
    )

    result = sync_granola_meeting_notes(client=client, now=NOW)

    note = MeetingNote.objects.get(source=MeetingNote.Source.GRANOLA)
    assert result.status == MeetingNoteSyncState.Status.PARTIAL
    assert result.source_available is False
    assert result.watermark_after == NOW
    assert note.detail_status == MeetingNote.DetailStatus.RETRY
    assert any("use Gemini" in warning for warning in result.warnings)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "failure",
    [
        GranolaRequestError("method rejected", status_code=405),
        GranolaResponseError("poison detail payload"),
    ],
    ids=["request-error", "schema-error"],
)
def test_note_local_poison_is_quarantined_without_starving_later_notes(failure):
    first_id = "not_aaaaaaaaaaaaaa"
    second_id = "not_bbbbbbbbbbbbbb"
    metadata = [_metadata(first_id), _metadata(second_id)]
    client = FakeGranolaClient(
        metadata,
        details={
            first_id: failure,
            second_id: _detail(second_id),
        },
    )

    result = sync_granola_meeting_notes(client=client, now=NOW)

    first = MeetingNote.objects.get(external_id=first_id)
    second = MeetingNote.objects.get(external_id=second_id)
    assert client.detail_calls == [first_id, second_id]
    assert first.detail_status == MeetingNote.DetailStatus.RETRY
    assert second.detail_status == MeetingNote.DetailStatus.COMPLETE
    assert result.details_fetched == 1
    assert result.detail_retries == 1
    assert result.source_available is True
    assert result.watermark_after == NOW


@pytest.mark.django_db
def test_only_true_not_found_is_terminally_unavailable():
    metadata = _metadata()
    client = FakeGranolaClient(
        [metadata],
        details={metadata["id"]: GranolaNotFoundError("gone")},
    )

    result = sync_granola_meeting_notes(client=client, now=NOW)

    note = MeetingNote.objects.get(external_id=metadata["id"])
    assert result.status == MeetingNoteSyncState.Status.SUCCESS
    assert result.unavailable == 1
    assert result.detail_retries == 0
    assert note.detail_status == MeetingNote.DetailStatus.UNAVAILABLE


@pytest.mark.django_db
def test_detail_budget_persists_full_metadata_scan_and_progresses_oldest_first():
    note_ids = [
        "not_aaaaaaaaaaaaaa",
        "not_bbbbbbbbbbbbbb",
        "not_cccccccccccccc",
    ]
    metadata = [_metadata(note_id) for note_id in note_ids]
    first_client = FakeGranolaClient(
        metadata,
        details={note_ids[0]: _detail(note_ids[0])},
    )

    first = sync_granola_meeting_notes(
        client=first_client,
        now=NOW,
        max_notes=1,
    )

    assert first.metadata_seen == 3
    assert first_client.iter_calls[0]["max_notes"] is None
    assert first_client.detail_calls == [note_ids[0]]
    assert first.pending_details == 2
    assert first.watermark_after == NOW

    second_client = FakeGranolaClient(
        [],
        details={note_ids[1]: _detail(note_ids[1])},
    )
    second = sync_granola_meeting_notes(
        client=second_client,
        now=NOW + timedelta(days=1),
        max_notes=1,
    )
    assert second_client.detail_calls == [note_ids[1]]
    assert second.pending_details == 1

    third_client = FakeGranolaClient(
        [],
        details={note_ids[2]: _detail(note_ids[2])},
    )
    third = sync_granola_meeting_notes(
        client=third_client,
        now=NOW + timedelta(days=2),
        max_notes=1,
    )
    assert third_client.detail_calls == [note_ids[2]]
    assert third.pending_details == 0
    assert third.status == MeetingNoteSyncState.Status.SUCCESS


@pytest.mark.django_db
def test_first_sync_filters_on_recent_updates_not_only_creation_time():
    client = FakeGranolaClient()

    sync_granola_meeting_notes(
        client=client,
        now=NOW,
        first_sync_lookback_days=30,
    )

    call = client.iter_calls[0]
    assert call["updated_after"].startswith("2026-07-27T15:00:00")
    assert "created_after" not in call
    assert call["max_notes"] is None


@pytest.mark.django_db
def test_client_configuration_error_records_failure_and_falls_back():
    error = GranolaError("Granola HTTP timeout must be positive.")

    result = sync_granola_meeting_notes(
        client=None,
        client_error=error,
        now=NOW,
    )

    state = MeetingNoteSyncState.objects.get(source=MeetingNote.Source.GRANOLA)
    assert result.status == MeetingNoteSyncState.Status.FAILED
    assert result.source_available is False
    assert any("using Gemini" in warning for warning in result.warnings)
    assert state.last_error_kind == "GranolaError"


@pytest.mark.django_db
def test_duplicate_email_inside_one_opportunity_stays_ambiguous():
    first = _lead(suffix="-first")
    second = _lead(name="Zelia Duplicate", suffix="-second")
    opportunity = _opportunity(first)
    OpportunityContact.objects.create(
        opportunity=opportunity,
        lead=second,
        role=OpportunityContact.Role.STAKEHOLDER,
    )
    metadata = _metadata()
    client = FakeGranolaClient([metadata], details={metadata["id"]: _detail()})

    result = sync_granola_meeting_notes(client=client, now=NOW)

    note = MeetingNote.objects.get(source=MeetingNote.Source.GRANOLA)
    assert result.ambiguous == 1
    assert note.match_status == MeetingNote.MatchStatus.AMBIGUOUS
    assert note.match_evidence["reason"] == (
        "attendee email resolves to duplicate CRM contacts"
    )


@pytest.mark.django_db
def test_payload_too_large_note_remains_retryable_and_caches_transcript_fallback():
    metadata = _metadata()
    transcript = [{"text": "Fallback transcript", "speaker": {}}]
    client = FakeGranolaClient(
        [metadata],
        details={metadata["id"]: GranolaPayloadTooLargeError("too large")},
        transcripts={metadata["id"]: transcript},
    )

    result = sync_granola_meeting_notes(client=client, now=NOW)

    note = MeetingNote.objects.get(source=MeetingNote.Source.GRANOLA)
    assert result.status == MeetingNoteSyncState.Status.PARTIAL
    assert result.unavailable == 0
    assert result.detail_retries == 1
    assert result.transcripts_fetched == 1
    assert result.source_available is True
    assert result.watermark_after == NOW
    assert note.detail_status == MeetingNote.DetailStatus.RETRY
    assert note.transcript == transcript


@pytest.mark.django_db
def test_dry_run_uses_existing_watermark_without_mutating_sync_state():
    previous_watermark = NOW - timedelta(days=7)
    previous_attempt = NOW - timedelta(days=2)
    state = MeetingNoteSyncState.objects.create(
        source=MeetingNote.Source.GRANOLA,
        successful_watermark=previous_watermark,
        last_attempt_at=previous_attempt,
        status=MeetingNoteSyncState.Status.SUCCESS,
    )
    client = FakeGranolaClient()

    result = sync_granola_meeting_notes(
        client=client,
        now=NOW,
        dry_run=True,
    )

    state.refresh_from_db()
    assert result.watermark_before == previous_watermark
    assert client.iter_calls[0]["updated_after"].startswith("2026-08-18T15:00:00")
    assert state.successful_watermark == previous_watermark
    assert state.last_attempt_at == previous_attempt
    assert state.status == MeetingNoteSyncState.Status.SUCCESS


@pytest.mark.django_db
def test_resolver_never_uses_a_note_linked_to_a_different_meeting():
    lead = _lead()
    opportunity = _opportunity(lead)
    requested = _meeting(lead, opportunity, event_id="event-requested")
    other = Meeting.objects.create(
        source=Meeting.Source.GOOGLE_CALENDAR,
        external_id="event-other",
        lead=lead,
        opportunity=opportunity,
        start_at=MEETING_AT + timedelta(days=1),
        title="Other Ramp call",
    )
    common = {
        "opportunity": opportunity,
        "detail_status": MeetingNote.DetailStatus.COMPLETE,
        "match_status": MeetingNote.MatchStatus.MATCHED,
        "match_method": MeetingNote.MatchMethod.MANUAL,
        "fetched_at": NOW,
    }
    MeetingNote.objects.create(
        source=MeetingNote.Source.GRANOLA,
        external_id="not_aaaaaaaaaaaaaa",
        meeting=requested,
        title="Requested",
        content="Requested meeting context",
        scheduled_start_at=requested.start_at,
        source_updated_at=MEETING_AT,
        **common,
    )
    MeetingNote.objects.create(
        source=MeetingNote.Source.GRANOLA,
        external_id="not_bbbbbbbbbbbbbb",
        meeting=other,
        title="Other",
        content="Wrong meeting context",
        scheduled_start_at=other.start_at,
        source_updated_at=MEETING_AT + timedelta(days=1),
        **common,
    )

    context = resolve_meeting_context(meeting=requested, opportunity=opportunity)

    assert context is not None
    assert context.content == "Requested meeting context"


@pytest.mark.django_db
def test_account_date_title_requires_all_structured_components():
    lead = _lead(email="prospect@ramp.com")
    opportunity = _opportunity(lead)
    meeting = _meeting(lead, opportunity)
    metadata = _metadata()
    detail = _detail(
        attendee_email="other@ramp.com",
        attendee_name="Untracked Person",
        event_id="",
        event_title="Ramp sandbox",
    )
    client = FakeGranolaClient([metadata], details={metadata["id"]: detail})

    result = sync_granola_meeting_notes(client=client, now=NOW)

    note = MeetingNote.objects.get(source=MeetingNote.Source.GRANOLA)
    assert result.matched_by_method == {MeetingNote.MatchMethod.ACCOUNT_DATE_TITLE: 1}
    assert note.meeting == meeting
    assert note.opportunity == opportunity
