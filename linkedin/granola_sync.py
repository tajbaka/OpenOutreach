"""Batch Granola cache sync, structured CRM matching, and context selection.

This module deliberately separates three concerns:

* :class:`linkedin.granola.GranolaClient` owns HTTP transport.
* ``sync_granola_meeting_notes`` fetches each changed note once and persists it.
* ``GranolaNoteMatcher`` associates cached notes using structured identity only.

Meeting-note content is enrichment.  Nothing here creates Opportunities or
OpportunityActions, advances pipeline state, or decides followup eligibility.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Iterable

from django.db import transaction
from django.utils import timezone

from crm.models import (
    Lead,
    Meeting,
    MeetingNote,
    MeetingNoteSyncState,
    MeetingParticipant,
    Opportunity,
    OpportunityContact,
)
from gmail.auth import GMAIL_ACCOUNTS, GMAIL_OPERATOR_MAPPING
from linkedin.exceptions import (
    GranolaAuthenticationError,
    GranolaError,
    GranolaNotFoundError,
    GranolaPayloadTooLargeError,
    GranolaRequestError,
    GranolaResponseError,
    GranolaTransientError,
)
from linkedin.granola import NOTE_ID_PATTERN, GranolaClient


logger = logging.getLogger(__name__)

MATCHER_VERSION = 1
DEFAULT_FIRST_SYNC_LOOKBACK_DAYS = 365
DEFAULT_WATERMARK_OVERLAP = timedelta(days=1)
DEFAULT_MEETING_TIME_TOLERANCE = timedelta(minutes=15)
INTERNAL_EMAIL_DOMAINS = frozenset({
    "boundera.io",
    "getboundera.com",
    "tryfedrampgpt.com",
})


@dataclass
class GranolaSyncResult:
    status: str = MeetingNoteSyncState.Status.IDLE
    source_available: bool = False
    metadata_seen: int = 0
    metadata_failures: int = 0
    detail_attempts: int = 0
    details_fetched: int = 0
    unchanged: int = 0
    transcripts_fetched: int = 0
    transcript_failures: int = 0
    detail_retries: int = 0
    pending_details: int = 0
    unavailable: int = 0
    matched: int = 0
    ambiguous: int = 0
    unmatched: int = 0
    matched_by_method: dict[str, int] = field(default_factory=dict)
    watermark_before: datetime | None = None
    watermark_after: datetime | None = None
    warnings: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, Any]:
        return {
            "metadata_seen": self.metadata_seen,
            "metadata_failures": self.metadata_failures,
            "detail_attempts": self.detail_attempts,
            "details_fetched": self.details_fetched,
            "unchanged": self.unchanged,
            "transcripts_fetched": self.transcripts_fetched,
            "transcript_failures": self.transcript_failures,
            "detail_retries": self.detail_retries,
            "pending_details": self.pending_details,
            "unavailable": self.unavailable,
            "matched": self.matched,
            "ambiguous": self.ambiguous,
            "unmatched": self.unmatched,
            "matched_by_method": dict(self.matched_by_method),
            "source_available": self.source_available,
            "status": self.status,
            "watermark_before": (
                self.watermark_before.isoformat() if self.watermark_before else None
            ),
            "watermark_after": (
                self.watermark_after.isoformat() if self.watermark_after else None
            ),
        }


@dataclass(frozen=True)
class ResolvedMeetingContext:
    source: str
    external_id: str
    meeting_id: int | None
    opportunity_id: str | None
    title: str
    scheduled_start_at: datetime | None
    content: str
    source_updated_at: datetime | None
    fetched_at: datetime | None


@dataclass(frozen=True)
class MatchDecision:
    status: str
    method: str = ""
    meeting: Meeting | None = None
    opportunity: Opportunity | None = None
    leads: tuple[Lead, ...] = ()
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _StructuredNote:
    external_emails: tuple[str, ...]
    external_attendee_names: tuple[str, ...]
    title: str
    normalized_title: str
    calendar_event_id: str
    scheduled_start_at: datetime | None
    attendee_by_email: dict[str, dict[str, str]]


class GranolaNoteMatcher:
    """In-memory CRM identity indexes shared by every note in one sync."""

    def __init__(
        self,
        *,
        internal_emails: Iterable[str] = (),
        time_tolerance: timedelta = DEFAULT_MEETING_TIME_TOLERANCE,
    ):
        self.time_tolerance = time_tolerance
        self.internal_emails = _default_internal_emails() | {
            _normalize_email(value) for value in internal_emails if value
        }
        self.leads = list(
            Lead.objects.filter(disqualified=False)
            .only(
                "id", "first_name", "last_name", "company_name", "email",
                "linkedin_url", "disqualified",
            )
            .order_by("id")
        )
        self.leads_by_email: dict[str, list[Lead]] = defaultdict(list)
        self.leads_by_name: dict[str, list[Lead]] = defaultdict(list)
        for lead in self.leads:
            email = _normalize_email(lead.email)
            if email:
                self.leads_by_email[email].append(lead)
            name = _normalize_identity(f"{lead.first_name} {lead.last_name}")
            if name:
                self.leads_by_name[name].append(lead)

        self.opportunities = list(
            Opportunity.objects.select_related("account").order_by("id")
        )
        contacts = list(
            OpportunityContact.objects.select_related("opportunity__account", "lead")
            .order_by("opportunity_id", "lead_id", "role")
        )
        self.opportunities_by_lead: dict[int, dict[Any, Opportunity]] = defaultdict(dict)
        for contact in contacts:
            self.opportunities_by_lead[contact.lead_id][contact.opportunity_id] = (
                contact.opportunity
            )

        self.meetings = list(
            Meeting.objects.select_related("opportunity__account", "lead").order_by("id")
        )
        self.meetings_by_event_id: dict[str, list[Meeting]] = defaultdict(list)
        self.meetings_by_lead: dict[int, dict[int, Meeting]] = defaultdict(dict)
        self.participant_pairs: set[tuple[int, int]] = set()
        for meeting in self.meetings:
            for event_id in _meeting_event_ids(meeting):
                self.meetings_by_event_id[event_id].append(meeting)
            self.meetings_by_lead[meeting.lead_id][meeting.id] = meeting
        for participant in MeetingParticipant.objects.select_related("meeting").all():
            self.meetings_by_lead[participant.lead_id][participant.meeting_id] = (
                participant.meeting
            )
            self.participant_pairs.add((participant.meeting_id, participant.lead_id))

    def decide(self, note: MeetingNote) -> MatchDecision:
        if (
            note.match_status == MeetingNote.MatchStatus.MATCHED
            and note.match_method == MeetingNote.MatchMethod.MANUAL
        ):
            return MatchDecision(
                status=note.match_status,
                method=note.match_method,
                meeting=note.meeting,
                opportunity=note.opportunity,
                evidence=dict(note.match_evidence or {}),
            )

        structured = _structured_note(note, internal_emails=self.internal_emails)

        email_matches = {
            email: tuple(self.leads_by_email.get(email, ()))
            for email in structured.external_emails
            if self.leads_by_email.get(email)
        }
        duplicate_email_matches = {
            email: leads for email, leads in email_matches.items() if len(leads) > 1
        }
        if duplicate_email_matches:
            return self._ambiguous(
                structured,
                reason="attendee email resolves to duplicate CRM contacts",
                leads=_unique_leads(
                    lead
                    for leads in duplicate_email_matches.values()
                    for lead in leads
                ),
            )
        email_leads = _unique_leads(
            lead for leads in email_matches.values() for lead in leads
        )
        if email_leads:
            return self._decision_for_leads(
                structured,
                leads=email_leads,
                method=MeetingNote.MatchMethod.ATTENDEE_EMAIL,
            )

        name_matches = {
            name: tuple(self.leads_by_name.get(name, ()))
            for name in structured.external_attendee_names
            if self.leads_by_name.get(name)
        }
        duplicate_name_matches = {
            name: leads for name, leads in name_matches.items() if len(leads) > 1
        }
        if duplicate_name_matches:
            return self._ambiguous(
                structured,
                reason="attendee identity resolves to duplicate CRM contacts",
                leads=_unique_leads(
                    lead
                    for leads in duplicate_name_matches.values()
                    for lead in leads
                ),
            )
        name_leads = _unique_leads(
            lead for leads in name_matches.values() for lead in leads
        )
        if name_leads:
            return self._decision_for_leads(
                structured,
                leads=name_leads,
                method=MeetingNote.MatchMethod.ATTENDEE_IDENTITY,
            )

        event_meetings = self._event_meetings(structured)
        if len(event_meetings) == 1:
            meeting = event_meetings[0]
            return MatchDecision(
                status=MeetingNote.MatchStatus.MATCHED,
                method=MeetingNote.MatchMethod.EXISTING_EVENT_ID,
                meeting=meeting,
                opportunity=meeting.opportunity,
                evidence=_evidence(
                    structured,
                    reason="exact existing calendar event id",
                    meetings=(meeting,),
                ),
            )
        if len(event_meetings) > 1:
            return self._ambiguous(
                structured,
                reason="calendar event id resolves to multiple meetings",
                meetings=event_meetings,
            )

        account_meetings = self._account_date_title_meetings(structured)
        if len(account_meetings) == 1:
            meeting = account_meetings[0]
            return MatchDecision(
                status=MeetingNote.MatchStatus.MATCHED,
                method=MeetingNote.MatchMethod.ACCOUNT_DATE_TITLE,
                meeting=meeting,
                opportunity=meeting.opportunity,
                evidence=_evidence(
                    structured,
                    reason="exact account, scheduled time, and title",
                    meetings=(meeting,),
                ),
            )
        if len(account_meetings) > 1:
            return self._ambiguous(
                structured,
                reason="account/date/title resolves to multiple meetings",
                meetings=account_meetings,
            )

        return MatchDecision(
            status=MeetingNote.MatchStatus.UNMATCHED,
            evidence=_evidence(structured, reason="no deterministic CRM identity match"),
        )

    def apply(self, note: MeetingNote, decision: MatchDecision) -> MatchDecision:
        if (
            note.match_status == MeetingNote.MatchStatus.MATCHED
            and note.match_method == MeetingNote.MatchMethod.MANUAL
        ):
            return decision

        note.match_status = decision.status
        note.match_method = decision.method
        note.meeting = decision.meeting
        note.opportunity = decision.opportunity
        note.match_evidence = decision.evidence
        note.matcher_version = MATCHER_VERSION
        note.save(update_fields={
            "match_status",
            "match_method",
            "meeting",
            "opportunity",
            "match_evidence",
            "matcher_version",
            "updated_at",
        })

        if decision.meeting is not None:
            attendee_by_email = _structured_note(
                note,
                internal_emails=self.internal_emails,
            ).attendee_by_email
            for lead in decision.leads:
                attendee = attendee_by_email.get(_normalize_email(lead.email), {})
                MeetingParticipant.objects.update_or_create(
                    meeting=decision.meeting,
                    lead=lead,
                    defaults={
                        "attendee_email": attendee.get("email", ""),
                        "attendee_name": attendee.get("name", ""),
                        "response_status": attendee.get("response_status", ""),
                        "match_method": _participant_match_method(decision.method),
                        "match_evidence": decision.evidence,
                    },
                )
                self.participant_pairs.add((decision.meeting.id, lead.id))
        return decision

    def needs_apply(self, note: MeetingNote, decision: MatchDecision) -> bool:
        """Return whether persisted links/evidence lag the deterministic decision."""
        if (
            note.match_status == MeetingNote.MatchStatus.MATCHED
            and note.match_method == MeetingNote.MatchMethod.MANUAL
        ):
            return False
        if (
            note.matcher_version != MATCHER_VERSION
            or note.match_status != decision.status
            or note.match_method != decision.method
            or note.meeting_id != (
                decision.meeting.id if decision.meeting is not None else None
            )
            or note.opportunity_id != (
                decision.opportunity.id if decision.opportunity is not None else None
            )
            or (note.match_evidence or {}) != decision.evidence
        ):
            return True
        if decision.meeting is None:
            return False
        return any(
            (decision.meeting.id, lead.id) not in self.participant_pairs
            for lead in decision.leads
        )

    def _decision_for_leads(
        self,
        structured: _StructuredNote,
        *,
        leads: tuple[Lead, ...],
        method: str,
    ) -> MatchDecision:
        opportunities: dict[Any, Opportunity] = {}
        for lead in leads:
            opportunities.update(self.opportunities_by_lead.get(lead.id, {}))
        if len(opportunities) > 1:
            return self._ambiguous(
                structured,
                reason="matched contacts belong to multiple opportunities",
                leads=leads,
                opportunities=tuple(opportunities.values()),
            )

        opportunity = next(iter(opportunities.values()), None)
        event_meetings = self._event_meetings(structured)
        if opportunity is not None:
            conflicting = [
                meeting
                for meeting in event_meetings
                if meeting.opportunity_id not in {None, opportunity.id}
            ]
            if conflicting:
                return self._ambiguous(
                    structured,
                    reason="contact opportunity conflicts with calendar event meeting",
                    leads=leads,
                    opportunities=(opportunity,),
                    meetings=tuple(conflicting),
                )
            event_meetings = [
                meeting
                for meeting in event_meetings
                if meeting.opportunity_id in {None, opportunity.id}
            ]
        if len(event_meetings) > 1:
            return self._ambiguous(
                structured,
                reason="structured identity resolves to multiple calendar meetings",
                leads=leads,
                opportunities=((opportunity,) if opportunity else ()),
                meetings=tuple(event_meetings),
            )

        meeting = event_meetings[0] if event_meetings else None
        if meeting is None:
            lead_meetings: dict[int, Meeting] = {}
            for lead in leads:
                lead_meetings.update(self.meetings_by_lead.get(lead.id, {}))
            candidates = [
                value
                for value in lead_meetings.values()
                if self._meeting_matches_time_and_title(value, structured)
                and (
                    opportunity is None
                    or value.opportunity_id in {None, opportunity.id}
                )
            ]
            if len(candidates) > 1:
                return self._ambiguous(
                    structured,
                    reason="structured identity resolves to multiple dated meetings",
                    leads=leads,
                    opportunities=((opportunity,) if opportunity else ()),
                    meetings=tuple(candidates),
                )
            meeting = candidates[0] if candidates else None

        if meeting is not None and opportunity is None:
            opportunity = meeting.opportunity
        if meeting is None and opportunity is None:
            return MatchDecision(
                status=MeetingNote.MatchStatus.UNMATCHED,
                evidence=_evidence(
                    structured,
                    reason="contact matched but has no canonical opportunity or meeting",
                    leads=leads,
                ),
            )

        return MatchDecision(
            status=MeetingNote.MatchStatus.MATCHED,
            method=method,
            meeting=meeting,
            opportunity=opportunity,
            leads=leads,
            evidence=_evidence(
                structured,
                reason=f"exact {method.replace('_', ' ')}",
                leads=leads,
                opportunities=((opportunity,) if opportunity else ()),
                meetings=((meeting,) if meeting else ()),
            ),
        )

    def _event_meetings(self, structured: _StructuredNote) -> list[Meeting]:
        if not structured.calendar_event_id:
            return []
        return list(self.meetings_by_event_id.get(structured.calendar_event_id, ()))

    def _account_date_title_meetings(
        self,
        structured: _StructuredNote,
    ) -> list[Meeting]:
        if not structured.scheduled_start_at or not structured.normalized_title:
            return []
        domains = {_email_domain(value) for value in structured.external_emails}
        domains.discard("")
        out = []
        for meeting in self.meetings:
            opportunity = meeting.opportunity
            if opportunity is None:
                continue
            account = opportunity.account
            account_name = _normalize_identity(account.name)
            account_domain = _normalize_domain(account.domain)
            account_evidence = (
                bool(account_domain and account_domain in domains)
                or _contains_exact_phrase(structured.normalized_title, account_name)
            )
            if not account_evidence:
                continue
            if self._meeting_matches_time_and_title(meeting, structured):
                out.append(meeting)
        return out

    def _meeting_matches_time_and_title(
        self,
        meeting: Meeting,
        structured: _StructuredNote,
    ) -> bool:
        if structured.scheduled_start_at is None:
            return False
        if abs(meeting.start_at - structured.scheduled_start_at) > self.time_tolerance:
            return False
        return _normalize_identity(meeting.title) == structured.normalized_title

    def _ambiguous(
        self,
        structured: _StructuredNote,
        *,
        reason: str,
        leads: Iterable[Lead] = (),
        opportunities: Iterable[Opportunity] = (),
        meetings: Iterable[Meeting] = (),
    ) -> MatchDecision:
        return MatchDecision(
            status=MeetingNote.MatchStatus.AMBIGUOUS,
            evidence=_evidence(
                structured,
                reason=reason,
                leads=tuple(leads),
                opportunities=tuple(opportunities),
                meetings=tuple(meetings),
            ),
        )


def sync_granola_meeting_notes(
    *,
    client: GranolaClient | None,
    client_error: GranolaError | None = None,
    now: datetime | None = None,
    first_sync_lookback_days: int = DEFAULT_FIRST_SYNC_LOOKBACK_DAYS,
    watermark_overlap: timedelta = DEFAULT_WATERMARK_OVERLAP,
    max_notes: int | None = None,
    max_consecutive_detail_failures: int = 5,
    active_opportunity_ids: Iterable[Any] = (),
    internal_emails: Iterable[str] = (),
    dry_run: bool = False,
) -> GranolaSyncResult:
    """Refresh the Granola cache once and match notes without widening actions.

    ``max_notes`` is a detail-request budget, not a metadata pagination cap.
    The complete metadata cursor is consumed and every pending identity is
    persisted first, so bounded runs make progress across the retry queue.

    Expected Granola failures are converted to a source status so a caller can
    continue with Gemini. Unexpected application errors still propagate.
    """
    if first_sync_lookback_days <= 0:
        raise ValueError("first_sync_lookback_days must be positive")
    if watermark_overlap < timedelta(0):
        raise ValueError("watermark_overlap cannot be negative")
    if max_notes is not None and (
        type(max_notes) is not int or max_notes <= 0
    ):
        raise ValueError("max_notes must be positive when supplied")
    if (
        type(max_consecutive_detail_failures) is not int
        or max_consecutive_detail_failures <= 0
    ):
        raise ValueError("max_consecutive_detail_failures must be positive")
    if client is not None and client_error is not None:
        raise ValueError("client and client_error cannot both be supplied")
    if client_error is not None and not isinstance(client_error, GranolaError):
        raise ValueError("client_error must be a GranolaError")

    attempted_at = now or timezone.now()
    if dry_run:
        state = MeetingNoteSyncState.objects.filter(
            source=MeetingNote.Source.GRANOLA,
        ).first()
    else:
        state, _ = MeetingNoteSyncState.objects.get_or_create(
            source=MeetingNote.Source.GRANOLA,
        )
        state.last_attempt_at = attempted_at
        state.save(update_fields={"last_attempt_at", "updated_at"})

    result = GranolaSyncResult(
        watermark_before=state.successful_watermark if state else None,
    )
    if client is None:
        if client_error is None:
            result.status = MeetingNoteSyncState.Status.DISABLED
            error_kind = "disabled"
            error_message = "GRANOLA_API_KEY is not configured"
            result.warnings.append(
                "Granola API access is disabled; using Gemini context."
            )
        else:
            result.status = MeetingNoteSyncState.Status.FAILED
            error_kind = type(client_error).__name__
            error_message = str(client_error)
            result.warnings.append(
                f"Granola client configuration failed; using Gemini context: "
                f"{client_error}"
            )
        if state is not None and not dry_run:
            _save_sync_state(
                state,
                status=result.status,
                attempted_at=attempted_at,
                error_kind=error_kind,
                error_message=error_message,
            )
        return result

    if result.watermark_before is not None:
        updated_after = _api_timestamp(result.watermark_before - watermark_overlap)
    else:
        # ``updated_after`` includes both new notes and older notes edited in
        # the initial lookback. A created-only first scan misses the latter.
        updated_after = _api_timestamp(
            attempted_at - timedelta(days=first_sync_lookback_days)
        )

    try:
        metadata_rows = list(
            client.iter_notes(
                updated_after=updated_after,
                # Always finish the cursor. ``max_notes`` limits detail calls
                # below; limiting metadata here repeats page one forever.
                max_notes=None,
            )
        )
    except GranolaError as exc:
        result.status = MeetingNoteSyncState.Status.FAILED
        result.warnings.append(f"Granola sync failed; using Gemini context: {exc}")
        if state is not None and not dry_run:
            _save_sync_state(
                state,
                status=result.status,
                attempted_at=attempted_at,
                error_kind=type(exc).__name__,
                error_message=str(exc),
            )
        return result

    matcher = GranolaNoteMatcher(internal_emails=internal_emails)
    active_ids = {str(value) for value in active_opportunity_ids}
    source_changed_by_id: dict[str, bool] = {}
    metadata_notes_for_dry_run: dict[str, MeetingNote] = {}
    checkpoint_safe = True

    for raw_item in metadata_rows:
        result.metadata_seen += 1
        try:
            item = _validated_metadata(raw_item)
        except GranolaResponseError as exc:
            result.metadata_failures += 1
            note_id = _metadata_note_id(raw_item)
            if note_id:
                if not dry_run:
                    _quarantine_metadata_note(note_id=note_id, raw=raw_item)
                result.warnings.append(
                    f"Granola metadata {note_id} was quarantined for detail retry: {exc}"
                )
            else:
                checkpoint_safe = False
                result.warnings.append(
                    "Granola returned an untrackable metadata row; the metadata "
                    "watermark will not advance."
                )
            continue
        note, needs_detail, source_changed = _metadata_note(item, dry_run=dry_run)
        source_changed_by_id[note.external_id] = source_changed
        if needs_detail:
            if dry_run:
                metadata_notes_for_dry_run[note.external_id] = note
            continue
        result.unchanged += 1
        decision = matcher.decide(note)
        if not dry_run and matcher.needs_apply(note, decision):
            matcher.apply(note, decision)
        _count_decision(result, decision)

    pending_query = MeetingNote.objects.filter(
        source=MeetingNote.Source.GRANOLA,
        detail_status__in={
            MeetingNote.DetailStatus.PENDING,
            MeetingNote.DetailStatus.RETRY,
        },
    ).order_by("updated_at", "id")
    if dry_run:
        pending_by_id = {
            note.external_id: note for note in pending_query
        }
        pending_by_id.update(metadata_notes_for_dry_run)
        pending_notes = list(pending_by_id.values())
        pending_notes.sort(
            key=lambda note: (
                note.updated_at or attempted_at,
                str(note.id),
            )
        )
        pending_before = len(pending_notes)
        if max_notes is not None:
            pending_notes = pending_notes[:max_notes]
    else:
        pending_before = pending_query.count()
        if max_notes is not None:
            pending_query = pending_query[:max_notes]
        pending_notes = list(pending_query)

    global_error: GranolaError | None = None
    consecutive_local_failures = 0
    terminal_or_completed = 0

    for note in pending_notes:
        result.detail_attempts += 1

        try:
            detail = client.get_note(note.external_id)
            _populate_note_from_detail(
                note,
                detail,
                fetched_at=attempted_at,
                reset_transcript=source_changed_by_id.get(
                    note.external_id,
                    False,
                ),
            )
            decision = matcher.decide(note)
            if not dry_run:
                with transaction.atomic():
                    note.save()
                    matcher.apply(note, decision)
            result.details_fetched += 1
            terminal_or_completed += 1
            consecutive_local_failures = 0
            _count_decision(result, decision)
        except GranolaNotFoundError as exc:
            result.unavailable += 1
            terminal_or_completed += 1
            consecutive_local_failures = 0
            if not dry_run:
                note.detail_status = MeetingNote.DetailStatus.UNAVAILABLE
                note.save(update_fields={"detail_status", "updated_at"})
            result.warnings.append(
                f"Granola note {note.external_id} returned 404 and is unavailable; "
                f"using Gemini: {exc}"
            )
            continue
        except GranolaPayloadTooLargeError as exc:
            result.detail_retries += 1
            _mark_note_for_retry(note, dry_run=dry_run)
            result.warnings.append(
                f"Granola note {note.external_id} detail was too large; it remains "
                f"retryable while the transcript endpoint is attempted: {exc}"
            )
            fallback_error = _fetch_transcript_fallback(
                client=client,
                note=note,
                fetched_at=attempted_at,
                dry_run=dry_run,
                result=result,
                force=source_changed_by_id.get(note.external_id, False),
            )
            if fallback_error is not None:
                global_error = fallback_error
                break
            consecutive_local_failures += 1
        except (GranolaRequestError, GranolaResponseError) as exc:
            result.detail_retries += 1
            _mark_note_for_retry(note, dry_run=dry_run)
            consecutive_local_failures += 1
            result.warnings.append(
                f"Granola note {note.external_id} was quarantined for retry; "
                f"later notes will continue: {exc}"
            )
        except (GranolaAuthenticationError, GranolaTransientError) as exc:
            result.detail_retries += 1
            _mark_note_for_retry(note, dry_run=dry_run)
            global_error = exc
            result.warnings.append(
                f"Granola detail refresh stopped on a source-wide failure; "
                f"affected meetings use Gemini: {exc}"
            )
            break
        except GranolaError as exc:
            # A base transport/configuration error cannot safely be classified
            # as note-local. Stop without losing the persisted retry queue.
            result.detail_retries += 1
            _mark_note_for_retry(note, dry_run=dry_run)
            global_error = exc
            result.warnings.append(
                f"Granola detail refresh stopped on an unclassified source "
                f"failure; affected meetings use Gemini: {exc}"
            )
            break

        if consecutive_local_failures >= max_consecutive_detail_failures:
            global_error = GranolaResponseError(
                "Granola detail circuit breaker opened after consecutive "
                "note-local failures."
            )
            result.warnings.append(
                "Granola detail circuit breaker opened; unattempted notes remain "
                "queued ahead of failed notes for the next refresh."
            )
            break

    if global_error is None:
        _fetch_active_transcripts(
            client=client,
            active_ids=active_ids,
            fetched_at=attempted_at,
            dry_run=dry_run,
            result=result,
        )

    if dry_run:
        result.pending_details = max(0, pending_before - terminal_or_completed)
    else:
        result.pending_details = MeetingNote.objects.filter(
            source=MeetingNote.Source.GRANOLA,
            detail_status__in={
                MeetingNote.DetailStatus.PENDING,
                MeetingNote.DetailStatus.RETRY,
            },
        ).count()

    result.source_available = global_error is None
    if (
        global_error is not None
        or result.metadata_failures
        or result.pending_details
        or result.detail_retries
    ):
        result.status = MeetingNoteSyncState.Status.PARTIAL
    else:
        result.status = MeetingNoteSyncState.Status.SUCCESS

    if checkpoint_safe:
        result.watermark_after = attempted_at
    else:
        result.watermark_after = result.watermark_before
    if (
        max_notes is not None
        and result.detail_attempts >= max_notes
        and result.pending_details
    ):
        result.warnings.append(
            "Granola detail budget was exhausted; remaining cached notes will "
            "resume oldest-first on the next refresh."
        )

    if state is not None and not dry_run:
        state_error_kind = ""
        state_error_message = ""
        if global_error is not None:
            state_error_kind = type(global_error).__name__
            state_error_message = str(global_error)
        elif not checkpoint_safe:
            state_error_kind = GranolaResponseError.__name__
            state_error_message = "Granola metadata contained an untrackable row."
        _save_sync_state(
            state,
            status=result.status,
            attempted_at=attempted_at,
            successful_watermark=result.watermark_after,
            mark_success=(result.status == MeetingNoteSyncState.Status.SUCCESS),
            error_kind=state_error_kind,
            error_message=state_error_message,
        )
    return result


def resolve_meeting_context(
    *,
    meeting: Meeting | None = None,
    opportunity: Opportunity | None = None,
    granola_available: bool = True,
) -> ResolvedMeetingContext | None:
    """Select Granola first, then Gemini, from already matched cached notes."""
    if meeting is None and opportunity is None:
        raise ValueError("meeting or opportunity is required")
    notes = MeetingNote.objects.filter(
        match_status=MeetingNote.MatchStatus.MATCHED,
        detail_status=MeetingNote.DetailStatus.COMPLETE,
    )
    scoped_notes = []
    if meeting is not None:
        scoped_notes.append(notes.filter(meeting=meeting))
        if opportunity is not None:
            # An opportunity can have many calls. Unbound opportunity context
            # is relevant, but a note bound to another meeting is not.
            scoped_notes.append(
                notes.filter(meeting__isnull=True, opportunity=opportunity)
            )
    else:
        scoped_notes.append(notes.filter(opportunity=opportunity))
    source_order = (
        (MeetingNote.Source.GRANOLA, MeetingNote.Source.GEMINI)
        if granola_available
        else (MeetingNote.Source.GEMINI,)
    )
    for source in source_order:
        for scope in scoped_notes:
            for note in scope.filter(source=source).order_by(
                "-source_updated_at", "-fetched_at", "-id"
            ):
                content = (
                    note.content
                    or note.summary_markdown
                    or note.summary_text
                    or _transcript_text(note.transcript)
                )
                if not content:
                    continue
                return ResolvedMeetingContext(
                    source=note.source,
                    external_id=note.external_id,
                    meeting_id=note.meeting_id,
                    opportunity_id=(
                        str(note.opportunity_id) if note.opportunity_id else None
                    ),
                    title=note.title,
                    scheduled_start_at=note.scheduled_start_at,
                    content=content,
                    source_updated_at=note.source_updated_at,
                    fetched_at=note.fetched_at,
                )
    return _legacy_gemini_context(meeting=meeting, opportunity=opportunity)


def rematch_cached_granola_notes(*, dry_run: bool = False) -> dict[str, int]:
    """Re-evaluate cached notes after CRM contacts/opportunities change."""
    matcher = GranolaNoteMatcher()
    counts = defaultdict(int)
    notes = MeetingNote.objects.filter(
        source=MeetingNote.Source.GRANOLA,
        detail_status=MeetingNote.DetailStatus.COMPLETE,
    ).order_by("id")
    for note in notes:
        decision = matcher.decide(note)
        counts[decision.status] += 1
        if not dry_run:
            matcher.apply(note, decision)
    return dict(counts)


def _legacy_gemini_context(
    *,
    meeting: Meeting | None,
    opportunity: Opportunity | None,
) -> ResolvedMeetingContext | None:
    """Read the actively maintained Meeting fields until all writers mirror notes."""
    candidates = Meeting.objects.exclude(gemini_notes_raw="")
    selected = None
    if meeting is not None:
        selected = candidates.filter(pk=meeting.pk).first()
    if selected is None and opportunity is not None:
        selected = (
            candidates.filter(opportunity=opportunity)
            .order_by("-start_at", "-id")
            .first()
        )
    if selected is None:
        return None
    return ResolvedMeetingContext(
        source=MeetingNote.Source.GEMINI,
        external_id=(
            selected.gemini_doc_id or f"legacy-meeting:{selected.pk}"
        ),
        meeting_id=selected.pk,
        opportunity_id=(
            str(selected.opportunity_id) if selected.opportunity_id else None
        ),
        title=selected.gemini_doc_title or selected.title,
        scheduled_start_at=selected.start_at,
        content=selected.gemini_notes_raw,
        source_updated_at=selected.gemini_notes_fetched_at,
        fetched_at=selected.gemini_notes_fetched_at,
    )


def _validated_metadata(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GranolaResponseError("Granola list item must be an object.")
    note_id = str(value.get("id") or "")
    if not NOTE_ID_PATTERN.fullmatch(note_id) or value.get("object") != "note":
        raise GranolaResponseError("Granola list item has an invalid note identity.")
    created_at = _parse_timestamp(value.get("created_at"), field="created_at")
    updated_at = _parse_timestamp(value.get("updated_at"), field="updated_at")
    title = value.get("title")
    if title is not None and not isinstance(title, str):
        raise GranolaResponseError("Granola note title must be a string or null.")
    owner = value.get("owner") or {}
    if not isinstance(owner, dict):
        raise GranolaResponseError("Granola note owner must be an object.")
    return {
        "id": note_id,
        "title": title or "",
        "owner_name": str(owner.get("name") or ""),
        "owner_email": _normalize_email(owner.get("email")),
        "created_at": created_at,
        "updated_at": updated_at,
        "raw": dict(value),
    }


def _metadata_note(
    metadata: dict[str, Any],
    *,
    dry_run: bool,
) -> tuple[MeetingNote, bool, bool]:
    existing = MeetingNote.objects.filter(
        source=MeetingNote.Source.GRANOLA,
        external_id=metadata["id"],
    ).first()
    source_changed = (
        existing is not None
        and existing.source_updated_at != metadata["updated_at"]
    )
    needs_detail = (
        existing is None
        or source_changed
        or existing.detail_status in {
            MeetingNote.DetailStatus.PENDING,
            MeetingNote.DetailStatus.RETRY,
        }
    )
    note = existing or MeetingNote(
        source=MeetingNote.Source.GRANOLA,
        external_id=metadata["id"],
    )
    values = {
        "title": metadata["title"][:500],
        "owner_name": metadata["owner_name"][:200],
        "owner_email": metadata["owner_email"],
        "source_created_at": metadata["created_at"],
        "source_updated_at": metadata["updated_at"],
    }
    changed = existing is None or any(
        getattr(note, field_name) != value
        for field_name, value in values.items()
    )
    for field_name, value in values.items():
        setattr(note, field_name, value)
    if existing is None or source_changed:
        if note.detail_status != MeetingNote.DetailStatus.PENDING:
            changed = True
        note.detail_status = MeetingNote.DetailStatus.PENDING
    if not dry_run and changed:
        note.save()
    return note, needs_detail, source_changed


def _metadata_note_id(value: object) -> str:
    if not isinstance(value, dict) or not isinstance(value.get("id"), str):
        return ""
    note_id = value["id"]
    return note_id if NOTE_ID_PATTERN.fullmatch(note_id) else ""


def _quarantine_metadata_note(*, note_id: str, raw: object) -> None:
    note = MeetingNote.objects.filter(
        source=MeetingNote.Source.GRANOLA,
        external_id=note_id,
    ).first()
    if note is None:
        raw_payload = dict(raw) if isinstance(raw, dict) else {}
        title = raw_payload.get("title")
        MeetingNote.objects.create(
            source=MeetingNote.Source.GRANOLA,
            external_id=note_id,
            title=(title[:500] if isinstance(title, str) else ""),
            raw=raw_payload,
            detail_status=MeetingNote.DetailStatus.RETRY,
        )
        return
    if note.detail_status != MeetingNote.DetailStatus.RETRY:
        note.detail_status = MeetingNote.DetailStatus.RETRY
        note.save(update_fields={"detail_status", "updated_at"})


def _mark_note_for_retry(note: MeetingNote, *, dry_run: bool) -> None:
    note.detail_status = MeetingNote.DetailStatus.RETRY
    if not dry_run:
        note.save(update_fields={"detail_status", "updated_at"})


def _populate_note_from_detail(
    note: MeetingNote,
    detail: dict[str, Any],
    *,
    fetched_at: datetime,
    reset_transcript: bool,
) -> None:
    if detail.get("id") != note.external_id or detail.get("object") != "note":
        raise GranolaResponseError("Granola get-note response has an unexpected identity.")
    owner = detail.get("owner") or {}
    calendar = detail.get("calendar_event") or {}
    attendees = detail.get("attendees") or []
    if not isinstance(owner, dict) or not isinstance(calendar, dict):
        raise GranolaResponseError("Granola note owner/calendar_event has an invalid shape.")
    if not isinstance(attendees, list) or not all(
        isinstance(item, dict) for item in attendees
    ):
        raise GranolaResponseError("Granola note attendees must be an array of objects.")
    summary_text = detail.get("summary_text") or ""
    summary_markdown = detail.get("summary_markdown") or ""
    if not isinstance(summary_text, str) or not isinstance(summary_markdown, str):
        raise GranolaResponseError("Granola note summaries must be strings or null.")

    note.title = str(detail.get("title") or "")[:500]
    note.owner_name = str(owner.get("name") or "")[:200]
    note.owner_email = _normalize_email(owner.get("email"))
    note.web_url = str(detail.get("web_url") or "")[:1000]
    note.calendar_event_id = str(calendar.get("calendar_event_id") or "")[:255]
    note.scheduled_start_at = _parse_optional_timestamp(
        calendar.get("scheduled_start_time"),
        field="calendar_event.scheduled_start_time",
    )
    note.scheduled_end_at = _parse_optional_timestamp(
        calendar.get("scheduled_end_time"),
        field="calendar_event.scheduled_end_time",
    )
    note.attendees = attendees
    note.summary_text = summary_text
    note.summary_markdown = summary_markdown
    note.content = summary_markdown or summary_text
    inline_transcript = detail.get("transcript")
    if isinstance(inline_transcript, list):
        note.transcript = inline_transcript
        note.transcript_fetched_at = fetched_at
    elif reset_transcript:
        note.transcript = []
        note.transcript_fetched_at = None
    note.raw = {key: value for key, value in detail.items() if key != "transcript"}
    note.source_created_at = _parse_timestamp(
        detail.get("created_at"), field="created_at"
    )
    note.source_updated_at = _parse_timestamp(
        detail.get("updated_at"), field="updated_at"
    )
    note.fetched_at = fetched_at
    note.detail_status = MeetingNote.DetailStatus.COMPLETE


def _fetch_transcript_fallback(
    *,
    client: GranolaClient,
    note: MeetingNote,
    fetched_at: datetime,
    dry_run: bool,
    result: GranolaSyncResult,
    force: bool,
) -> GranolaError | None:
    if note.transcript_fetched_at is not None and not force:
        return None
    try:
        transcript = client.get_transcript(note.external_id)
    except (GranolaAuthenticationError, GranolaTransientError) as exc:
        result.transcript_failures += 1
        result.warnings.append(
            f"Granola transcript fallback stopped on a source-wide failure: {exc}"
        )
        return exc
    except GranolaError as exc:
        result.transcript_failures += 1
        result.warnings.append(
            f"Granola transcript fallback for {note.external_id} remains "
            f"retryable: {exc}"
        )
        return None
    result.transcripts_fetched += 1
    note.transcript = transcript
    note.transcript_fetched_at = fetched_at
    if not dry_run:
        note.save(update_fields={
            "transcript",
            "transcript_fetched_at",
            "updated_at",
        })
    return None


def _fetch_active_transcripts(
    *,
    client: GranolaClient,
    active_ids: set[str],
    fetched_at: datetime,
    dry_run: bool,
    result: GranolaSyncResult,
) -> None:
    if not active_ids:
        return
    notes = MeetingNote.objects.filter(
        source=MeetingNote.Source.GRANOLA,
        detail_status=MeetingNote.DetailStatus.COMPLETE,
        match_status=MeetingNote.MatchStatus.MATCHED,
        opportunity_id__in=active_ids,
        transcript_fetched_at__isnull=True,
    ).order_by("id")
    for note in notes:
        try:
            transcript = client.get_transcript(note.external_id)
        except GranolaError as exc:
            result.transcript_failures += 1
            result.warnings.append(
                f"Granola transcript {note.external_id} was unavailable; summary retained: {exc}"
            )
            continue
        result.transcripts_fetched += 1
        if not dry_run:
            note.transcript = transcript
            note.transcript_fetched_at = fetched_at
            note.save(update_fields={
                "transcript", "transcript_fetched_at", "updated_at",
            })


def _save_sync_state(
    state: MeetingNoteSyncState,
    *,
    status: str,
    attempted_at: datetime,
    successful_watermark: datetime | None = None,
    mark_success: bool = False,
    error_kind: str = "",
    error_message: str = "",
) -> None:
    state.status = status
    state.last_attempt_at = attempted_at
    if successful_watermark is not None:
        state.successful_watermark = successful_watermark
    if mark_success:
        state.last_success_at = attempted_at
    state.last_error_kind = error_kind[:64]
    state.last_error_message = error_message[:500]
    state.save()


def _count_decision(result: GranolaSyncResult, decision: MatchDecision) -> None:
    if decision.status == MeetingNote.MatchStatus.MATCHED:
        result.matched += 1
        result.matched_by_method[decision.method] = (
            result.matched_by_method.get(decision.method, 0) + 1
        )
    elif decision.status == MeetingNote.MatchStatus.AMBIGUOUS:
        result.ambiguous += 1
    else:
        result.unmatched += 1


def _structured_note(
    note: MeetingNote,
    *,
    internal_emails: set[str],
) -> _StructuredNote:
    raw = note.raw if isinstance(note.raw, dict) else {}
    calendar = raw.get("calendar_event") or {}
    attendees = list(note.attendees or [])
    invitees = (calendar.get("invitees") or []) if isinstance(calendar, dict) else []
    owner_email = _normalize_email(note.owner_email)
    organiser_email = _normalize_email(
        calendar.get("organiser") if isinstance(calendar, dict) else ""
    )
    excluded = set(internal_emails)
    excluded.update(value for value in (owner_email, organiser_email) if value)
    excluded_names = {_normalize_identity(note.owner_name)}
    excluded_names.discard("")

    attendee_by_email: dict[str, dict[str, str]] = {}
    names: set[str] = set()
    emails: set[str] = set()
    for item in [*attendees, *invitees]:
        if not isinstance(item, dict):
            continue
        email = _normalize_email(item.get("email"))
        name = str(item.get("name") or item.get("displayName") or "").strip()
        normalized_name = _normalize_identity(name)
        if (
            _is_internal_email(email, excluded)
            or normalized_name in excluded_names
        ):
            continue
        if email:
            emails.add(email)
            attendee_by_email[email] = {
                "email": email,
                "name": name,
                "response_status": str(
                    item.get("responseStatus") or item.get("response_status") or ""
                ),
            }
        if normalized_name:
            names.add(normalized_name)

    event_title = ""
    if isinstance(calendar, dict):
        event_title = str(calendar.get("event_title") or "")
    title = event_title or note.title
    return _StructuredNote(
        external_emails=tuple(sorted(emails)),
        external_attendee_names=tuple(sorted(names)),
        title=title,
        normalized_title=_normalize_identity(title),
        calendar_event_id=(note.calendar_event_id or "").strip(),
        scheduled_start_at=note.scheduled_start_at,
        attendee_by_email=attendee_by_email,
    )


def _evidence(
    structured: _StructuredNote,
    *,
    reason: str,
    leads: Iterable[Lead] = (),
    opportunities: Iterable[Opportunity] = (),
    meetings: Iterable[Meeting] = (),
) -> dict[str, Any]:
    return {
        "reason": reason,
        "attendee_emails": list(structured.external_emails),
        "attendee_identities": list(structured.external_attendee_names),
        "calendar_event_id": structured.calendar_event_id,
        "scheduled_start_at": (
            structured.scheduled_start_at.isoformat()
            if structured.scheduled_start_at
            else None
        ),
        "normalized_title": structured.normalized_title,
        "lead_ids": [lead.id for lead in leads],
        "opportunity_ids": [str(opportunity.id) for opportunity in opportunities],
        "meeting_ids": [meeting.id for meeting in meetings],
    }


def _participant_match_method(method: str) -> str:
    if method in MeetingParticipant.MatchMethod.values:
        return method
    if method == MeetingNote.MatchMethod.EXISTING_EVENT_ID:
        return MeetingParticipant.MatchMethod.MANUAL
    return MeetingParticipant.MatchMethod.MANUAL


def _meeting_event_ids(meeting: Meeting) -> set[str]:
    out = {(meeting.external_id or "").strip()}
    raw = meeting.raw if isinstance(meeting.raw, dict) else {}
    for key in ("id", "iCalUID", "calendar_event_id"):
        value = str(raw.get(key) or "").strip()
        if value:
            out.add(value)
    out.discard("")
    return out


def _default_internal_emails() -> set[str]:
    emails = {
        _normalize_email(value)
        for account in GMAIL_ACCOUNTS.values()
        for value in account.send_as_aliases
    }
    emails.update(
        _normalize_email(mapping.get("send_as"))
        for mapping in GMAIL_OPERATOR_MAPPING.values()
    )
    emails.discard("")
    return emails


def _unique_leads(values: Iterable[Lead]) -> tuple[Lead, ...]:
    return tuple({lead.id: lead for lead in values}.values())


def _normalize_email(value: Any) -> str:
    return str(value or "").strip().casefold()


def _normalize_domain(value: Any) -> str:
    return str(value or "").strip().casefold().lstrip("@")


def _email_domain(value: str) -> str:
    return value.rsplit("@", 1)[1] if "@" in value else ""


def _is_internal_email(email: str, explicit: set[str]) -> bool:
    if not email:
        return False
    return email in explicit or _email_domain(email) in INTERNAL_EMAIL_DOMAINS


def _normalize_identity(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[^a-z0-9]+", " ", normalized).strip()


def _contains_exact_phrase(haystack: str, needle: str) -> bool:
    if not haystack or not needle:
        return False
    return bool(re.search(rf"(?:^| ){re.escape(needle)}(?: |$)", haystack))


def _parse_timestamp(value: Any, *, field: str) -> datetime:
    parsed = _parse_optional_timestamp(value, field=field)
    if parsed is None:
        raise GranolaResponseError(f"Granola note {field} is required.")
    return parsed


def _parse_optional_timestamp(value: Any, *, field: str) -> datetime | None:
    if value in {None, ""}:
        return None
    if not isinstance(value, str):
        raise GranolaResponseError(f"Granola note {field} must be an ISO timestamp.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GranolaResponseError(
            f"Granola note {field} must be an ISO timestamp."
        ) from exc
    if parsed.tzinfo is None:
        raise GranolaResponseError(f"Granola note {field} must include a timezone.")
    return parsed


def _api_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("Granola sync timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _transcript_text(value: Any) -> str:
    if not isinstance(value, list):
        return ""
    lines = []
    for item in value:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        speaker = item.get("speaker") or {}
        name = ""
        if isinstance(speaker, dict):
            name = str(
                speaker.get("name")
                or speaker.get("diarization_label")
                or speaker.get("attribution")
                or ""
            ).strip()
        lines.append(f"{name}: {text}" if name else text)
    return "\n".join(lines)
