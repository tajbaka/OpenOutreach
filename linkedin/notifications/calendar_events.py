"""Calendar event + Drive Gemini note persistence for the data-sync workflow.

Mirrors `gmail_threads.py` in shape: Claude Code calls the Google Calendar
MCP from inside a session, gets back a list of event payloads, hands them
to `persist_calendar_events`. The helper does the DB upsert. Drive Gemini
docs are downloaded by Claude and handed to `persist_gemini_notes` in a
separate call.

Why this isn't part of `gmail_threads.py`: calendar events live in
`crm.Meeting`, not `crm.Message`. The shape differences (multi-attendee,
no direction, start/end times, attached Gemini notes) made it cleaner to
break out into its own table — see `crm/models/meeting.py` docstring.

Population owner: the data-sync workflow (see `docs/data-sync-workflow.md`
Phase 1+ Calendar / Phase 4 Drive Gemini sections). Consumers: the
followup workflow's Phase 1 row builder reads `lead.meetings` for Met
cohort drafts and for the `Days since` anchor on Met rows.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Iterable

from django.db import transaction
from django.utils import timezone as dj_tz

from crm.models import Meeting

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Persistence — Calendar events
# ---------------------------------------------------------------------------


def persist_calendar_events(
    *,
    lead,
    events: list[dict],
) -> int:
    """Upsert calendar events for a single Lead into `crm.Meeting`.

    `events` is the raw response shape produced by Google Calendar MCP
    (list of event dicts). We're tolerant of the two start/end shapes the
    API uses — `dateTime` (timed events) and `date` (all-day events).

    Returns the number of NEW Meeting rows created. Re-running with the
    same payload is a no-op (idempotent on `(source, external_id)`).
    Existing rows are left untouched — we don't try to track edits to
    title/description because that would race with operator edits on the
    Sheet side and the load-bearing field (`start_at`) is immutable in
    practice.
    """
    created = 0
    with transaction.atomic():
        for event in events or []:
            external_id = (event.get("id") or "").strip()
            if not external_id:
                continue

            start_at = _extract_start(event)
            if start_at is None:
                logger.debug(
                    "calendar_events: skipping event %s — no parseable start",
                    external_id,
                )
                continue

            _, was_created = Meeting.objects.get_or_create(
                source=Meeting.Source.GOOGLE_CALENDAR,
                external_id=external_id,
                defaults={
                    "lead": lead,
                    "start_at": start_at,
                    "end_at": _extract_end(event),
                    "title": (event.get("summary") or "")[:500],
                    "description": (event.get("description") or "")[:5000],
                    "attendees": _slim_attendees(event.get("attendees") or []),
                    "raw": _stripped_event(event),
                },
            )
            if was_created:
                created += 1
    return created


# ---------------------------------------------------------------------------
# Persistence — Drive Gemini notes attached to a meeting
# ---------------------------------------------------------------------------


def persist_gemini_notes(
    *,
    meeting: Meeting,
    doc_id: str,
    doc_title: str,
    raw_text: str,
) -> bool:
    """Attach raw Gemini doc content to an existing Meeting row.

    Caller is responsible for matching the Drive doc to the Meeting (via
    title + date heuristic per the data-sync workflow). This helper just
    persists what's been matched. Re-calling overwrites in case Gemini
    regenerated the doc or the operator manually edited it — these notes
    aren't append-only.

    Returns True if anything actually changed, False if the existing row
    already has identical content.
    """
    new_doc_id = (doc_id or "").strip()
    new_title = (doc_title or "")[:500]
    new_raw = raw_text or ""

    if (
        meeting.gemini_doc_id == new_doc_id
        and meeting.gemini_doc_title == new_title
        and meeting.gemini_notes_raw == new_raw
    ):
        return False

    meeting.gemini_doc_id = new_doc_id
    meeting.gemini_doc_title = new_title
    meeting.gemini_notes_raw = new_raw
    meeting.gemini_notes_fetched_at = dj_tz.now()
    meeting.save(update_fields=[
        "gemini_doc_id", "gemini_doc_title",
        "gemini_notes_raw", "gemini_notes_fetched_at",
        "update_date",
    ])
    return True


# ---------------------------------------------------------------------------
# Read-side helpers — used by the followup workflow's row builder
# ---------------------------------------------------------------------------


def latest_meeting_for(lead) -> Meeting | None:
    """Return the most recent past Meeting for a lead, or None.

    "Past" here = `start_at` ≤ now. We don't want upcoming meetings to set
    the `Days since` anchor — those belong in the Scheduling cohort. For
    the Met cohort the meaningful timestamp is "when did we last actually
    meet", not "when is the next meeting".
    """
    return (
        lead.meetings
        .filter(start_at__lte=dj_tz.now())
        .order_by("-start_at")
        .first()
    )


# ---------------------------------------------------------------------------
# Payload-shape adapters — tolerant of MCP / Calendar API variation
# ---------------------------------------------------------------------------


def _extract_start(event: dict) -> datetime | None:
    return _extract_timestamp(event.get("start") or {}) or _extract_timestamp(
        {"dateTime": event.get("start_time") or event.get("startTime")}
    )


def _extract_end(event: dict) -> datetime | None:
    return _extract_timestamp(event.get("end") or {}) or _extract_timestamp(
        {"dateTime": event.get("end_time") or event.get("endTime")}
    )


def _extract_timestamp(start_or_end: dict) -> datetime | None:
    """Google Calendar uses `{"dateTime": "..."}` for timed events and
    `{"date": "YYYY-MM-DD"}` for all-day events. Either form yields a
    timezone-aware datetime; falls back to None when we can't parse."""
    dt_str = start_or_end.get("dateTime") or start_or_end.get("date_time")
    if dt_str:
        try:
            s = str(dt_str).replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            pass

    date_str = start_or_end.get("date")
    if date_str:
        try:
            d = datetime.fromisoformat(str(date_str))
            return d.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            pass

    # RFC2822-formatted date as a last-resort fallback (some MCP wrappers
    # do this for legacy reasons)
    rfc = start_or_end.get("rfc2822")
    if rfc:
        try:
            dt = parsedate_to_datetime(str(rfc))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            pass

    return None


def _slim_attendees(attendees: Iterable[dict]) -> list[dict]:
    """Keep just the fields the drafter / classifier might care about.
    Drops the noisy `additionalGuests`, `organizer`, etc. fields the
    Calendar API returns."""
    out = []
    for a in attendees or []:
        if not isinstance(a, dict):
            continue
        out.append({
            "email": (a.get("email") or "").strip().lower(),
            "name": a.get("displayName") or a.get("name") or "",
            "responseStatus": a.get("responseStatus") or "",
            "self": bool(a.get("self")),
        })
    return out


_KEEP_EVENT_FIELDS = (
    "id", "htmlLink", "status", "summary", "description",
    "start", "end", "attendees", "creator", "organizer",
    "recurringEventId", "originalStartTime", "hangoutLink",
)


def _stripped_event(event: dict) -> dict:
    """Trim event payload before persisting — full responses have iCalUID,
    extended properties, sequence numbers, attachments-by-reference etc.
    that we don't need. Keep just the fields useful for forensic replay."""
    return {k: event[k] for k in _KEEP_EVENT_FIELDS if k in event}
