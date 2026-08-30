"""Gmail-backed data-sync for followup context.

This is the Python/API replacement for the Gmail-accessible portion of the
old Claude data-sync workflow. It persists:
  - normal prospect email threads into crm.Message(source=gmail)
  - Gemini / Google Meet note emails into crm.Meeting.gemini_notes_raw
It also returns a bounded review set of exact, bidirectional external Gmail
participants that do not yet have a Lead. That discovery path never sends or
auto-creates CRM rows.

Calendar + Drive APIs can still provide richer matching later, but Gmail note
emails already contain useful note text and a Drive link, so this gives the
followup workflow the same DB surfaces without requiring MCP access.
"""
from __future__ import annotations

import base64
import hashlib
import html
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from typing import Iterable

from django.db import transaction
from django.utils import timezone as dj_tz

from crm.models import Lead, Meeting, Message
from gmail.client import GmailClient, scoped_gmail_id
from linkedin.notifications.calendar_events import persist_gemini_notes
from linkedin.notifications.gmail_threads import persist_gmail_threads
from linkedin.operators import resolve_sales_owner_handle


NOTE_QUERY = (
    "from:(gemini-notes@google.com OR meetings-noreply@google.com) "
    "subject:Notes newer_than:{days}d"
)
RECENT_HUMAN_THREAD_QUERY = (
    "newer_than:{days}d -category:promotions -category:social -category:forums "
    "-label:drafts -label:scheduled"
)

# Exact-address OR queries keep Gmail discovery proportional to batches of
# contacts, rather than issuing one Gmail search for every Lead. Query length
# is capped as well as address count because Lead.email permits long values.
EMAILS_PER_SEARCH_QUERY = 40
PARTICIPANT_QUERY_MAX_CHARS = 6_000
KNOWN_SEARCH_MAX_MESSAGES_PER_QUERY = 500
KNOWN_SEARCH_MAX_MESSAGES_PER_ADDRESS = 2_000

# Gmail threads.get currently costs 40 quota units and the per-user/project
# limit is 6,000 units/minute. Leave room for list/profile/note calls and make
# repeat runs converge by fetching only threads whose search hits include an
# as-yet-unpersisted Gmail message ID.
MAX_THREAD_FETCHES_PER_RUN = 80
KNOWN_THREAD_FETCH_SHARE = 0.75

# The recent mailbox discovery lane exists to surface email-first relationships
# that have no Lead yet.  It is intentionally a small, newest-first review
# window, not an unbounded mailbox export.
RECENT_DISCOVERY_DAYS = 90
RECENT_DISCOVERY_MAX_MESSAGES = 500
RECENT_DISCOVERY_MAX_THREADS = 500
MAX_SCAN_CHECKPOINT_ENTRIES = 5_000


@dataclass
class GmailContextSyncResult:
    leads_considered: int = 0
    leads_with_email_threads: int = 0
    gmail_search_queries: int = 0
    gmail_search_queries_at_cap: int = 0
    gmail_search_batches_split: int = 0
    gmail_search_messages_seen: int = 0
    gmail_threads_fetched: int = 0
    gmail_threads_matched: int = 0
    gmail_threads_ambiguous: int = 0
    gmail_threads_deferred: int = 0
    gmail_automated_messages_skipped: int = 0
    gmail_unsent_messages_skipped: int = 0
    gmail_human_inbound_messages: int = 0
    gmail_messages_created: int = 0
    discovery_messages_scanned: int = 0
    discovery_threads_selected: int = 0
    gmail_processed_thread_versions: dict[str, str] = field(default_factory=dict)
    unmapped_external_participants: list[dict] = field(default_factory=list)
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


def candidate_leads(*, campaign_id: int | None = None):
    """Return every exact email identity that Gmail context may enrich.

    ``Lead.disqualified`` and legacy ``Deal.state`` are outbound-automation
    controls, not evidence that a human relationship stopped existing.  Gmail
    ingestion therefore never uses either field as an admission gate.  An
    explicit campaign filter remains available for a deliberately scoped
    diagnostic run.
    """
    qs = Lead.objects.exclude(email="").exclude(email__isnull=True)
    if campaign_id is not None:
        qs = qs.filter(deal__campaign_id=campaign_id)
    return qs.distinct().order_by("id")


_EMAIL_RE = re.compile(r"^[^\s<>@]+@[^\s<>@]+$")
_RECIPIENT_HEADER_NAMES = ("to", "cc", "bcc")
_AUTOMATED_SUBJECT_RE = re.compile(
    r"^(?:(?:re|fw|fwd):\s*)*(?:"
    r"automatic reply|auto(?:matic)?[ -]?response|auto[ -]?reply|"
    r"out of office|ooo(?:\s*$|\s+(?:until|through|returning|back)\b|:)|"
    r"delivery status notification|undeliverable|"
    r"returned mail|failure notice|mail delivery failed"
    r")",
    re.I,
)
_AUTOMATED_LOCAL_PART_RE = re.compile(
    r"(?:^|[._-])(?:no[._-]?reply|do[._-]?not[._-]?reply|mailer[._-]?daemon|"
    r"postmaster|notifications?|alerts?)(?:$|[+._-])",
    re.I,
)
_PUBLIC_MAILBOX_DOMAINS = {
    "aol.com",
    "gmail.com",
    "googlemail.com",
    "hotmail.com",
    "icloud.com",
    "live.com",
    "outlook.com",
    "proton.me",
    "protonmail.com",
    "yahoo.com",
}


def _normalize_email(value: str) -> str:
    """Return one exact lowercase RFC address or an empty invalid marker."""
    _, parsed = parseaddr(str(value or ""))
    normalized = (parsed or "").strip().lower()
    return normalized if _EMAIL_RE.fullmatch(normalized) else ""


def _scoped_gmail_id(account_key: str, raw_id: str) -> str:
    """Namespace Gmail's mailbox-local IDs before CRM persistence."""
    return scoped_gmail_id(account_key, raw_id)


def _thread_checkpoint_key(thread_id: str) -> str:
    return hashlib.sha256(thread_id.encode("utf-8")).hexdigest()


def _thread_checkpoint_version(message_ids: Iterable[str]) -> str:
    payload = "\0".join(sorted(set(message_ids)))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _chunks(values: list[str], size: int):
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _participant_query(emails: list[str], *, since_days: int) -> str:
    terms = []
    for email in emails:
        terms.extend((
            f"from:{email}",
            f"to:{email}",
            f"cc:{email}",
            f"bcc:{email}",
        ))
    return (
        f"newer_than:{int(since_days)}d -label:drafts -label:scheduled "
        f"{{{' '.join(terms)}}}"
    )


def _participant_query_batches(emails: list[str], *, since_days: int):
    batch: list[str] = []
    for email in emails:
        candidate = [*batch, email]
        if batch and (
            len(candidate) > EMAILS_PER_SEARCH_QUERY
            or len(_participant_query(candidate, since_days=since_days))
            > PARTICIPANT_QUERY_MAX_CHARS
        ):
            yield batch
            batch = [email]
        else:
            batch = candidate
    if batch:
        yield batch


def _bounded_message_search(
    service,
    *,
    query: str,
    max_items: int,
) -> tuple[list[dict], bool, int]:
    """Return bounded hits plus an explicit Gmail-pagination truncation bit."""
    items: list[dict] = []
    page_token = None
    api_calls = 0
    truncated = False
    while len(items) < max_items:
        remaining = max_items - len(items)
        kwargs = {
            "userId": "me",
            "q": query,
            "maxResults": min(500, remaining),
        }
        if page_token:
            kwargs["pageToken"] = page_token
        response = service.users().messages().list(**kwargs).execute()
        api_calls += 1
        page_items = list(response.get("messages", []))
        items.extend(page_items[:remaining])
        next_token = response.get("nextPageToken")
        if len(page_items) > remaining:
            truncated = True
            break
        if not next_token:
            break
        if len(items) >= max_items:
            truncated = True
            break
        page_token = next_token
    return items, truncated, api_calls


def _known_message_searches(
    service,
    *,
    email_batch: list[str],
    since_days: int,
    result: GmailContextSyncResult,
) -> list[list[dict]]:
    """Recursively split a truncated OR query so one noisy Lead is fair."""
    terminal_hits: list[list[dict]] = []

    def search(batch: list[str]) -> None:
        max_items = (
            KNOWN_SEARCH_MAX_MESSAGES_PER_ADDRESS
            if len(batch) == 1
            else KNOWN_SEARCH_MAX_MESSAGES_PER_QUERY
        )
        items, truncated, api_calls = _bounded_message_search(
            service,
            query=_participant_query(batch, since_days=since_days),
            max_items=max_items,
        )
        result.gmail_search_queries += api_calls
        if truncated and len(batch) > 1:
            result.gmail_search_batches_split += 1
            midpoint = len(batch) // 2
            search(batch[:midpoint])
            search(batch[midpoint:])
            return
        if truncated:
            result.gmail_search_queries_at_cap += 1
        result.gmail_search_messages_seen += len(items)
        terminal_hits.append(items)

    search(email_batch)
    return terminal_hits


def _message_emails(msg: dict, names: Iterable[str]) -> set[str]:
    headers = _headers(msg)
    # Python 3.13's strict RFC parser treats a list padded with empty header
    # values as malformed, so only pass headers that are actually present.
    values = [headers[name] for name in names if headers.get(name)]
    return {
        email
        for _, raw_email in getaddresses(values)
        if (email := _normalize_email(raw_email))
    }


def _message_from(msg: dict) -> str:
    addresses = _message_emails(msg, ("from",))
    return next(iter(addresses)) if len(addresses) == 1 else ""


def _is_automated_message(msg: dict) -> bool:
    """Recognize machine mail using transport headers, labels, and envelope."""
    headers = _headers(msg)
    auto_submitted = headers.get("auto-submitted", "").strip().lower()
    if auto_submitted and auto_submitted != "no":
        return True
    if headers.get("list-id") or headers.get("list-unsubscribe"):
        return True
    if headers.get("x-autoreply") or headers.get("x-autorespond"):
        return True
    if headers.get("precedence", "").strip().lower() in {"bulk", "junk", "list"}:
        return True
    if _AUTOMATED_SUBJECT_RE.match(headers.get("subject", "").strip()):
        return True
    sender = _message_from(msg)
    local_part = sender.partition("@")[0]
    return bool(_AUTOMATED_LOCAL_PART_RE.search(local_part))


def _without_non_human_messages(thread: dict) -> tuple[dict, int, int]:
    automated_count = 0
    unsent_count = 0
    human_messages = []
    for msg in thread.get("messages", []):
        if _is_automated_message(msg):
            automated_count += 1
            continue
        labels = {str(label).upper() for label in msg.get("labelIds", [])}
        if labels & {"DRAFT", "SCHEDULED"}:
            unsent_count += 1
            continue
        human_messages.append(msg)
    return {
        "id": thread.get("id", ""),
        "messages": human_messages,
    }, automated_count, unsent_count


def _thread_within_days(thread: dict, *, days: int) -> dict:
    cutoff = dj_tz.now() - timedelta(days=int(days))
    return {
        "id": thread.get("id", ""),
        "messages": [
            msg
            for msg in thread.get("messages", [])
            if _message_datetime(msg, _headers(msg)) >= cutoff
        ],
    }


def _human_inbound_count(thread: dict, *, self_emails: set[str]) -> int:
    return sum(
        1
        for msg in thread.get("messages", [])
        if _message_from(msg) and _message_from(msg) not in self_emails
    )


def _messages_by_lead_for_thread(
    thread: dict,
    *,
    leads_by_email: dict[str, list[Lead]],
    leads_by_id: dict[int, Lead],
    self_emails: set[str],
    existing_message_owner_ids: dict[str, int],
) -> tuple[dict[int, list[dict]], bool]:
    """Map each message independently; never assign a whole group thread."""
    grouped: dict[int, list[dict]] = defaultdict(list)
    ambiguous = False
    for msg in thread.get("messages", []):
        external_id = (msg.get("id") or "").strip()
        stable_owner_id = existing_message_owner_ids.get(external_id)
        if stable_owner_id in leads_by_id:
            grouped[stable_owner_id].append(msg)
            continue

        sender = _message_from(msg)
        if not sender:
            continue
        if sender not in self_emails:
            candidates = {lead.id for lead in leads_by_email.get(sender, [])}
        else:
            candidates = {
                lead.id
                for email in _message_emails(msg, _RECIPIENT_HEADER_NAMES)
                for lead in leads_by_email.get(email, [])
            }
        if len(candidates) == 1:
            grouped[next(iter(candidates))].append(msg)
        elif len(candidates) > 1:
            ambiguous = True
    return grouped, ambiguous


def _unmapped_external_participants(
    thread: dict,
    *,
    account_key: str,
    known_emails: set[str],
    self_emails: set[str],
) -> list[dict]:
    """Return exact unknown humans with bidirectional evidence in one thread."""
    self_domains = {
        email.rsplit("@", 1)[1]
        for email in self_emails
        if "@" in email
        and email.rsplit("@", 1)[1] not in _PUBLIC_MAILBOX_DOMAINS
    }
    outbound_recipients: set[str] = set()
    for msg in thread.get("messages", []):
        if _message_from(msg) in self_emails:
            outbound_recipients.update(_message_emails(msg, _RECIPIENT_HEADER_NAMES))

    candidates: dict[str, dict] = {}
    for msg in thread.get("messages", []):
        sender = _message_from(msg)
        if (
            not sender
            or sender in self_emails
            or sender in known_emails
            or sender not in outbound_recipients
        ):
            continue
        local_part, _, domain = sender.partition("@")
        if (
            not domain
            or domain in self_domains
            or _AUTOMATED_LOCAL_PART_RE.search(local_part)
        ):
            continue
        raw_from = _headers(msg).get("from", "")
        display_name, _ = parseaddr(raw_from)
        display_name = _clean_text(display_name)
        sent_at = _message_datetime(msg, _headers(msg)).astimezone(timezone.utc)
        candidate = {
            "account_key": account_key,
            "email": sender,
            "display_name": display_name,
            "domain": domain,
            "last_inbound_at": sent_at.isoformat(),
            "latest_thread_id": thread.get("id", ""),
        }
        previous = candidates.get(sender)
        if previous is None or candidate["last_inbound_at"] > previous["last_inbound_at"]:
            candidates[sender] = candidate
    return list(candidates.values())


def _prior_unmapped_candidates(
    candidates: Iterable[dict],
    *,
    account_key: str,
    known_emails: set[str],
    self_emails: set[str],
    discovery_since_days: int,
) -> dict[str, dict]:
    """Keep still-recent structured review candidates from the last run."""
    cutoff = dj_tz.now() - timedelta(days=int(discovery_since_days))
    kept: dict[str, dict] = {}
    for raw in candidates or ():
        if not isinstance(raw, dict) or raw.get("account_key") != account_key:
            continue
        email = _normalize_email(raw.get("email", ""))
        if not email or email in known_emails or email in self_emails:
            continue
        try:
            last_inbound = datetime.fromisoformat(
                str(raw.get("last_inbound_at", "")).replace("Z", "+00:00")
            )
        except ValueError:
            continue
        if last_inbound.tzinfo is None:
            last_inbound = last_inbound.replace(tzinfo=timezone.utc)
        if last_inbound < cutoff:
            continue
        domain = email.rpartition("@")[2]
        try:
            thread_count = max(1, int(raw.get("thread_count", 1)))
        except (TypeError, ValueError):
            thread_count = 1
        kept[email] = {
            "account_key": account_key,
            "email": email,
            "display_name": _clean_text(str(raw.get("display_name", "")))[:200],
            "domain": domain,
            "last_inbound_at": last_inbound.astimezone(timezone.utc).isoformat(),
            "latest_thread_id": str(raw.get("latest_thread_id", ""))[:200],
            "thread_count": thread_count,
        }
    return kept


def _round_robin_thread_ids(batches: list[list[str]]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    max_length = max((len(batch) for batch in batches), default=0)
    for index in range(max_length):
        for batch in batches:
            if index >= len(batch):
                continue
            thread_id = batch[index]
            if thread_id not in seen:
                seen.add(thread_id)
                ordered.append(thread_id)
    return ordered


def _select_thread_fetches(
    *,
    known_batches: list[list[str]],
    discovery_order: list[str],
    unseen_thread_ids: set[str],
    max_fetches: int,
) -> list[str]:
    """Select fresh threads fairly while reserving email-first discovery."""
    if max_fetches <= 0:
        return []
    known_order = _round_robin_thread_ids(known_batches)
    known_set = set(known_order)
    recent_known = [tid for tid in discovery_order if tid in known_set]
    known_priority = recent_known + [tid for tid in known_order if tid not in recent_known]

    selected: list[str] = []
    seen: set[str] = set()

    def add(thread_id: str) -> None:
        if thread_id in unseen_thread_ids and thread_id not in seen:
            seen.add(thread_id)
            selected.append(thread_id)

    known_budget = (
        max_fetches
        if not discovery_order
        else int(max_fetches * KNOWN_THREAD_FETCH_SHARE)
    )
    for thread_id in known_priority:
        if len(selected) >= known_budget:
            break
        add(thread_id)
    for thread_id in discovery_order:
        if len(selected) >= max_fetches:
            break
        add(thread_id)
    for thread_id in known_priority:
        if len(selected) >= max_fetches:
            break
        add(thread_id)
    return selected


def _outbound_operator(
    msg: dict,
    *,
    self_emails: set[str],
    operator_by_self_email: dict[str, str],
) -> str:
    sender = _message_from(msg)
    if sender not in self_emails:
        return ""
    explicit = operator_by_self_email.get(sender, "")
    return explicit or resolve_sales_owner_handle(sender)


def sync_gmail_threads(
    *,
    client: GmailClient,
    leads: Iterable[Lead],
    self_emails: Iterable[str],
    since_days: int,
    dry_run: bool,
    discover_unmapped: bool = True,
    discovery_since_days: int = RECENT_DISCOVERY_DAYS,
    discovery_max_messages: int = RECENT_DISCOVERY_MAX_MESSAGES,
    discovery_max_threads: int = RECENT_DISCOVERY_MAX_THREADS,
    max_thread_fetches: int = MAX_THREAD_FETCHES_PER_RUN,
    operator_by_self_email: dict[str, str] | None = None,
    processed_thread_versions: dict[str, str] | None = None,
    prior_unmapped_external_participants: Iterable[dict] = (),
) -> GmailContextSyncResult:
    """Discover, exactly map, and persist human Gmail thread context.

    Known CRM identities are searched in bounded exact-address batches.  Each
    unique Gmail thread is fetched once and mapped from RFC From/To/Cc/Bcc
    participant addresses, never from subject/body text.  A separate bounded
    recent-mailbox pass returns bidirectional human participants that do not yet
    map to a Lead so a higher-level shadow/review workflow can consider them.
    It deliberately does not create Leads, Deals, Tasks, or outbound work.
    """
    result = GmailContextSyncResult()
    service = client._service
    account_key = str(client.account_key).strip()
    if not account_key:
        raise ValueError("Gmail context sync requires a stable account key.")
    self_set = {_normalize_email(value) for value in self_emails}
    self_set.discard("")
    if not self_set:
        raise ValueError(
            "Gmail context sync requires at least one valid mailbox identity."
        )
    operator_map = {
        normalized: operator
        for email, operator in (operator_by_self_email or {}).items()
        if (normalized := _normalize_email(email))
    }

    lead_list = list(leads)
    result.leads_considered = len(lead_list)
    leads_by_email: dict[str, list[Lead]] = defaultdict(list)
    leads_by_id: dict[int, Lead] = {}
    for lead in lead_list:
        email = _normalize_email(lead.email)
        if not email or email in self_set:
            continue
        leads_by_email[email].append(lead)
        leads_by_id[lead.id] = lead

    known_batches: list[list[str]] = []
    search_message_ids_by_thread: dict[str, set[str]] = defaultdict(set)
    for email_batch in _participant_query_batches(
        sorted(leads_by_email),
        since_days=since_days,
    ):
        terminal_searches = _known_message_searches(
            service,
            email_batch=email_batch,
            since_days=since_days,
            result=result,
        )
        for hits in terminal_searches:
            batch_thread_ids: list[str] = []
            batch_seen: set[str] = set()
            for item in hits:
                thread_id = (item.get("threadId") or "").strip()
                if not thread_id:
                    continue
                search_message_ids_by_thread.setdefault(thread_id, set())
                message_id = (item.get("id") or "").strip()
                if message_id:
                    search_message_ids_by_thread[thread_id].add(message_id)
                if thread_id not in batch_seen:
                    batch_seen.add(thread_id)
                    batch_thread_ids.append(thread_id)
            known_batches.append(batch_thread_ids)
    known_thread_ids = {
        thread_id for batch in known_batches for thread_id in batch
    }

    discovery_order: list[str] = []
    discovery_thread_ids: set[str] = set()
    if (
        discover_unmapped
        and discovery_max_messages > 0
        and discovery_max_threads > 0
    ):
        query = RECENT_HUMAN_THREAD_QUERY.format(days=int(discovery_since_days))
        for item in _list_messages(
            service,
            query=query,
            max_items=discovery_max_messages,
        ):
            result.discovery_messages_scanned += 1
            thread_id = (item.get("threadId") or "").strip()
            if not thread_id:
                continue
            search_message_ids_by_thread.setdefault(thread_id, set())
            message_id = (item.get("id") or "").strip()
            if message_id:
                search_message_ids_by_thread[thread_id].add(message_id)
            if thread_id not in discovery_thread_ids:
                discovery_thread_ids.add(thread_id)
                discovery_order.append(thread_id)
            if len(discovery_thread_ids) >= discovery_max_threads:
                break

    persisted_search_message_ids: set[str] = set()
    all_search_message_ids = sorted({
        message_id
        for message_ids in search_message_ids_by_thread.values()
        for message_id in message_ids
    })
    external_to_raw = {
        external_id: raw_id
        for raw_id in all_search_message_ids
        for external_id in (
            raw_id,
            _scoped_gmail_id(account_key, raw_id),
        )
    }
    for external_id_batch in _chunks(sorted(external_to_raw), 500):
        for external_id in Message.objects.filter(
            source=Message.Source.GMAIL,
            external_id__in=external_id_batch,
        ).values_list("external_id", flat=True):
            persisted_search_message_ids.add(external_to_raw[external_id])

    prior_versions = {
        str(key): str(version)
        for key, version in (processed_thread_versions or {}).items()
        if re.fullmatch(r"[0-9a-f]{64}", str(key))
        and re.fullmatch(r"[0-9a-f]{64}", str(version))
    }
    current_versions = {
        _thread_checkpoint_key(_scoped_gmail_id(account_key, thread_id)):
            _thread_checkpoint_version(message_ids)
        for thread_id, message_ids in search_message_ids_by_thread.items()
        if message_ids
    }
    unseen_thread_ids = {
        thread_id
        for thread_id, message_ids in search_message_ids_by_thread.items()
        if (
            not message_ids
            or (
                message_ids - persisted_search_message_ids
                and prior_versions.get(_thread_checkpoint_key(
                    _scoped_gmail_id(account_key, thread_id)
                ))
                != current_versions.get(_thread_checkpoint_key(
                    _scoped_gmail_id(account_key, thread_id)
                ))
            )
        )
    }
    selected_thread_ids = _select_thread_fetches(
        known_batches=known_batches,
        discovery_order=discovery_order,
        unseen_thread_ids=unseen_thread_ids,
        max_fetches=max_thread_fetches,
    )
    result.discovery_threads_selected = sum(
        thread_id in discovery_thread_ids for thread_id in selected_thread_ids
    )
    result.gmail_threads_deferred = len(unseen_thread_ids - set(selected_thread_ids))

    matched_lead_ids: set[int] = set()
    unmapped = _prior_unmapped_candidates(
        prior_unmapped_external_participants,
        account_key=account_key,
        known_emails=set(leads_by_email),
        self_emails=self_set,
        discovery_since_days=discovery_since_days,
    )
    for thread_id in selected_thread_ids:
        thread = _gmail_thread_payload(
            service,
            thread_id,
            include_body=(not dry_run and thread_id in known_thread_ids),
        )
        thread = _scope_gmail_thread(thread, account_key=account_key)
        result.gmail_threads_fetched += 1
        human_thread, automated_count, unsent_count = _without_non_human_messages(
            thread
        )
        result.gmail_automated_messages_skipped += automated_count
        result.gmail_unsent_messages_skipped += unsent_count
        if not human_thread["messages"]:
            continue

        known_human_thread = _thread_within_days(human_thread, days=since_days)
        discovery_human_thread = _thread_within_days(
            human_thread,
            days=discovery_since_days,
        )
        evidence_thread = (
            known_human_thread
            if thread_id in known_thread_ids
            else discovery_human_thread
        )
        result.gmail_human_inbound_messages += _human_inbound_count(
            evidence_thread,
            self_emails=self_set,
        )

        persistence_thread = (
            known_human_thread
            if thread_id in known_thread_ids
            else {"id": human_thread["id"], "messages": []}
        )
        raw_id_by_scoped_id = {
            (msg.get("id") or "").strip(): (msg.get("_gmail_raw_id") or "").strip()
            for msg in persistence_thread.get("messages", [])
            if (msg.get("id") or "").strip()
        }
        persisted_rows = list(Message.objects.filter(
            source=Message.Source.GMAIL,
            external_id__in={
                value
                for scoped_id, raw_id in raw_id_by_scoped_id.items()
                for value in (scoped_id, raw_id)
                if value
            },
        ).values_list("external_id", "lead_id"))
        existing_message_owner_ids = {
            (
                _scoped_gmail_id(account_key, external_id)
                if external_id in raw_id_by_scoped_id.values()
                else external_id
            ): lead_id
            for external_id, lead_id in persisted_rows
        }
        legacy_scoped_ids = {
            scoped_id
            for scoped_id, raw_id in raw_id_by_scoped_id.items()
            if any(external_id == raw_id for external_id, _lead_id in persisted_rows)
        }
        messages_by_lead, ambiguous = _messages_by_lead_for_thread(
            persistence_thread,
            leads_by_email=leads_by_email,
            leads_by_id=leads_by_id,
            self_emails=self_set,
            existing_message_owner_ids=existing_message_owner_ids,
        )
        if ambiguous:
            result.gmail_threads_ambiguous += 1
        if messages_by_lead:
            result.gmail_threads_matched += 1
        for lead_id, messages in messages_by_lead.items():
            matched_lead_ids.add(lead_id)
            if dry_run:
                continue
            messages_by_operator: dict[str, list[dict]] = defaultdict(list)
            for msg in messages:
                if (msg.get("id") or "").strip() in legacy_scoped_ids:
                    continue
                operator = _outbound_operator(
                    msg,
                    self_emails=self_set,
                    operator_by_self_email=operator_map,
                )
                messages_by_operator[operator].append(msg)
            for operator, operator_messages in messages_by_operator.items():
                result.gmail_messages_created += persist_gmail_threads(
                    lead=leads_by_id[lead_id],
                    threads=[{"id": thread["id"], "messages": operator_messages}],
                    self_emails=self_set,
                    operator=operator,
                )

        if thread_id in discovery_thread_ids:
            for candidate in _unmapped_external_participants(
                discovery_human_thread,
                account_key=account_key,
                known_emails=set(leads_by_email),
                self_emails=self_set,
            ):
                previous = unmapped.get(candidate["email"])
                if previous is None:
                    candidate["thread_count"] = 1
                    unmapped[candidate["email"]] = candidate
                    continue
                if previous["latest_thread_id"] != candidate["latest_thread_id"]:
                    previous["thread_count"] += 1
                if candidate["last_inbound_at"] > previous["last_inbound_at"]:
                    previous.update({
                        "display_name": candidate["display_name"],
                        "domain": candidate["domain"],
                        "last_inbound_at": candidate["last_inbound_at"],
                        "latest_thread_id": candidate["latest_thread_id"],
                    })

    current_keys = set(current_versions)
    checkpoint = {
        key: value
        for key, value in prior_versions.items()
        if key in current_keys
    }
    for thread_id in selected_thread_ids:
        key = _thread_checkpoint_key(_scoped_gmail_id(account_key, thread_id))
        version = current_versions.get(key)
        if version:
            checkpoint[key] = version
    if len(checkpoint) > MAX_SCAN_CHECKPOINT_ENTRIES:
        checkpoint = dict(sorted(checkpoint.items())[-MAX_SCAN_CHECKPOINT_ENTRIES:])
    result.gmail_processed_thread_versions = dict(sorted(checkpoint.items()))

    result.leads_with_email_threads = len(matched_lead_ids)
    result.unmapped_external_participants = sorted(
        unmapped.values(),
        key=lambda item: (item["last_inbound_at"], item["email"]),
        reverse=True,
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


def _list_messages(service, *, query: str, max_items: int | None = None):
    page_token = None
    yielded = 0
    while True:
        remaining = None if max_items is None else max_items - yielded
        if remaining is not None and remaining <= 0:
            break
        kwargs = {
            "userId": "me",
            "q": query,
            "maxResults": min(500, remaining) if remaining is not None else 500,
        }
        if page_token:
            kwargs["pageToken"] = page_token
        resp = service.users().messages().list(**kwargs).execute()
        for item in resp.get("messages", []):
            if max_items is not None and yielded >= max_items:
                return
            yielded += 1
            yield item
        page_token = resp.get("nextPageToken")
        if not page_token:
            break


_DISCOVERY_METADATA_HEADERS = (
    "From",
    "To",
    "Cc",
    "Bcc",
    "Reply-To",
    "Subject",
    "Date",
    "Auto-Submitted",
    "List-ID",
    "List-Unsubscribe",
    "X-Autoreply",
    "X-Autorespond",
    "Precedence",
)


def _gmail_thread_payload(
    service,
    thread_id: str,
    *,
    include_body: bool,
) -> dict:
    kwargs = {
        "userId": "me",
        "id": thread_id,
        "format": "full" if include_body else "metadata",
    }
    if not include_body:
        kwargs["metadataHeaders"] = list(_DISCOVERY_METADATA_HEADERS)
    thread = service.users().threads().get(**kwargs).execute()
    return {
        "id": thread.get("id") or thread_id,
        "messages": [_adapt_gmail_message(m) for m in thread.get("messages", [])],
    }


def _scope_gmail_thread(thread: dict, *, account_key: str) -> dict:
    """Scope mailbox-local Gmail IDs without losing API IDs in memory."""
    raw_thread_id = (thread.get("id") or "").strip()
    messages = []
    for raw_msg in thread.get("messages", []):
        msg = dict(raw_msg)
        raw_message_id = (msg.get("id") or "").strip()
        msg["_gmail_raw_id"] = raw_message_id
        msg["id"] = _scoped_gmail_id(account_key, raw_message_id)
        msg["threadId"] = _scoped_gmail_id(account_key, raw_thread_id)
        messages.append(msg)
    return {
        "id": _scoped_gmail_id(account_key, raw_thread_id),
        "messages": messages,
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
    raw_headers = msg.get("headers") or (msg.get("payload") or {}).get("headers", [])
    if isinstance(raw_headers, dict):
        return {
            str(name).strip().lower(): str(value or "")
            for name, value in raw_headers.items()
            if str(name).strip()
        }
    for h in raw_headers:
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
    unmapped: dict[tuple[str, str], dict] = {}
    for result in results:
        combined.leads_considered += result.leads_considered
        combined.leads_with_email_threads += result.leads_with_email_threads
        combined.gmail_search_queries += result.gmail_search_queries
        combined.gmail_search_queries_at_cap += result.gmail_search_queries_at_cap
        combined.gmail_search_batches_split += result.gmail_search_batches_split
        combined.gmail_search_messages_seen += result.gmail_search_messages_seen
        combined.gmail_threads_fetched += result.gmail_threads_fetched
        combined.gmail_threads_matched += result.gmail_threads_matched
        combined.gmail_threads_ambiguous += result.gmail_threads_ambiguous
        combined.gmail_threads_deferred += result.gmail_threads_deferred
        combined.gmail_automated_messages_skipped += result.gmail_automated_messages_skipped
        combined.gmail_unsent_messages_skipped += result.gmail_unsent_messages_skipped
        combined.gmail_human_inbound_messages += result.gmail_human_inbound_messages
        combined.gmail_messages_created += result.gmail_messages_created
        combined.discovery_messages_scanned += result.discovery_messages_scanned
        combined.discovery_threads_selected += result.discovery_threads_selected
        combined.gmail_processed_thread_versions.update(
            result.gmail_processed_thread_versions
        )
        combined.note_emails_seen += result.note_emails_seen
        combined.note_emails_matched += result.note_emails_matched
        combined.note_emails_created_meetings += result.note_emails_created_meetings
        combined.note_emails_updated_meetings += result.note_emails_updated_meetings
        combined.note_emails_unchanged += result.note_emails_unchanged
        combined.note_emails_unmatched += result.note_emails_unmatched
        combined.unmatched_notes.extend(result.unmatched_notes)
        for candidate in result.unmapped_external_participants:
            candidate_key = (
                candidate.get("account_key", ""),
                candidate["email"],
            )
            previous = unmapped.get(candidate_key)
            if previous is None:
                unmapped[candidate_key] = dict(candidate)
                continue
            previous["thread_count"] = (
                int(previous.get("thread_count", 0))
                + int(candidate.get("thread_count", 0))
            )
            if candidate["last_inbound_at"] > previous["last_inbound_at"]:
                previous.update({
                    "display_name": candidate["display_name"],
                    "domain": candidate["domain"],
                    "last_inbound_at": candidate["last_inbound_at"],
                    "latest_thread_id": candidate["latest_thread_id"],
                })
    combined.unmapped_external_participants = sorted(
        unmapped.values(),
        key=lambda item: (item["last_inbound_at"], item["email"]),
        reverse=True,
    )
    return combined
