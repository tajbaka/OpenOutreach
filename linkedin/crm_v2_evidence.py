"""Resolve persisted CRM activity into account-first v2 policy facts.

This module is deliberately read-only.  It is the bridge between channel data
(``Lead``, ``Message``, ``Meeting`` and existing human opportunity state) and
the pure admission policy in :mod:`linkedin.crm_v2_policy`.

Identity is conservative: an exact non-consumer email domain may join company
name variants, while a normalized company name joins the existing LinkedIn
rows that do not yet carry email.  Ambiguous fuzzy/name-body matching is never
used.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import Iterable, Mapping

from django.utils import timezone

from crm.models.sales import normalize_account_name
from linkedin.crm_v2_policy import (
    AccountPolicyDecision,
    AccountPolicyFacts,
    ConversationEvidence,
    evaluate_account,
)


PUBLIC_EMAIL_DOMAINS = frozenset({
    "aol.com",
    "gmail.com",
    "googlemail.com",
    "hotmail.com",
    "icloud.com",
    "live.com",
    "me.com",
    "msn.com",
    "outlook.com",
    "proton.me",
    "protonmail.com",
    "yahoo.com",
})

_AUTOMATED_SENDER_MARKERS = (
    "mailer-daemon",
    "no-reply",
    "noreply",
    "notifications@",
    "notification@",
)
_AUTOMATED_BODY_MARKERS = (
    "automatic reply",
    "automated response",
    "auto-reply",
    "out of office",
    "away from the office",
    "delivery status notification",
    "message could not be delivered",
)
_AUTOMATED_GMAIL_LABELS = frozenset({
    "CATEGORY_FORUMS",
    "CATEGORY_PROMOTIONS",
    "CATEGORY_SOCIAL",
    "CATEGORY_UPDATES",
})
_POLITE_DECLINE_MARKERS = (
    "not interested",
    "not a fit",
    "not the right time",
    "timing is not right",
    "no opportunity",
    "unable to participate",
    "do not contact",
    "don't contact",
    "please remove me",
    "unsubscribe",
    "no thank you",
    "no thanks",
)
_ACKNOWLEDGEMENTS = frozenset({
    "acknowledged",
    "cool",
    "got it",
    "great",
    "k",
    "ok",
    "okay",
    "sounds good",
    "thank you",
    "thanks",
    "thanks arian",
    "will do",
})
_SUBSTANTIVE_INTENT_MARKERS = (
    "calendar",
    "call",
    "demo",
    "discuss",
    "email me",
    "interested",
    "introduce",
    "meeting",
    "schedule",
    "send me",
    "tell me more",
    "what time",
    "when can",
)
_SPACE_RE = re.compile(r"\s+")
_WORD_RE = re.compile(r"[\w@.-]+", re.UNICODE)


@dataclass(frozen=True)
class ResolvedAccountEvidence:
    account_key: str
    account_name: str
    lead_ids: tuple[int, ...]
    opportunity_id: str
    owner: str
    key_contacts: tuple[str, ...]
    last_meaningful_touch: datetime | None
    reminder_target_lead_id: int | None
    trigger_message_id: int | None
    trigger_meeting_id: int | None
    facts: AccountPolicyFacts
    decision: AccountPolicyDecision
    owner_is_override: bool = False
    reminder_do_not_outreach: bool = False


@dataclass(frozen=True)
class _ConversationResolution:
    evidence: ConversationEvidence
    thread_key: tuple[int, str] | None = None


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def add(self, value: str) -> None:
        self.parent.setdefault(value, value)

    def find(self, value: str) -> str:
        parent = self.parent[value]
        if parent != value:
            self.parent[value] = self.find(parent)
        return self.parent[value]

    def union(self, left: str, right: str) -> None:
        self.add(left)
        self.add(right)
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root == right_root:
            return
        # Stable roots make identical snapshots byte-for-byte deterministic.
        first, second = sorted((left_root, right_root))
        self.parent[second] = first


def email_domain(value: str) -> str:
    email = (value or "").strip().casefold()
    if "@" not in email:
        return ""
    domain = email.rsplit("@", 1)[1].strip().strip(".")
    if domain.startswith("www."):
        domain = domain[4:]
    if not domain or domain in PUBLIC_EMAIL_DOMAINS:
        return ""
    return domain


def collect_account_evidence(
    *,
    sales_motion_accounts: Iterable[str] = (),
    manual_account_pins: Iterable[str] = (),
    owner_overrides: Mapping[str, str] | None = None,
    dont_send_lead_ids: Iterable[int] = (),
    now: datetime | None = None,
) -> list[ResolvedAccountEvidence]:
    """Build one deterministic policy decision per conservative account group."""
    from crm.models import Lead, Meeting, Message, Opportunity, OpportunityAction

    current_time = now or timezone.now()
    today = current_time.date()
    leads = list(Lead.objects.all().order_by("id"))
    lead_by_id = {lead.id: lead for lead in leads}
    explicit_dont_send_ids = {
        int(lead_id)
        for lead_id in dont_send_lead_ids
        if isinstance(lead_id, int) and not isinstance(lead_id, bool)
    }

    union = _UnionFind()
    lead_tokens: dict[int, tuple[str, ...]] = {}
    display_names: dict[str, list[str]] = defaultdict(list)

    opportunities = list(
        Opportunity.objects.select_related("account", "owner")
        .only(
            "id",
            "account_id",
            "account__id",
            "account__name",
            "account__domain",
            "owner_id",
            "owner__handle",
            "motion_key",
            "manual_pin",
            "human_revision",
            "source",
            "stage",
        )
        .prefetch_related("contacts__lead", "actions")
        .order_by("id")
    )

    # Domain is the primary account identity.  A normalized name may join a
    # domain only when every observed row with that name resolves to exactly
    # one business domain.  This prevents two unrelated "Acme" companies from
    # being transitively merged through their display name.
    domains_by_name: dict[str, set[str]] = defaultdict(set)
    for lead in leads:
        company_key = normalize_account_name(lead.company_name)
        domain = email_domain(lead.email)
        if company_key and domain:
            domains_by_name[company_key].add(domain)
    for opportunity in opportunities:
        company_key = normalize_account_name(opportunity.account.name)
        domain = email_domain(f"x@{opportunity.account.domain}")
        if company_key and domain:
            domains_by_name[company_key].add(domain)

    for lead in leads:
        company_key = normalize_account_name(lead.company_name)
        domain = email_domain(lead.email)
        name_token = f"name:{company_key}" if company_key else ""
        domain_token = f"domain:{domain}" if domain else ""
        if name_token:
            union.add(name_token)
            display_names[name_token].append(lead.company_name.strip())
        if domain_token:
            union.add(domain_token)
            display_names[domain_token].append(lead.company_name.strip() or domain)
        if name_token and len(domains_by_name[company_key]) == 1:
            union.union(name_token, f"domain:{next(iter(domains_by_name[company_key]))}")
        primary = domain_token or name_token
        if primary:
            lead_tokens[lead.id] = (primary,)

    opportunity_tokens: dict[object, tuple[str, ...]] = {}
    for opportunity in opportunities:
        account_name_key = normalize_account_name(opportunity.account.name)
        name_token = f"name:{account_name_key}" if account_name_key else ""
        account_domain = email_domain(f"x@{opportunity.account.domain}")
        domain_token = f"domain:{account_domain}" if account_domain else ""
        if account_name_key:
            union.add(name_token)
            display_names[name_token].append(opportunity.account.name)
        if domain_token:
            union.add(domain_token)
            display_names[domain_token].append(opportunity.account.name or account_domain)
        if name_token and len(domains_by_name[account_name_key]) == 1:
            union.union(name_token, f"domain:{next(iter(domains_by_name[account_name_key]))}")
        account_token = domain_token or name_token
        if account_token:
            opportunity_tokens[opportunity.id] = (account_token,)
        for contact in opportunity.contacts.all():
            for lead_token in lead_tokens.get(contact.lead_id, ()):
                if account_token:
                    union.union(account_token, lead_token)

    sales_motion_tokens: set[str] = set()
    for name in sales_motion_accounts:
        normalized = normalize_account_name(name)
        if not normalized:
            continue
        token = f"name:{normalized}"
        union.add(token)
        display_names[token].append((name or "").strip())
        sales_motion_tokens.add(token)

    manual_pin_tokens: set[str] = set()
    for name in manual_account_pins:
        normalized = normalize_account_name(name)
        if not normalized:
            continue
        token = f"name:{normalized}"
        union.add(token)
        display_names[token].append((name or "").strip())
        manual_pin_tokens.add(token)

    owner_override_tokens: dict[str, str] = {}
    for name, handle in (owner_overrides or {}).items():
        normalized = normalize_account_name(name)
        clean_handle = (handle or "").strip()
        if not normalized or not clean_handle:
            continue
        token = f"name:{normalized}"
        union.add(token)
        display_names[token].append((name or "").strip())
        existing = owner_override_tokens.get(token)
        if existing is not None and existing.casefold() != clean_handle.casefold():
            raise ValueError(f"Conflicting owner overrides for account {name!r}")
        owner_override_tokens[token] = clean_handle

    root_lead_ids: dict[str, list[int]] = defaultdict(list)
    for lead_id, tokens in lead_tokens.items():
        root_lead_ids[union.find(tokens[0])].append(lead_id)

    root_sales_motion: set[str] = {
        union.find(token) for token in sales_motion_tokens
    }
    root_manual_pins: set[str] = {
        union.find(token) for token in manual_pin_tokens
    }
    root_owner_overrides: dict[str, str] = {}
    for token, handle in owner_override_tokens.items():
        root = union.find(token)
        existing = root_owner_overrides.get(root)
        if existing is not None and existing.casefold() != handle.casefold():
            raise ValueError(
                f"Conflicting owner overrides resolve to account {_account_key(root)!r}"
            )
        root_owner_overrides[root] = handle
    root_opportunities: dict[str, list] = defaultdict(list)
    for opportunity in opportunities:
        tokens = opportunity_tokens.get(opportunity.id, ())
        if tokens and tokens[0] in union.parent:
            root_opportunities[union.find(tokens[0])].append(opportunity)
    roots = (
        set(root_lead_ids)
        | root_sales_motion
        | root_manual_pins
        | set(root_owner_overrides)
        | set(root_opportunities)
    )

    messages_by_root: dict[str, list] = defaultdict(list)
    for message in Message.objects.select_related("operator").order_by("sent_at", "id"):
        tokens = lead_tokens.get(message.lead_id)
        if tokens:
            messages_by_root[union.find(tokens[0])].append(message)

    meetings_by_root: dict[str, dict[int, object]] = defaultdict(dict)
    meetings = Meeting.objects.prefetch_related("participants", "notes").order_by(
        "start_at", "id",
    )
    for meeting in meetings:
        participant_ids = {meeting.lead_id}
        participant_ids.update(meeting.participants.values_list("id", flat=True))
        for lead_id in participant_ids:
            tokens = lead_tokens.get(lead_id)
            if tokens:
                meetings_by_root[union.find(tokens[0])][meeting.id] = meeting

    resolved: list[ResolvedAccountEvidence] = []
    for root in sorted(roots):
        lead_ids = tuple(sorted(root_lead_ids.get(root, ())))
        root_messages = messages_by_root.get(root, ())
        root_meetings = list(meetings_by_root.get(root, {}).values())
        root_opps = root_opportunities.get(root, ())
        opportunity = _canonical_opportunity(root_opps)

        gmail_resolution = _resolve_conversation(root_messages, source="gmail")
        linkedin_resolution = _resolve_conversation(
            root_messages,
            source="linkedin",
        )
        gmail = gmail_resolution.evidence
        linkedin = linkedin_resolution.evidence
        upcoming, completed = _meeting_dates(
            root_meetings,
            today=today,
            current_time=current_time,
        )
        action = _current_action(opportunity, OpportunityAction) if opportunity else None
        suppressed_lead_ids = {
            lead_id
            for lead_id in lead_ids
            if lead_by_id[lead_id].disqualified
            or lead_id in explicit_dont_send_ids
        }
        all_suppressed = bool(lead_ids) and set(lead_ids) <= suppressed_lead_ids
        latest_outbound = max(
            (
                message.sent_at
                for message in root_messages
                if message.direction == Message.Direction.OUTBOUND
                and not _is_automated(message)
            ),
            default=None,
        )
        latest_completed_meeting = max(
            (
                meeting for meeting in root_meetings
                if _meeting_is_completed(meeting, current_time=current_time)
            ),
            key=lambda meeting: (meeting.start_at, meeting.id),
            default=None,
        )
        latest_completed_meeting_at = (
            latest_completed_meeting.start_at
            if latest_completed_meeting is not None
            else None
        )
        meeting_participant_ids: set[int] = set()
        if latest_completed_meeting is not None:
            meeting_participant_ids.add(latest_completed_meeting.lead_id)
            meeting_participant_ids.update(
                latest_completed_meeting.participants.values_list("id", flat=True)
            )
        latest_meeting_contact_outbound = max(
            (
                message.sent_at
                for message in root_messages
                if message.lead_id in meeting_participant_ids
                and message.direction == Message.Direction.OUTBOUND
                and not _is_automated(message)
            ),
            default=None,
        )
        post_meeting_followup_required = bool(
            latest_completed_meeting_at
            and (
                latest_meeting_contact_outbound is None
                or latest_meeting_contact_outbound < latest_completed_meeting_at
            )
        )

        nonterminal_opportunity = bool(
            opportunity
            and opportunity.stage not in {
                Opportunity.Stage.CLOSED_WON,
                Opportunity.Stage.CLOSED_LOST,
            }
        )
        human_managed_opportunity = bool(
            nonterminal_opportunity
            and (
                opportunity.source in {
                    Opportunity.Source.MANUAL,
                    Opportunity.Source.SHEET,
                }
                or opportunity.human_revision > 0
            )
        )
        human_current_action = bool(
            nonterminal_opportunity
            and action is not None
            and _action_controls_policy(action)
        )

        account_key = _account_key(root)
        facts = AccountPolicyFacts(
            account_key=account_key,
            manual_pin=bool(
                (opportunity and opportunity.manual_pin)
                or root in root_manual_pins
            ),
            sales_motion_active=root in root_sales_motion,
            human_managed_opportunity=human_managed_opportunity,
            human_current_action=human_current_action,
            do_not_outreach=all_suppressed,
            upcoming_external_meeting_on=upcoming,
            latest_completed_external_meeting_on=completed,
            gmail=gmail,
            linkedin=linkedin,
            next_action_due_on=(
                action.due_on
                if action is not None and _action_controls_policy(action)
                else None
            ),
            waiting_until=(
                action.waiting_until
                if action is not None and _action_controls_policy(action)
                else None
            ),
            post_meeting_followup_required=post_meeting_followup_required,
        )
        decision = evaluate_account(facts, today=today)
        inferred_owner = _inferred_owner(
            root_messages,
            current_time=current_time,
        )
        owner_override = root_owner_overrides.get(root, "")
        resolved_owner = (
            owner_override
            or (
                opportunity.owner.handle
                if opportunity and opportunity.owner
                else inferred_owner
            )
        )
        target_lead_id, trigger_message_id, trigger_meeting_id = (
            _reminder_provenance(
                decision=decision,
                current_action=action,
                messages=root_messages,
                meetings=root_meetings,
                current_time=current_time,
                conversation_thread_keys={
                    "gmail": gmail_resolution.thread_key,
                    "linkedin": linkedin_resolution.thread_key,
                },
            )
        )
        reminder_do_not_outreach = bool(
            all_suppressed
            or (
                target_lead_id is not None
                and target_lead_id in suppressed_lead_ids
            )
        )
        account_name = _display_name(
            root,
            union=union,
            display_names=display_names,
            opportunity=opportunity,
        )
        meaningful_times = [
            value
            for value in (
                latest_completed_meeting_at,
                latest_outbound,
                max(
                    (
                        message.sent_at
                        for message in root_messages
                        if message.direction == Message.Direction.INBOUND
                        and not _is_automated(message)
                    ),
                    default=None,
                ),
            )
            if value is not None
        ]
        resolved.append(ResolvedAccountEvidence(
            account_key=account_key,
            account_name=account_name,
            lead_ids=lead_ids,
            opportunity_id=str(opportunity.id) if opportunity else "",
            owner=resolved_owner,
            key_contacts=_key_contacts(lead_ids, lead_by_id=lead_by_id),
            last_meaningful_touch=max(meaningful_times, default=None),
            reminder_target_lead_id=target_lead_id,
            trigger_message_id=trigger_message_id,
            trigger_meeting_id=trigger_meeting_id,
            facts=facts,
            decision=decision,
            owner_is_override=bool(owner_override),
            reminder_do_not_outreach=reminder_do_not_outreach,
        ))
    return resolved


def conversation_evidence(messages: Iterable, *, source: str) -> ConversationEvidence:
    """Return evidence from the newest qualifying exact contact/thread.

    Counts and timestamps that drive admission/reminders never combine an
    inbound from one contact/thread with an outbound from another.  When no
    thread qualifies, diagnostic counts still describe the account without
    falsely making it bidirectional.
    """
    return _resolve_conversation(messages, source=source).evidence


def _resolve_conversation(
    messages: Iterable,
    *,
    source: str,
) -> _ConversationResolution:
    source_messages = [message for message in messages if message.source == source]
    by_thread: dict[tuple[int, str], list] = defaultdict(list)
    for message in source_messages:
        thread_identity = (
            message.thread_external_id
            or f"lead:{message.lead_id}"
        )
        by_thread[(message.lead_id, thread_identity)].append(message)

    classified: list[tuple[tuple[int, str], ConversationEvidence]] = []
    for thread_key, thread_messages in by_thread.items():
        evidence = _classify_conversation(thread_messages, source=source)
        same_thread_bidirectional = bool(
            evidence.outbound_count > 0
            and (
                evidence.real_human_inbound_count > 0
                if source == "gmail"
                else evidence.substantive_inbound_count > 0
            )
        )
        classified.append((
            thread_key,
            replace(
                evidence,
                bidirectional_thread_count=int(same_thread_bidirectional),
            ),
        ))

    qualifying = [
        item for item in classified
        if (
            item[1].real_human_inbound_count > 0
            if source == "gmail"
            else item[1].is_substantive_bidirectional
        )
    ]
    if qualifying:
        thread_key, evidence = max(
            qualifying,
            key=lambda item: (_conversation_latest_at(item[1]), item[0]),
        )
        return _ConversationResolution(evidence=evidence, thread_key=thread_key)

    diagnostic = _classify_conversation(source_messages, source=source)
    return _ConversationResolution(
        evidence=replace(diagnostic, bidirectional_thread_count=0),
        thread_key=None,
    )


def _classify_conversation(messages: Iterable, *, source: str) -> ConversationEvidence:
    source_messages = list(messages)
    substantive_linkedin_ids: set[int] = set()
    if source == "linkedin":
        by_thread: dict[str, list] = defaultdict(list)
        for message in source_messages:
            thread_key = message.thread_external_id or f"lead:{message.lead_id}"
            by_thread[thread_key].append(message)
        for thread_messages in by_thread.values():
            ordered = sorted(thread_messages, key=lambda item: (item.sent_at, item.id or 0))
            inbound = [
                item for item in ordered
                if item.direction == item.Direction.INBOUND and not _is_automated(item)
            ]
            outbound_thread = [
                item for item in ordered
                if item.direction == item.Direction.OUTBOUND and not _is_automated(item)
            ]
            alternations = sum(
                left.direction != right.direction
                for left, right in zip(ordered, ordered[1:])
            )
            genuine_multi_turn = bool(
                len(inbound) >= 2
                and len(outbound_thread) >= 2
                and alternations >= 2
            )
            for item in inbound:
                body = _normalized_body(item.body)
                if _is_polite_decline(body) or _is_acknowledgement(body):
                    continue
                if _has_sales_intent(body) or (
                    genuine_multi_turn and _has_substantive_text(body)
                ):
                    substantive_linkedin_ids.add(item.id or id(item))
    human_inbound = []
    substantive = []
    acknowledgements = []
    declines = []
    automated = []
    connection_events = []
    outbound = []

    for message in source_messages:
        if message.direction == message.Direction.OUTBOUND:
            if not _is_automated(message):
                outbound.append(message)
            continue
        if _is_automated(message):
            automated.append(message)
            continue
        body = _normalized_body(message.body)
        human_inbound.append(message)
        if source == "linkedin" and not body:
            connection_events.append(message)
        elif _is_polite_decline(body):
            declines.append(message)
        elif _is_acknowledgement(body):
            acknowledgements.append(message)
        elif source == "gmail" or (message.id or id(message)) in substantive_linkedin_ids:
            substantive.append(message)

    return ConversationEvidence(
        human_inbound_count=len(human_inbound),
        substantive_inbound_count=len(substantive),
        acknowledgement_inbound_count=len(acknowledgements),
        polite_decline_inbound_count=len(declines),
        automated_inbound_count=len(automated),
        connection_event_count=len(connection_events),
        outbound_count=len(outbound),
        latest_human_inbound_on=_latest_date(human_inbound),
        latest_substantive_inbound_on=_latest_date(substantive),
        latest_outbound_on=_latest_date(outbound),
    )


def _conversation_latest_at(evidence: ConversationEvidence) -> float:
    values = [
        value for value in (
            evidence.latest_human_inbound_on,
            evidence.latest_substantive_inbound_on,
            evidence.latest_outbound_on,
        )
        if isinstance(value, datetime)
    ]
    return max((value.timestamp() for value in values), default=float("-inf"))


def _normalized_body(value: str) -> str:
    return _SPACE_RE.sub(" ", (value or "").strip().casefold())


def _is_automated(message) -> bool:
    sender = (message.sender or "").casefold()
    body = _normalized_body(message.body)
    raw = message.raw or {}
    headers = _raw_headers(raw)
    subject = headers.get("subject", "").casefold()
    labels = set(raw.get("labelIds") or ())
    automated_header = bool(
        headers.get("auto-submitted", "").casefold() not in {"", "no"}
        or headers.get("x-autoreply", "")
        or headers.get("x-autorespond", "")
        or headers.get("list-id", "")
        or headers.get("list-unsubscribe", "")
        or headers.get("precedence", "").casefold() in {"bulk", "junk", "list"}
    )
    return bool(
        automated_header
        or labels & _AUTOMATED_GMAIL_LABELS
        or any(marker in sender for marker in _AUTOMATED_SENDER_MARKERS)
        or any(marker in subject for marker in _AUTOMATED_BODY_MARKERS)
        or any(marker in body for marker in _AUTOMATED_BODY_MARKERS)
    )


def _raw_headers(raw: dict) -> dict[str, str]:
    value = raw.get("headers") or ()
    if isinstance(value, dict):
        return {
            str(key).strip().casefold(): str(item or "")
            for key, item in value.items()
        }
    headers = {}
    for item in value:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip().casefold()
        if name:
            headers[name] = str(item.get("value") or "")
    return headers


def _is_polite_decline(body: str) -> bool:
    return any(marker in body for marker in _POLITE_DECLINE_MARKERS)


def _is_acknowledgement(body: str) -> bool:
    collapsed = body.strip(" .,!?:;-—")
    return collapsed in _ACKNOWLEDGEMENTS


def _has_sales_intent(body: str) -> bool:
    return any(marker in body for marker in _SUBSTANTIVE_INTENT_MARKERS)


def _has_substantive_text(body: str) -> bool:
    words = _WORD_RE.findall(body)
    return len(words) >= 5 and len(body) >= 24


def _latest_date(messages: Iterable) -> datetime | None:
    return max((message.sent_at for message in messages), default=None)


def _meeting_is_completed(meeting, *, current_time: datetime) -> bool:
    """A persisted external meeting is evidence even without recorder notes."""
    return (meeting.end_at or meeting.start_at) <= current_time


def _meeting_dates(meetings: Iterable, *, today: date, current_time: datetime):
    upcoming = min(
        (meeting.start_at.date() for meeting in meetings if meeting.start_at >= current_time),
        default=None,
    )
    completed = max(
        (
            meeting.start_at.date()
            for meeting in meetings
            if _meeting_is_completed(meeting, current_time=current_time)
        ),
        default=None,
    )
    return upcoming, completed


def _canonical_opportunity(opportunities: Iterable):
    rows = list(opportunities)
    if not rows:
        return None
    primary = [row for row in rows if row.motion_key == "primary"]
    candidates = primary or rows
    # A human pin/revision is authoritative if old bootstrap duplicates exist.
    return sorted(
        candidates,
        key=lambda row: (not row.manual_pin, -row.human_revision, str(row.id)),
    )[0]


def _current_action(opportunity, action_model):
    if opportunity is None:
        return None
    return next(
        (
            action
            for action in opportunity.actions.all()
            if action.status in {
                action_model.Status.OPEN,
                action_model.Status.WAITING,
            }
        ),
        None,
    )


def _action_controls_policy(action) -> bool:
    """Only genuinely human action state is authoritative.

    The legacy CRM created ``system:`` tasks in bulk.  Treating those as human
    was the exact clutter failure v2 is replacing.  Human revisions and
    explicitly hand-created/non-generated keys still control policy; generated
    ``system:`` and ``v2:`` dates do not.
    """
    key = (action.idempotency_key or "").strip()
    return bool(
        action.human_revision > 0
        or not key
        or not key.startswith(("system:", "v2:"))
    )


def _account_key(root: str) -> str:
    return root.split(":", 1)[1]


def _display_name(root: str, *, union, display_names, opportunity) -> str:
    if opportunity is not None and opportunity.account.name.strip():
        return opportunity.account.name.strip()
    candidates: list[str] = []
    for token, names in display_names.items():
        if union.find(token) == root:
            candidates.extend(name for name in names if name)
    if candidates:
        counts = Counter(candidates)
        return sorted(counts, key=lambda name: (-counts[name], name.casefold()))[0]
    return _account_key(root)


def _key_contacts(lead_ids: Iterable[int], *, lead_by_id) -> tuple[str, ...]:
    contacts = []
    for lead_id in lead_ids:
        lead = lead_by_id[lead_id]
        name = f"{lead.first_name} {lead.last_name}".strip()
        contacts.append(name or lead.email or f"Lead {lead.id}")
    return tuple(sorted(set(contacts), key=str.casefold))


def _inferred_owner(messages: Iterable, *, current_time: datetime) -> str:
    """Return a sender owner only when recent persisted evidence is unambiguous."""
    cutoff = current_time - timedelta(days=365)
    handles = {
        message.operator.handle
        for message in messages
        if message.direction == message.Direction.OUTBOUND
        and message.sent_at >= cutoff
        and message.operator_id is not None
        and not _is_automated(message)
    }
    return next(iter(handles)) if len(handles) == 1 else ""


def _reminder_provenance(
    *,
    decision,
    current_action,
    messages,
    meetings,
    current_time,
    conversation_thread_keys,
) -> tuple[int | None, int | None, int | None]:
    """Resolve the exact contact/event behind a reminder without guessing."""
    if (
        current_action is not None
        and current_action.target_lead_id is not None
        and _action_controls_policy(current_action)
    ):
        return (
            current_action.target_lead_id,
            current_action.trigger_message_id,
            current_action.trigger_meeting_id,
        )

    reason = decision.reminder.reason_code.value
    if "gmail" in reason or "linkedin" in reason:
        source = "gmail" if "gmail" in reason else "linkedin"
        selected_thread = conversation_thread_keys.get(source)
        if selected_thread is None:
            return None, None, None
        direction = (
            "inbound"
            if "unanswered" in reason
            else "outbound"
        )
        candidates = [
            message
            for message in messages
            if message.source == source
            and message.direction == direction
            and (
                message.lead_id,
                message.thread_external_id or f"lead:{message.lead_id}",
            ) == selected_thread
            and not _is_automated(message)
            and (
                source == "gmail"
                or direction == "outbound"
                or _is_substantive_linkedin_message(message, messages)
            )
        ]
        if candidates:
            message = max(candidates, key=lambda item: (item.sent_at, item.id))
            return message.lead_id, message.id, None

    if "meeting" in reason:
        if "prep" in reason or "outside_prep" in reason:
            candidates = [
                meeting for meeting in meetings
                if meeting.start_at >= current_time
            ]
            meeting = min(
                candidates,
                key=lambda item: (item.start_at, item.id),
                default=None,
            )
        else:
            candidates = [
                meeting for meeting in meetings
                if _meeting_is_completed(meeting, current_time=current_time)
            ]
            meeting = max(
                candidates,
                key=lambda item: (item.start_at, item.id),
                default=None,
            )
        if meeting is not None:
            return meeting.lead_id, None, meeting.id
    return None, None, None


def _is_substantive_linkedin_message(message, messages) -> bool:
    thread_key = message.thread_external_id or f"lead:{message.lead_id}"
    thread = [
        item for item in messages
        if item.source == "linkedin"
        and (item.thread_external_id or f"lead:{item.lead_id}") == thread_key
    ]
    evidence = conversation_evidence(thread, source="linkedin")
    if not evidence.is_substantive_bidirectional:
        return False
    body = _normalized_body(message.body)
    return bool(
        not _is_acknowledgement(body)
        and not _is_polite_decline(body)
        and (_has_sales_intent(body) or _has_substantive_text(body))
    )
