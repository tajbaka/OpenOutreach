"""Gmail-backed data-sync for followup context.

This is the Python/API replacement for the Gmail-accessible portion of the
old Claude data-sync workflow. It persists:
  - normal prospect email threads into crm.Message(source=gmail)
  - Gemini / Google Meet note emails into crm.Meeting.gemini_notes_raw

Calendar + Drive APIs can still provide richer matching later, but Gmail note
emails already contain useful note text and a Drive link, so this gives the
followup workflow the same DB surfaces without requiring MCP access.
"""
from __future__ import annotations

import base64
import html
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Iterable

from django.db import transaction
from django.utils import timezone as dj_tz

from crm.models import Lead, Meeting
from gmail.client import GmailClient
from linkedin.enums import ProfileState
from linkedin.notifications.calendar_events import persist_gemini_notes
from linkedin.notifications.gmail_threads import persist_gmail_threads


NOTE_QUERY = (
    "from:(gemini-notes@google.com OR meetings-noreply@google.com) "
    "subject:Notes newer_than:{days}d"
)

ACTIVE_STATES = (
    ProfileState.QUALIFIED,
    ProfileState.READY_TO_CONNECT,
    ProfileState.PENDING,
    ProfileState.CONNECTED,
    ProfileState.COMPLETED,
)


@dataclass
class GmailContextSyncResult:
    leads_considered: int = 0
    leads_with_email_threads: int = 0
    gmail_threads_fetched: int = 0
    gmail_messages_created: int = 0
    note_emails_seen: int = 0
    note_emails_matched: int = 0
    note_emails_created_meetings: int = 0
    note_emails_updated_meetings: int = 0
    note_emails_unchanged: int = 0
    note_emails_unmatched: int = 0
    unmatched_notes: list[dict] = field(default_factory=list)


def self_emails_for_client(client: GmailClient) -> set[str]:
    """Resolve the real mailbox + Send-As aliases from the connected account."""
    service = client._service
    profile = service.users().getProfile(userId="me").execute()
    emails = {(profile.get("emailAddress") or "").strip().lower()}
    for alias in client.send_as_aliases():
        emails.add(alias.strip().lower())
    emails.discard("")
    return emails


def candidate_leads(*, campaign_id: int | None = None, all_leads: bool = False):
    qs = Lead.objects.filter(disqualified=False).exclude(email="")
    if campaign_id is not None:
        qs = qs.filter(deal__campaign_id=campaign_id)
    if not all_leads:
        qs = qs.filter(deal__state__in=ACTIVE_STATES)
    return qs.distinct().order_by("id")


def sync_gmail_threads(
    *,
    client: GmailClient,
    leads: Iterable[Lead],
    self_emails: Iterable[str],
    since_days: int,
    dry_run: bool,
) -> GmailContextSyncResult:
    result = GmailContextSyncResult()
    service = client._service
    seen_thread_ids: set[str] = set()

    for lead in leads:
        result.leads_considered += 1
        email = (lead.email or "").strip()
        if not email:
            continue

        query = f"{email} newer_than:{int(since_days)}d"
        thread_ids = sorted({
            item["threadId"]
            for item in _list_messages(service, query=query)
            if item.get("threadId")
        })
        thread_ids = [tid for tid in thread_ids if tid not in seen_thread_ids]
        if not thread_ids:
            continue

        result.leads_with_email_threads += 1
        seen_thread_ids.update(thread_ids)
        result.gmail_threads_fetched += len(thread_ids)
        threads = [_gmail_thread_payload(service, thread_id) for thread_id in thread_ids]
        if not dry_run:
            result.gmail_messages_created += persist_gmail_threads(
                lead=lead,
                threads=threads,
                self_emails=self_emails,
            )
    return result


def sync_gmail_note_emails(
    *,
    client: GmailClient,
    leads: Iterable[Lead],
    since_days: int,
    dry_run: bool,
    create_missing_meetings: bool = True,
) -> GmailContextSyncResult:
    result = GmailContextSyncResult()
    service = client._service
    lead_list = list(leads)
    query = NOTE_QUERY.format(days=int(since_days))

    for item in _list_messages(service, query=query):
        message_id = item.get("id")
        if not message_id:
            continue
        result.note_emails_seen += 1
        msg = _gmail_message_payload(service, message_id)
        note = _note_from_message(msg)
        if note is None:
            continue

        meeting, created = _match_or_build_meeting_for_note(
            note=note,
            leads=lead_list,
            create_missing_meetings=create_missing_meetings,
        )
        if meeting is None:
            result.note_emails_unmatched += 1
            result.unmatched_notes.append({
                "subject": note.subject,
                "date": note.sent_at.isoformat(),
                "reason": "no unique CRM lead/meeting match",
            })
            continue

        result.note_emails_matched += 1
        if dry_run:
            if created:
                result.note_emails_created_meetings += 1
            else:
                result.note_emails_updated_meetings += 1
            continue

        with transaction.atomic():
            if created:
                meeting.save()
                result.note_emails_created_meetings += 1
            changed = persist_gemini_notes(
                meeting=meeting,
                doc_id=note.drive_doc_id or f"gmail:{client.account_key}:{note.message_id}",
                doc_title=note.title,
                raw_text=note.body,
            )
        if changed:
            if not created:
                result.note_emails_updated_meetings += 1
        else:
            result.note_emails_unchanged += 1
    return result


@dataclass(frozen=True)
class GmailNote:
    message_id: str
    thread_id: str
    subject: str
    sender: str
    sent_at: datetime
    title: str
    body: str
    drive_doc_id: str


def _list_messages(service, *, query: str):
    page_token = None
    while True:
        kwargs = {"userId": "me", "q": query, "maxResults": 500}
        if page_token:
            kwargs["pageToken"] = page_token
        resp = service.users().messages().list(**kwargs).execute()
        yield from resp.get("messages", [])
        page_token = resp.get("nextPageToken")
        if not page_token:
            break


def _gmail_thread_payload(service, thread_id: str) -> dict:
    thread = service.users().threads().get(
        userId="me",
        id=thread_id,
        format="full",
    ).execute()
    return {
        "id": thread.get("id") or thread_id,
        "messages": [_adapt_gmail_message(m) for m in thread.get("messages", [])],
    }


def _gmail_message_payload(service, message_id: str) -> dict:
    return service.users().messages().get(
        userId="me",
        id=message_id,
        format="full",
    ).execute()


def _adapt_gmail_message(msg: dict) -> dict:
    return {
        "id": msg.get("id", ""),
        "threadId": msg.get("threadId", ""),
        "headers": (msg.get("payload") or {}).get("headers", []),
        "snippet": _message_text(msg) or msg.get("snippet", ""),
        "internalDate": msg.get("internalDate", ""),
        "labelIds": msg.get("labelIds", []),
    }


def _note_from_message(msg: dict) -> GmailNote | None:
    headers = _headers(msg)
    subject = headers.get("subject", "")
    sender = headers.get("from", "")
    title = _note_title(subject)
    if not title:
        return None
    sent_at = _message_datetime(msg, headers)
    body = _message_text(msg) or msg.get("snippet", "")
    body = _clean_text(body)
    if not body:
        return None
    return GmailNote(
        message_id=msg.get("id", ""),
        thread_id=msg.get("threadId", ""),
        subject=subject,
        sender=sender,
        sent_at=sent_at,
        title=title,
        body=body,
        drive_doc_id=_drive_doc_id(body),
    )


def _match_or_build_meeting_for_note(
    *,
    note: GmailNote,
    leads: list[Lead],
    create_missing_meetings: bool,
) -> tuple[Meeting | None, bool]:
    existing = _find_existing_meeting(note)
    if existing is not None:
        return existing, False

    if not create_missing_meetings:
        return None, False

    lead = _unique_lead_for_note_title(note.title, leads)
    if lead is None:
        return None, False

    meeting = Meeting(
        source=Meeting.Source.GOOGLE_CALENDAR,
        external_id=f"gmail-note:{note.message_id}",
        lead=lead,
        start_at=note.sent_at,
        end_at=None,
        title=note.title[:500],
        description="",
        attendees=[],
        raw={
            "source": "gmail_note_email",
            "message_id": note.message_id,
            "thread_id": note.thread_id,
            "subject": note.subject,
            "from": note.sender,
        },
    )
    return meeting, True


def _find_existing_meeting(note: GmailNote) -> Meeting | None:
    normalized_note = _norm(note.title)
    start = note.sent_at - timedelta(days=2)
    end = note.sent_at + timedelta(days=1)
    candidates = Meeting.objects.filter(start_at__gte=start, start_at__lte=end)
    exact = []
    contains = []
    for meeting in candidates:
        title_norm = _norm(meeting.title)
        doc_norm = _norm(meeting.gemini_doc_title)
        if not title_norm and not doc_norm:
            continue
        if normalized_note in {title_norm, doc_norm}:
            exact.append(meeting)
        elif normalized_note and (
            normalized_note in title_norm
            or title_norm in normalized_note
            or normalized_note in doc_norm
        ):
            contains.append(meeting)
    if len(exact) == 1:
        return exact[0]
    if not exact and len(contains) == 1:
        return contains[0]
    return None


def _unique_lead_for_note_title(title: str, leads: list[Lead]) -> Lead | None:
    title_norm = _norm(title)
    scored: list[tuple[int, Lead]] = []
    for lead in leads:
        first = _norm(lead.first_name)
        last = _norm(lead.last_name)
        company = _norm(lead.company_name)
        full = _norm(f"{lead.first_name} {lead.last_name}")
        score = 0
        if full and full in title_norm:
            score += 5
        if first and last and first in title_norm and last in title_norm:
            score += 4
        elif last and last in title_norm:
            score += 2
        if company and company in title_norm:
            score += 2
        if first and first in title_norm:
            score += 1
        if score >= 4:
            scored.append((score, lead))

    if not scored:
        return None
    scored.sort(key=lambda item: item[0], reverse=True)
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return None
    return scored[0][1]


def _headers(msg: dict) -> dict[str, str]:
    out = {}
    for h in (msg.get("payload") or {}).get("headers", []):
        name = (h.get("name") or "").strip().lower()
        if name:
            out[name] = h.get("value") or ""
    return out


def _message_datetime(msg: dict, headers: dict[str, str]) -> datetime:
    internal = msg.get("internalDate")
    if internal:
        try:
            return datetime.fromtimestamp(int(internal) / 1000, tz=timezone.utc)
        except (TypeError, ValueError):
            pass
    raw_date = headers.get("date")
    if raw_date:
        try:
            dt = parsedate_to_datetime(raw_date)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            pass
    return dj_tz.now()


def _message_text(msg: dict) -> str:
    payload = msg.get("payload") or {}
    plain = []
    html_parts = []
    for part in _iter_parts(payload):
        mime_type = part.get("mimeType", "")
        data = ((part.get("body") or {}).get("data") or "").strip()
        if not data:
            continue
        decoded = _decode_body(data)
        if mime_type == "text/plain":
            plain.append(decoded)
        elif mime_type == "text/html":
            html_parts.append(_html_to_text(decoded))
    if plain:
        return _clean_text("\n".join(plain))
    if html_parts:
        return _clean_text("\n".join(html_parts))
    return ""


def _iter_parts(payload: dict):
    yield payload
    for part in payload.get("parts", []) or []:
        yield from _iter_parts(part)


def _decode_body(data: str) -> str:
    padded = data + ("=" * (-len(data) % 4))
    return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8", "replace")


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_QUOTE_RE = re.compile(r"[“\"]([^”\"]+)[”\"]")
_NOTES_PREFIX_RE = re.compile(r"^(?:problem with the notes:\s*)?notes:\s*", re.I)
_TRAILING_DATE_RE = re.compile(
    r"\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*"
    r"\s+\d{1,2},\s+\d{4}.*$",
    re.I,
)
_DOC_RE = re.compile(r"https://docs\.google\.com/document/d/([a-zA-Z0-9_-]+)")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def _html_to_text(raw: str) -> str:
    no_tags = _TAG_RE.sub(" ", raw)
    return html.unescape(no_tags)


def _clean_text(raw: str) -> str:
    return _WS_RE.sub(" ", raw or "").strip()


def _note_title(subject: str) -> str:
    subject = (subject or "").strip()
    quoted = _QUOTE_RE.search(subject)
    if quoted:
        return _clean_text(quoted.group(1))
    title = _NOTES_PREFIX_RE.sub("", subject)
    title = _TRAILING_DATE_RE.sub("", title)
    return _clean_text(title)


def _drive_doc_id(body: str) -> str:
    match = _DOC_RE.search(body or "")
    return match.group(1) if match else ""


def _norm(value: str) -> str:
    return _NON_ALNUM_RE.sub(" ", (value or "").lower()).strip()


def combine_results(*results: GmailContextSyncResult) -> GmailContextSyncResult:
    combined = GmailContextSyncResult()
    for result in results:
        combined.leads_considered += result.leads_considered
        combined.leads_with_email_threads += result.leads_with_email_threads
        combined.gmail_threads_fetched += result.gmail_threads_fetched
        combined.gmail_messages_created += result.gmail_messages_created
        combined.note_emails_seen += result.note_emails_seen
        combined.note_emails_matched += result.note_emails_matched
        combined.note_emails_created_meetings += result.note_emails_created_meetings
        combined.note_emails_updated_meetings += result.note_emails_updated_meetings
        combined.note_emails_unchanged += result.note_emails_unchanged
        combined.note_emails_unmatched += result.note_emails_unmatched
        combined.unmatched_notes.extend(result.unmatched_notes)
    return combined
