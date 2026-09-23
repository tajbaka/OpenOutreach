"""Rebuild ``People`` from the companies in manual ``Active Accounts``.

Active Accounts is the account boundary.  A Lead is published only when it is
inside that exact company/account scope and has relationship evidence: human
inbound Gmail, calendar/Granola participation, an exact Main point of contact
name, or a deliberately curated OpportunityContact role.  Cold prospect rows
do not qualify merely because their company name matches.

People is a derived view except for Notes, Priority, and unknown operator
columns.  Those values are carried forward by canonical LinkedIn URL, with a
unique email fallback for email-first contacts.  AI Notes, Lead ID, Stage, and
Outreach status are intentionally absent from the published schema.  Company
cohorts are ordered by their latest Gmail/meeting contact, while people inside
each cohort are ordered by operator priority, explicit opportunity role,
reviewed role/title relevance, and then their latest Gmail/meeting contact.
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from django.db.models import Q
from django.utils import timezone

from linkedin.exceptions import SheetsError
from linkedin.notifications import manual_active_accounts as active_accounts


TAB_NAME = "People"

COL_NAME = "Name"
COL_FIRST_NAME = "First name"
COL_LAST_NAME = "Last name"
COL_COMPANY = "Company"
COL_TITLE = "Title"
COL_LINKEDIN_URL = "LinkedIn URL"
COL_EMAILS = "Email addresses"
COL_PRIORITY = "Priority"
COL_PRIMARY_LOCATION = "Primary location"
COL_NOTES = "Notes"
COL_CREATED_AT = "Created at"
COL_LAST_SYNCED = "Last synced"
COL_ROLE_TAG = "Role Tag"

HEADERS = (
    COL_NAME,
    COL_FIRST_NAME,
    COL_LAST_NAME,
    COL_COMPANY,
    COL_TITLE,
    COL_LINKEDIN_URL,
    COL_EMAILS,
    COL_PRIORITY,
    COL_PRIMARY_LOCATION,
    COL_NOTES,
    COL_CREATED_AT,
    COL_LAST_SYNCED,
    COL_ROLE_TAG,
)
REMOVED_HEADERS = frozenset({"AI Notes", "Lead ID", "Outreach status", "Stage"})
MANUAL_HEADERS = frozenset({COL_NOTES, COL_PRIORITY})

_SORT_LATEST_CONTACT = "__latest_contact"
_SORT_RELEVANCE = "__relevance"
_SORT_COMPANY_KEY = "__company_key"
_COHORT_FILLS = (
    {"red": 1.0, "green": 1.0, "blue": 1.0},
    {"red": 0.94, "green": 0.96, "blue": 0.99},
)
_COHORT_BORDER = {"red": 0.55, "green": 0.61, "blue": 0.70}

_EXECUTIVE_ROLE_TAGS = frozenset({
    "Founder/CEO",
    "CFO/Finance",
    "COO/Operations",
    "CRO/Revenue",
    "Product Executive",
    "Technology/Engineering Executive",
    "CIO/Internal IT Executive",
    "Security/Trust Executive",
    "Compliance/Risk/Privacy Executive",
    "Federal/Public Sector Executive",
})
_LEADER_ROLE_TAGS = frozenset({
    "Product/Engineering N-1",
    "Security/Compliance N-1",
    "FedRAMP Owner/Operator",
    "GRC Manager/Lead",
    "Field CTO/CISO/Technical Evangelist",
})


@dataclass(frozen=True)
class AccountScope:
    company_keys: frozenset[str]
    company_display: dict[str, str]
    account_ids: frozenset[str]
    account_company: dict[str, str]
    main_contacts: dict[str, frozenset[str]]
    rows: int


def _fingerprint(values: list[list[Any]]) -> str:
    payload = json.dumps(values, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _material_rows(values: list[list[Any]]) -> list[list[str]]:
    rows = [[str(cell or "") for cell in row] for row in values]
    while rows and not any(cell != "" for cell in rows[-1]):
        rows.pop()
    return rows


def _normalized_person_name(value: str) -> str:
    return " ".join(str(value or "").split()).casefold()


def _main_contact_names(value: str) -> frozenset[str]:
    parts = str(value or "").replace("\r", "\n").replace("\n", ";").split(";")
    return frozenset(name for part in parts if (name := _normalized_person_name(part)))


def read_account_scope(values: list[list[Any]]) -> AccountScope:
    """Parse an exact Active Accounts inclusion scope without fuzzy matching."""
    from crm.models import Account
    from crm.models.sales import normalize_account_name

    rows = _material_rows(values)
    if not rows:
        raise SheetsError("Active Accounts is empty")
    headers = tuple(cell.strip() for cell in rows[0])
    if len(headers) != len(set(headers)):
        raise SheetsError("Active Accounts contains duplicate headings")
    missing = [header for header in active_accounts.HEADERS if header not in headers]
    if missing:
        raise SheetsError(
            "Active Accounts is missing manual-ledger heading(s): " + ", ".join(missing)
        )
    indexes = {header: headers.index(header) for header in active_accounts.HEADERS}

    parsed: list[tuple[int, str, str, frozenset[str]]] = []
    stable_ids: set[str] = set()
    company_keys: set[str] = set()
    company_display: dict[str, str] = {}
    main_contacts: dict[str, set[str]] = defaultdict(set)
    for row_number, row in enumerate(rows[1:], start=2):
        cells = {
            header: str(row[index] if index < len(row) else "").strip()
            for header, index in indexes.items()
        }
        if not any(cells.values()):
            continue
        company = cells[active_accounts.COL_COMPANY]
        company_key = normalize_account_name(company)
        raw_account_id = cells[active_accounts.COL_ACCOUNT_ID]
        account_id = ""
        if raw_account_id:
            try:
                account_id = str(UUID(raw_account_id))
            except (TypeError, ValueError, AttributeError) as exc:
                raise SheetsError(
                    f"Active Accounts row {row_number} has an invalid Account ID"
                ) from exc
            if account_id in stable_ids:
                raise SheetsError("Active Accounts contains duplicate Account IDs")
            stable_ids.add(account_id)
        if not company_key and not account_id:
            raise SheetsError(
                f"Active Accounts row {row_number} needs Company or Account ID"
            )
        if company_key:
            company_keys.add(company_key)
            company_display.setdefault(company_key, company)
        parsed.append((
            row_number,
            account_id,
            company_key,
            _main_contact_names(cells[active_accounts.COL_MAIN_POINT_OF_CONTACT]),
        ))

    accounts = {
        str(account.id): account
        for account in Account.objects.filter(pk__in=stable_ids)
    }
    account_company: dict[str, str] = {}
    for row_number, account_id, company_key, contact_names in parsed:
        if not account_id:
            main_contacts[company_key].update(contact_names)
            continue
        account = accounts.get(account_id)
        if account is None:
            raise SheetsError(
                f"Active Accounts row {row_number} references an unknown Account ID"
            )
        if company_key and company_key != account.normalized_name:
            raise SheetsError(
                f"Active Accounts row {row_number} has a Company/Account ID mismatch"
            )
        company_key = account.normalized_name
        account_company[account_id] = company_key
        company_keys.add(company_key)
        company_display.setdefault(account.normalized_name, account.name)
        main_contacts[company_key].update(contact_names)

    return AccountScope(
        company_keys=frozenset(company_keys),
        company_display=company_display,
        account_ids=frozenset(stable_ids),
        account_company=account_company,
        main_contacts={key: frozenset(names) for key, names in main_contacts.items()},
        rows=len(parsed),
    )


def _profile_fields(lead) -> tuple[str, str]:
    try:
        profile = json.loads(lead.description or "{}")
    except (TypeError, json.JSONDecodeError):
        return "", ""
    if not isinstance(profile, dict):
        return "", ""
    positions = profile.get("positions") or []
    current = positions[0] if positions and isinstance(positions[0], dict) else {}
    title = str(current.get("title") or profile.get("headline") or "").strip()
    location = str(profile.get("location_name") or "").strip()
    return title, location


def _email_values(value: str) -> list[str]:
    return [part.strip() for part in str(value or "").replace(",", "\n").splitlines() if part.strip()]


def _identity(linkedin_url: str, emails: str) -> tuple[str, str] | None:
    from linkedin.notifications.sheets import canonical_linkedin_url

    url = canonical_linkedin_url(linkedin_url)
    if url:
        return "linkedin", url.casefold()
    values = _email_values(emails)
    if len(values) == 1:
        return "email", values[0].casefold()
    return None


def _latest(current, candidate):
    if candidate is None:
        return current
    if current is None or candidate > current:
        return candidate
    return current


def _priority_rank(value: str) -> int:
    normalized = " ".join(str(value or "").split()).casefold()
    if normalized in {"urgent", "highest", "high", "p0", "p1", "1"}:
        return 3
    if normalized in {"medium", "normal", "p2", "2"}:
        return 2
    if normalized in {"low", "p3", "3"}:
        return 1
    return 0


def _role_rank(role_tag: str, title: str) -> int:
    """Rank closing relevance from reviewed role metadata, then a narrow title fallback."""
    if role_tag in _EXECUTIVE_ROLE_TAGS:
        return 3
    if role_tag in _LEADER_ROLE_TAGS:
        return 2
    if role_tag:
        return 1

    normalized_title = " ".join(str(title or "").split()).casefold()
    executive_terms = (
        "chief ", "ceo", "cfo", "coo", "cro", "cio", "cto", "ciso",
        "founder", "owner", "president", "vice president", "vp ", "head of",
    )
    if any(term in normalized_title for term in executive_terms):
        return 3
    if "director" in normalized_title:
        return 2
    if any(term in normalized_title for term in ("manager", " lead", "lead ")):
        return 1
    return 0


def _contact_role_rank(*, role: str, is_primary: bool) -> int:
    if role == "decision_maker":
        return 4
    if role == "champion":
        return 3
    if is_primary:
        return 2
    return 0


def _timestamp_sort_value(value) -> float:
    return value.timestamp() if value is not None else 0.0


def _company_cohort_format_requests(
    *,
    sheet_id: int,
    headers: list[str],
    rows: list[list[str]],
) -> tuple[list[dict[str, Any]], int]:
    """Band contiguous company cohorts without adding spacer rows or merges."""
    if not rows:
        return [], 0
    company_index = headers.index(COL_COMPANY)
    groups: list[tuple[int, int]] = []
    start = 0
    for index in range(1, len(rows) + 1):
        if index == len(rows) or rows[index][company_index] != rows[start][company_index]:
            groups.append((start, index))
            start = index

    requests: list[dict[str, Any]] = []
    for band_index, (start, end) in enumerate(groups):
        sheet_start = start + 1
        sheet_end = end + 1
        requests.append({
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": sheet_start,
                    "endRowIndex": sheet_end,
                    "startColumnIndex": 0,
                    "endColumnIndex": len(headers),
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": _COHORT_FILLS[band_index % 2],
                    },
                },
                "fields": "userEnteredFormat.backgroundColor",
            },
        })
        requests.append({
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": sheet_start,
                    "endRowIndex": sheet_start + 1,
                    "startColumnIndex": 0,
                    "endColumnIndex": len(headers),
                },
                "cell": {
                    "userEnteredFormat": {
                        "borders": {
                            "top": {
                                "style": "SOLID_MEDIUM",
                                "color": _COHORT_BORDER,
                            },
                        },
                    },
                },
                "fields": "userEnteredFormat.borders.top",
            },
        })
    return requests, len(groups)


def _existing_manual_values(
    values: list[list[Any]],
    *,
    reset: bool,
) -> tuple[list[str], dict[tuple[str, str], dict[str, str]]]:
    """Return output headers and operator cells keyed by a visible identity."""
    rows = _material_rows(values)
    if not rows:
        return list(HEADERS), {}
    headers = [cell.strip() for cell in rows[0]]
    if not all(headers) or len(headers) != len(set(headers)):
        raise SheetsError("People contains blank or duplicate headings")
    missing = [header for header in HEADERS if header not in headers]
    # The first migration may only be missing newly managed headers; append
    # them while preserving the position of operator columns.
    output_headers = [header for header in headers if header not in REMOVED_HEADERS]
    output_headers.extend(header for header in missing if header not in output_headers)
    if reset:
        return output_headers, {}

    indexes = {header: headers.index(header) for header in headers}
    if COL_LINKEDIN_URL not in indexes or COL_EMAILS not in indexes:
        raise SheetsError("People needs LinkedIn URL and Email addresses for identity")
    manual = MANUAL_HEADERS | (set(output_headers) - set(HEADERS))
    preserved: dict[tuple[str, str], dict[str, str]] = {}
    for row_number, row in enumerate(rows[1:], start=2):
        if not any(str(cell or "") for cell in row):
            continue
        key = _identity(
            row[indexes[COL_LINKEDIN_URL]] if indexes[COL_LINKEDIN_URL] < len(row) else "",
            row[indexes[COL_EMAILS]] if indexes[COL_EMAILS] < len(row) else "",
        )
        if key is None:
            continue
        if key in preserved:
            raise SheetsError(
                f"People rows contain duplicate visible identity at row {row_number}"
            )
        preserved[key] = {
            header: str(row[indexes[header]] if indexes[header] < len(row) else "")
            for header in manual
            if header in indexes
        }
    return output_headers, preserved


def build_people_rows(
    scope: AccountScope,
    *,
    preserved: dict[tuple[str, str], dict[str, str]],
    output_headers: list[str],
) -> tuple[list[list[str]], dict[str, int]]:
    """Build deterministic relationship-backed rows for Active Accounts."""
    from crm.models import (
        Lead,
        Meeting,
        MeetingNote,
        MeetingParticipant,
        Message,
        OpportunityContact,
    )
    from crm.models.sales import normalize_account_name
    from linkedin.notifications import sheets

    now = timezone.now()
    contact_rows = list(
        OpportunityContact.objects.filter(opportunity__account_id__in=scope.account_ids)
        .values_list(
            "lead_id",
            "opportunity__account_id",
            "role",
            "is_primary",
        )
    )
    account_ids_by_lead: dict[int, list[str]] = defaultdict(list)
    contact_role_rank_by_lead_company: dict[tuple[int, str], int] = defaultdict(int)
    for lead_id, account_id, role, is_primary in contact_rows:
        lead_id = int(lead_id)
        account_id = str(account_id)
        account_ids_by_lead[lead_id].append(account_id)
        company_key = scope.account_company.get(account_id, "")
        contact_key = (lead_id, company_key)
        contact_role_rank_by_lead_company[contact_key] = max(
            contact_role_rank_by_lead_company[contact_key],
            _contact_role_rank(role=role, is_primary=is_primary),
        )

    scoped_candidates: list[tuple[Any, list[str], set[str]]] = []
    for lead in (
        Lead.objects.defer("embedding")
        .order_by("company_name", "last_name", "first_name", "id")
    ):
        company_key = normalize_account_name(lead.company_name)
        linked_accounts = account_ids_by_lead.get(int(lead.pk), [])
        scoped_company_keys = {
            scope.account_company[account_id]
            for account_id in linked_accounts
            if account_id in scope.account_company
        }
        if company_key in scope.company_keys:
            scoped_company_keys.add(company_key)
        if scoped_company_keys:
            scoped_candidates.append((lead, linked_accounts, scoped_company_keys))

    candidate_ids = [int(lead.pk) for lead, _, _ in scoped_candidates]
    gmail_lead_ids = set(
        Message.objects.filter(
            lead_id__in=candidate_ids,
            source=Message.Source.GMAIL,
            direction=Message.Direction.INBOUND,
        ).values_list("lead_id", flat=True)
    )
    latest_contact_by_lead: dict[int, Any] = {}
    for lead_id, sent_at in Message.objects.filter(
        lead_id__in=candidate_ids,
        source=Message.Source.GMAIL,
    ).values_list("lead_id", "sent_at"):
        lead_id = int(lead_id)
        latest_contact_by_lead[lead_id] = _latest(
            latest_contact_by_lead.get(lead_id),
            sent_at,
        )
    meeting_lead_ids = set(
        Meeting.objects.filter(lead_id__in=candidate_ids).values_list("lead_id", flat=True)
    )
    meeting_lead_ids.update(
        MeetingParticipant.objects.filter(lead_id__in=candidate_ids)
        .values_list("lead_id", flat=True)
    )
    for lead_id, start_at in Meeting.objects.filter(
        lead_id__in=candidate_ids,
        start_at__lte=now,
    ).values_list("lead_id", "start_at"):
        lead_id = int(lead_id)
        latest_contact_by_lead[lead_id] = _latest(
            latest_contact_by_lead.get(lead_id),
            start_at,
        )
    for lead_id, start_at in MeetingParticipant.objects.filter(
        lead_id__in=candidate_ids,
        meeting__start_at__lte=now,
    ).values_list("lead_id", "meeting__start_at"):
        lead_id = int(lead_id)
        latest_contact_by_lead[lead_id] = _latest(
            latest_contact_by_lead.get(lead_id),
            start_at,
        )
    curated_contact_ids = set(
        OpportunityContact.objects.filter(opportunity__account_id__in=scope.account_ids)
        .filter(
            Q(is_primary=True)
            | Q(role__in=(
                OpportunityContact.Role.CHAMPION,
                OpportunityContact.Role.DECISION_MAKER,
            ))
            | ~Q(notes="")
        )
        .values_list("lead_id", flat=True)
    )
    meeting_attendee_emails: set[str] = set()
    latest_note_by_company_email: dict[tuple[str, str], Any] = {}
    company_latest_contact: dict[str, Any] = {}
    note_rows = MeetingNote.objects.filter(
        opportunity__account_id__in=scope.account_ids,
        match_status=MeetingNote.MatchStatus.MATCHED,
    ).values_list(
        "attendees",
        "scheduled_start_at",
        "opportunity__account_id",
    )
    for attendees, scheduled_start_at, account_id in note_rows:
        company_key = scope.account_company.get(str(account_id))
        is_past_contact = scheduled_start_at is not None and scheduled_start_at <= now
        if company_key and is_past_contact:
            company_latest_contact[company_key] = _latest(
                company_latest_contact.get(company_key),
                scheduled_start_at,
            )
        if not isinstance(attendees, list):
            continue
        for attendee in attendees:
            if not isinstance(attendee, dict):
                continue
            email = str(attendee.get("email") or "").strip().casefold()
            if email:
                meeting_attendee_emails.add(email)
                if company_key and is_past_contact:
                    note_key = (company_key, email)
                    latest_note_by_company_email[note_key] = _latest(
                        latest_note_by_company_email.get(note_key),
                        scheduled_start_at,
                    )

    for account_id, start_at in Meeting.objects.filter(
        opportunity__account_id__in=scope.account_ids,
        start_at__lte=now,
    ).values_list("opportunity__account_id", "start_at"):
        company_key = scope.account_company.get(str(account_id))
        if company_key:
            company_latest_contact[company_key] = _latest(
                company_latest_contact.get(company_key),
                start_at,
            )

    today = timezone.localdate().isoformat()
    person_rows: list[tuple[str, dict[str, Any]]] = []
    skipped_no_identity = 0
    excluded_without_relationship = 0
    selected = 0
    gmail_contacts = 0
    meeting_contacts = 0
    manual_contacts = 0
    curated_contacts = 0
    for lead, linked_accounts, scoped_company_keys in scoped_candidates:
        email = str(lead.email or "").strip().casefold()
        full_name = f"{lead.first_name} {lead.last_name}".strip()
        normalized_full_name = _normalized_person_name(full_name)
        has_gmail = int(lead.pk) in gmail_lead_ids
        has_meeting = int(lead.pk) in meeting_lead_ids or (
            bool(email) and email in meeting_attendee_emails
        )
        is_main_contact = any(
            normalized_full_name in scope.main_contacts.get(company_key, ())
            for company_key in scoped_company_keys
        )
        is_curated_contact = int(lead.pk) in curated_contact_ids
        if not (has_gmail or has_meeting or is_main_contact or is_curated_contact):
            excluded_without_relationship += 1
            continue
        selected += 1
        gmail_contacts += int(has_gmail)
        meeting_contacts += int(has_meeting)
        manual_contacts += int(is_main_contact)
        curated_contacts += int(is_curated_contact)
        title, location = _profile_fields(lead)
        lead_company_key = normalize_account_name(lead.company_name)
        if lead_company_key in scoped_company_keys:
            company_key = lead_company_key
        elif len(scoped_company_keys) == 1:
            company_key = next(iter(scoped_company_keys))
        else:
            raise SheetsError(
                f"Lead {lead.pk} maps to multiple Active Accounts without an exact company match"
            )
        company = scope.company_display.get(company_key, lead.company_name.strip())
        linkedin_url = sheets.canonical_linkedin_url(lead.linkedin_url or "")
        emails = lead.email.strip()
        key = _identity(linkedin_url, emails)
        if key is None:
            skipped_no_identity += 1
            continue
        operator = preserved.get(key, {})
        created = lead.creation_date.date().isoformat() if lead.creation_date else ""
        latest_contact = _latest(
            latest_contact_by_lead.get(int(lead.pk)),
            latest_note_by_company_email.get((company_key, email)) if email else None,
        )
        company_latest_contact[company_key] = _latest(
            company_latest_contact.get(company_key),
            latest_contact,
        )
        contact_rank = contact_role_rank_by_lead_company.get(
            (int(lead.pk), company_key),
            0,
        )
        if is_main_contact:
            contact_rank = max(contact_rank, 1)
        row = {
            COL_NAME: full_name,
            COL_FIRST_NAME: lead.first_name or "",
            COL_LAST_NAME: lead.last_name or "",
            COL_COMPANY: company,
            COL_TITLE: title,
            COL_LINKEDIN_URL: linkedin_url,
            COL_EMAILS: emails,
            COL_PRIORITY: operator.get(COL_PRIORITY, ""),
            COL_PRIMARY_LOCATION: location,
            COL_NOTES: operator.get(COL_NOTES, ""),
            COL_CREATED_AT: created,
            COL_LAST_SYNCED: today,
            COL_ROLE_TAG: lead.role_tag or "",
            _SORT_COMPANY_KEY: company_key,
            _SORT_LATEST_CONTACT: latest_contact,
            _SORT_RELEVANCE: (
                _priority_rank(operator.get(COL_PRIORITY, "")),
                contact_rank,
                _role_rank(lead.role_tag or "", title),
                int(has_gmail) + int(has_meeting),
            ),
        }
        for header in output_headers:
            if header not in HEADERS:
                row[header] = operator.get(header, "")
        person_rows.append((company_key, row))

    grouped_rows: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for company_key, row in person_rows:
        normalized_name = _normalized_person_name(row[COL_NAME])
        # A blank name is not enough evidence to merge two otherwise distinct
        # identities. Keep those rows separate and let visible identity police
        # accidental duplicates below.
        person_key = normalized_name or f"identity:{_identity(row[COL_LINKEDIN_URL], row[COL_EMAILS])}"
        grouped_rows[(company_key, person_key)].append(row)

    rows: list[dict[str, Any]] = []
    deduplicated_rows = 0
    manual_headers = MANUAL_HEADERS | (set(output_headers) - set(HEADERS))
    for grouped in grouped_rows.values():
        linkedin_urls = {
            row[COL_LINKEDIN_URL].casefold()
            for row in grouped
            if row[COL_LINKEDIN_URL]
        }
        emails = {
            email.casefold()
            for row in grouped
            for email in _email_values(row[COL_EMAILS])
        }
        if len(grouped) == 1 or len(linkedin_urls) > 1 or len(emails) > 1:
            rows.extend(grouped)
            continue

        merged = dict(grouped[0])
        for field in (
            COL_TITLE,
            COL_LINKEDIN_URL,
            COL_EMAILS,
            COL_PRIMARY_LOCATION,
            COL_ROLE_TAG,
        ):
            if not merged.get(field):
                merged[field] = next(
                    (row[field] for row in grouped[1:] if row.get(field)),
                    "",
                )
        created_dates = [row[COL_CREATED_AT] for row in grouped if row[COL_CREATED_AT]]
        if created_dates:
            merged[COL_CREATED_AT] = min(created_dates)
        merged[_SORT_LATEST_CONTACT] = max(
            (row.get(_SORT_LATEST_CONTACT) for row in grouped),
            key=_timestamp_sort_value,
        )
        merged[_SORT_RELEVANCE] = max(
            row.get(_SORT_RELEVANCE, (0, 0, 0, 0))
            for row in grouped
        )
        for header in manual_headers:
            values = {row.get(header, "") for row in grouped if row.get(header, "")}
            if len(values) > 1:
                raise SheetsError(
                    f"duplicate People identities for {merged[COL_NAME]!r} have conflicting {header!r}"
                )
            merged[header] = next(iter(values), "")
        rows.append(merged)
        deduplicated_rows += len(grouped) - 1

    rows.sort(key=lambda row: (
        -_timestamp_sort_value(company_latest_contact.get(row[_SORT_COMPANY_KEY])),
        row[COL_COMPANY].casefold(),
        *(-rank for rank in row.get(_SORT_RELEVANCE, (0, 0, 0, 0))),
        -_timestamp_sort_value(row.get(_SORT_LATEST_CONTACT)),
        row[COL_LAST_NAME].casefold(), row[COL_FIRST_NAME].casefold(),
        row[COL_LINKEDIN_URL].casefold(), row[COL_EMAILS].casefold(),
    ))
    identities: set[tuple[str, str]] = set()
    for row in rows:
        key = _identity(row[COL_LINKEDIN_URL], row[COL_EMAILS])
        if key in identities:
            raise SheetsError("Selected People contain duplicate visible identities")
        identities.add(key)
    return (
        [[row.get(header, "") for header in output_headers] for row in rows],
        {
            "selected_leads": selected,
            "published_rows": len(rows),
            "deduplicated_rows": deduplicated_rows,
            "skipped_no_identity": skipped_no_identity,
            "explicit_contact_links": len(contact_rows),
            "excluded_without_relationship_evidence": excluded_without_relationship,
            "gmail_contacts": gmail_contacts,
            "meeting_contacts": meeting_contacts,
            "manual_main_contacts": manual_contacts,
            "curated_opportunity_contacts": curated_contacts,
        },
    )


def sync_people_from_active_accounts(
    *,
    dry_run: bool = True,
    reset: bool = False,
    backup_dir: str | Path = "artifacts/crm-backups",
) -> dict[str, Any]:
    """Plan or atomically replace People from the current Active Accounts."""
    from linkedin import conf
    from linkedin.notifications import crm_sheets, crm_v2_layout, sheets

    spreadsheet = sheets._gspread_client()
    if str(getattr(spreadsheet, "id", "")) != str(conf.GOOGLE_SHEETS_ID):
        raise SheetsError("People publisher opened an unexpected workbook")
    try:
        active_ws = spreadsheet.worksheet(active_accounts.TAB_NAME)
        people_ws = spreadsheet.worksheet(TAB_NAME)
        active_values = active_ws.get_all_values()
        people_values = people_ws.get_all_values()
    except Exception as exc:
        raise SheetsError("failed reading Active Accounts or People") from exc

    scope = read_account_scope(active_values)
    output_headers, preserved = _existing_manual_values(people_values, reset=reset)
    people_rows, counts = build_people_rows(
        scope,
        preserved=preserved,
        output_headers=output_headers,
    )
    _, cohort_count = _company_cohort_format_requests(
        sheet_id=0,
        headers=output_headers,
        rows=people_rows,
    )
    desired = [output_headers, *people_rows]
    before = _material_rows(people_values)
    report: dict[str, Any] = {
        "schema": "openoutreach.active-account-people.v1",
        "status": "planned" if dry_run else "published",
        "reset": reset,
        "active_account_rows": scope.rows,
        "rows_before": max(0, len(before) - 1),
        "rows_after": len(people_rows),
        "changed": before != desired,
        "removed_headers": sorted(set(before[0] if before else ()) & REMOVED_HEADERS),
        "manual_columns": sorted(MANUAL_HEADERS | (set(output_headers) - set(HEADERS))),
        "company_cohorts": cohort_count,
        "sort_policy": "company latest contact; person closing relevance; person latest contact",
        "sends_performed": 0,
        **counts,
    }
    if dry_run:
        return report

    backup_path = crm_sheets.backup_spreadsheet(
        spreadsheet,
        Path(backup_dir),
        titles=(active_accounts.TAB_NAME, TAB_NAME),
        prefix="people-before-active-account-sync",
    )
    report["backup"] = str(backup_path)
    active_fingerprint = _fingerprint(active_values)
    people_fingerprint = _fingerprint(people_values)
    token = uuid4().hex[:12]
    staged = None
    try:
        staged = spreadsheet.add_worksheet(
            title=f"_People staging {token}",
            rows=max(1000, len(desired) + 10),
            cols=len(output_headers),
        )
        for start in range(0, len(desired), 500):
            chunk = desired[start:start + 500]
            end = start + len(chunk)
            staged.update(
                range_name=f"A{start + 1}:{_column_letter(len(output_headers))}{end}",
                values=chunk,
            )
        crm_v2_layout.apply_layout(
            spreadsheet,
            staged,
            headers=output_headers,
            technical_fields=(),
        )
        cohort_requests, _ = _company_cohort_format_requests(
            sheet_id=int(staged.id),
            headers=output_headers,
            rows=people_rows,
        )
        if cohort_requests:
            spreadsheet.batch_update({"requests": cohort_requests})
        readback = _material_rows(staged.get_all_values())
        if readback != desired:
            raise SheetsError("staged People readback did not match")
        if _fingerprint(active_ws.get_all_values()) != active_fingerprint:
            raise SheetsError("Active Accounts changed during People publication")
        if _fingerprint(people_ws.get_all_values()) != people_fingerprint:
            raise SheetsError("People changed during publication")
        archive_title = f"_People archived {token}"
        spreadsheet.batch_update({"requests": [
            _rename_request(people_ws.id, archive_title),
            _rename_request(staged.id, TAB_NAME, index=int(people_ws.index)),
        ]})
        spreadsheet.batch_update({"requests": [
            {"deleteSheet": {"sheetId": people_ws.id}},
        ]})
        published = spreadsheet.worksheet(TAB_NAME)
        if _material_rows(published.get_all_values()) != desired:
            raise SheetsError("published People readback did not match")
        report["verified"] = True
        return report
    except Exception as exc:
        if isinstance(exc, SheetsError):
            raise
        raise SheetsError("People publication failed") from exc


def _column_letter(number_1: int) -> str:
    letters = ""
    while number_1:
        number_1, remainder = divmod(number_1 - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _rename_request(sheet_id: int, title: str, *, index: int | None = None) -> dict:
    properties: dict[str, Any] = {"sheetId": sheet_id, "title": title}
    fields = ["title"]
    if index is not None:
        properties["index"] = index
        fields.append("index")
    return {
        "updateSheetProperties": {
            "properties": properties,
            "fields": ",".join(fields),
        }
    }
