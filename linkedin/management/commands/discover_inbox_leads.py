"""Discover business-relevant leads from the LinkedIn message inbox.

This command crawls LinkedIn Messaging conversations for one or more configured
accounts, skips people already represented in our CRM, asks an LLM whether the
profile/thread is relevant to the selected campaign, and optionally imports the
qualified conversations as Lead + CONNECTED Deal + crm.Message rows.

Default mode is review-only. Pass --apply to write to the database.
"""
from __future__ import annotations

import logging
import os
import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable

import jinja2
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from pydantic import BaseModel, Field

from crm.models import Deal, Lead, Message
from linkedin.actions.conversations import parse_messages
from linkedin.actions.standalone_session import StandaloneLinkedInSession
from linkedin.api.client import PlaywrightLinkedinAPI
from linkedin.api.messaging import (
    fetch_conversations,
    fetch_conversations_by_category,
    fetch_messages,
)
from linkedin.api.messaging.utils import get_self_urn
from linkedin.db.messages import persist_thread
from linkedin.db.urls import public_id_to_url, url_to_public_id
from linkedin.enums import ProfileState
from linkedin.models import Campaign

logger = logging.getLogger(__name__)


ACCOUNTS = [
    ("primary", "LINKEDIN_USERNAME", "LINKEDIN_PASSWORD"),
    ("backfill", "BACKFILL_LINKEDIN_USERNAME", "BACKFILL_LINKEDIN_PASSWORD"),
]

SLEEP_MIN_SECONDS = 8
SLEEP_MAX_SECONDS = 22
MESSAGING_INBOX_URL = "https://www.linkedin.com/messaging/"


@dataclass(frozen=True)
class ParticipantInfo:
    member_urn: str
    public_id: str
    profile_url: str
    first_name: str
    last_name: str
    full_name: str
    headline: str


@dataclass(frozen=True)
class InboxConversation:
    conversation_urn: str
    participant: ParticipantInfo
    latest_at: datetime | None
    raw: dict


@dataclass(frozen=True)
class ApplyResult:
    status: str
    lead_id: int | None = None
    deal_id: int | None = None
    messages_created: int = 0
    reason: str = ""


class InboxLeadDecision(BaseModel):
    should_import: bool = Field(
        description="True only when this is a relevant Boundera FedRAMP/CMMC business prospect."
    )
    icp: str = Field(
        default="",
        description=(
            "Canonical ICP bucket for imports: CSPs, 3PAOs/Assessors, Advisors, "
            "Channel, CMMC Buyers, or CMMC Advisor/Channel. Empty for rejected "
            "conversations."
        ),
    )
    category: str = Field(
        description=(
            "For imports, the same value as icp. For rejects, a short category such as "
            "recruiter, job_seeker, vendor, random_networking, non_fedramp, spam, or unknown."
        )
    )
    reason: str = Field(description="Brief explanation for the import decision.")


def _make_session(label: str, env_user: str, env_pass: str) -> StandaloneLinkedInSession:
    return StandaloneLinkedInSession(
        env_username=env_user,
        env_password=env_pass,
        label=f"Inbox {label}",
    )


def _open_messaging_inbox(session: StandaloneLinkedInSession) -> None:
    """Place the visible browser on the Messaging inbox before API reads."""
    page = session.page
    page.goto(MESSAGING_INBOX_URL, wait_until="domcontentloaded", timeout=30_000)
    page.wait_for_timeout(random.randint(2_000, 5_000))


def _resolve_target_campaign(campaign_id: int | None) -> Campaign:
    if campaign_id is not None:
        try:
            return Campaign.objects.get(pk=campaign_id)
        except Campaign.DoesNotExist:
            raise CommandError(f"No Campaign with pk={campaign_id}")

    campaigns = list(Campaign.objects.all())
    if len(campaigns) == 1:
        return campaigns[0]
    if not campaigns:
        raise CommandError("No Campaigns exist. Create one first via Django Admin.")
    names = ", ".join(f"{c.pk}={c.name!r}" for c in campaigns)
    raise CommandError(
        f"Multiple Campaigns exist ({names}). Pass --campaign <id> to choose one.",
    )


def _configured_accounts(account: str | None) -> list[tuple[str, str, str]]:
    configured = [
        (label, env_user, env_pass)
        for (label, env_user, env_pass) in ACCOUNTS
        if os.getenv(env_user) and os.getenv(env_pass)
    ]
    if account is not None:
        configured = [c for c in configured if c[0] == account]
        if not configured:
            raise CommandError(
                f"--account {account!r} is not configured in .env "
                f"(missing username/password for that slot)."
            )
    if not configured:
        raise CommandError(
            "No LinkedIn accounts configured in .env. Set at least one of "
            "LINKEDIN_USERNAME/LINKEDIN_PASSWORD or "
            "BACKFILL_LINKEDIN_USERNAME/BACKFILL_LINKEDIN_PASSWORD."
        )
    return configured


def _localized_text(value) -> str:
    if isinstance(value, dict):
        return (value.get("text") or value.get("localized") or "").strip()
    return str(value or "").strip()


def _normalize_name(value: str) -> str:
    return " ".join((value or "").strip().lower().split())


def _participant_info(participant: dict) -> ParticipantInfo:
    member = (
        participant.get("participantType", {}).get("member", {})
        if isinstance(participant, dict)
        else {}
    )
    first = _localized_text(member.get("firstName"))
    last = _localized_text(member.get("lastName"))
    full = (_localized_text(member.get("fullName")) or f"{first} {last}").strip()
    profile_url = (member.get("profileUrl") or member.get("url") or "").strip()
    public_id = (
        member.get("publicIdentifier")
        or member.get("public_identifier")
        or url_to_public_id(profile_url)
        or ""
    )
    member_urn = (
        participant.get("hostIdentityUrn")
        or member.get("entityUrn")
        or member.get("urn")
        or ""
    )
    return ParticipantInfo(
        member_urn=member_urn,
        public_id=public_id,
        profile_url=profile_url,
        first_name=first,
        last_name=last,
        full_name=full,
        headline=_localized_text(member.get("headline")),
    )


def _other_participant(
    conversation: dict,
    *,
    self_urn: str,
    self_name: str,
) -> ParticipantInfo | None:
    participants = conversation.get("conversationParticipants") or []
    infos = [_participant_info(p) for p in participants]

    self_name_norm = _normalize_name(self_name)
    others = [
        info for info in infos
        if info.member_urn != self_urn
        and _normalize_name(info.full_name) != self_name_norm
    ]
    if len(others) != 1:
        return None
    return others[0]


def _conversation_box(raw: dict) -> dict:
    data = raw.get("data", {}) or {}
    return (
        data.get("messengerConversationsBySyncToken")
        or data.get("messengerConversationsByCategoryQuery")
        or {}
    )


def _conversation_elements(raw: dict) -> list[dict]:
    elements = _conversation_box(raw).get("elements", [])
    return elements if isinstance(elements, list) else []


def _find_token(value) -> str:
    if isinstance(value, dict):
        for key in (
            "nextCursor",
            "nextSyncToken",
            "syncToken",
            "paginationToken",
            "nextPageToken",
        ):
            token = value.get(key)
            if isinstance(token, str) and token:
                return token
        for child in value.values():
            token = _find_token(child)
            if token:
                return token
    elif isinstance(value, list):
        for child in value:
            token = _find_token(child)
            if token:
                return token
    return ""


def _next_sync_token(raw: dict) -> str:
    box = _conversation_box(raw)
    return _find_token(box)


def _next_cursor(raw: dict) -> str:
    box = _conversation_box(raw)
    metadata = box.get("metadata") if isinstance(box, dict) else None
    if isinstance(metadata, dict):
        token = metadata.get("nextCursor")
        if isinstance(token, str) and token:
            return token
    return ""


_TIMESTAMP_KEYS = {
    "lastActivityAt",
    "lastModifiedAt",
    "latestMessageAt",
    "deliveredAt",
    "createdAt",
    "timestamp",
}


def _timestamp_values(value) -> Iterable[int | float]:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in _TIMESTAMP_KEYS and isinstance(child, (int, float)):
                yield child
            else:
                yield from _timestamp_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from _timestamp_values(child)


def _coerce_timestamp(value: int | float) -> datetime | None:
    if value <= 0:
        return None
    seconds = value / 1000 if value > 10_000_000_000 else value
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.get_current_timezone())
    except (OSError, OverflowError, ValueError):
        return None


def _conversation_latest_at(conversation: dict) -> datetime | None:
    values = list(_timestamp_values(conversation))
    if not values:
        return None
    return _coerce_timestamp(max(values))


def _conversation_latest_millis(conversation: dict) -> int | None:
    values = list(_timestamp_values(conversation))
    if not values:
        return None
    value = max(values)
    return int(value if value > 10_000_000_000 else value * 1000)


def iter_inbox_conversations(
    api: PlaywrightLinkedinAPI,
    *,
    self_urn: str,
    self_name: str,
    since_days: int,
    max_pages: int,
    page_size: int,
    page_delay_seconds: float,
) -> Iterable[InboxConversation]:
    cutoff = timezone.now() - timedelta(days=since_days)
    sync_token = ""
    next_cursor = ""
    last_updated_before: int | None = None
    seen_tokens: set[str] = set()
    seen_conversations: set[str] = set()

    for page_num in range(max_pages):
        if page_num == 0:
            raw = fetch_conversations(
                api,
                sync_token=sync_token or None,
                count=page_size,
            )
        elif next_cursor:
            raw = fetch_conversations_by_category(
                api,
                next_cursor=next_cursor,
                count=page_size,
            )
        elif last_updated_before is not None:
            raw = fetch_conversations_by_category(
                api,
                last_updated_before=last_updated_before,
                count=page_size,
            )
        else:
            return
        elements = _conversation_elements(raw)
        if not elements:
            return

        stop_after_page = False
        page_latest_values: list[int] = []
        for conv in elements:
            conversation_urn = conv.get("entityUrn") or ""
            if not conversation_urn or conversation_urn in seen_conversations:
                continue
            seen_conversations.add(conversation_urn)

            latest_ms = _conversation_latest_millis(conv)
            if latest_ms is not None:
                page_latest_values.append(latest_ms)
            latest_at = _conversation_latest_at(conv)
            if latest_at and latest_at < cutoff:
                stop_after_page = True
                continue

            participant = _other_participant(
                conv,
                self_urn=self_urn,
                self_name=self_name,
            )
            if participant is None:
                logger.info("Skipping non-1:1 or self-only conversation %s", conversation_urn)
                continue

            yield InboxConversation(
                conversation_urn=conversation_urn,
                participant=participant,
                latest_at=latest_at,
                raw=conv,
            )

        if stop_after_page:
            return

        next_cursor = _next_cursor(raw)
        if next_cursor:
            if next_cursor in seen_tokens:
                return
            seen_tokens.add(next_cursor)
        elif page_num == 0 and page_latest_values:
            last_updated_before = min(page_latest_values)
        else:
            next_token = _next_sync_token(raw)
            if not next_token or next_token in seen_tokens or next_token == sync_token:
                return
            seen_tokens.add(next_token)
            sync_token = next_token

        if not next_cursor and last_updated_before is None and not sync_token:
            return

        if page_num + 1 < max_pages and page_delay_seconds > 0:
            time.sleep(page_delay_seconds)


def _urn_suffix(urn: str) -> str:
    return urn.rsplit(":", 1)[-1] if urn else ""


def _profile_lookup_candidates(participant: ParticipantInfo) -> list[str]:
    candidates = [
        participant.public_id,
        url_to_public_id(participant.profile_url),
        _urn_suffix(participant.member_urn),
    ]
    seen: set[str] = set()
    result: list[str] = []
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        result.append(candidate)
    return result


def _fallback_profile(participant: ParticipantInfo) -> dict | None:
    public_id = (
        participant.public_id
        or url_to_public_id(participant.profile_url)
        or _urn_suffix(participant.member_urn)
    )
    if not public_id and not participant.member_urn:
        return None
    return {
        "url": participant.profile_url or public_id_to_url(public_id),
        "urn": participant.member_urn,
        "full_name": participant.full_name,
        "first_name": participant.first_name,
        "last_name": participant.last_name,
        "headline": participant.headline,
        "summary": "",
        "public_identifier": public_id,
        "location_name": "",
        "geo": None,
        "industry": None,
        "positions": [],
        "educations": [],
        "country_code": None,
        "supported_locales": [],
        "connection_distance": None,
        "connection_degree": None,
    }


def _resolve_profile(
    api: PlaywrightLinkedinAPI,
    participant: ParticipantInfo,
) -> dict | None:
    for identity in _profile_lookup_candidates(participant):
        try:
            profile, _raw = api.get_profile(public_identifier=identity)
        except Exception:
            logger.warning("Inbox discovery profile lookup failed for %s", identity)
            continue
        if profile:
            return profile
    return _fallback_profile(participant)


def _profile_public_id(profile: dict, participant: ParticipantInfo) -> str:
    return (
        profile.get("public_identifier")
        or participant.public_id
        or url_to_public_id(profile.get("url") or "")
        or url_to_public_id(participant.profile_url)
        or _urn_suffix(participant.member_urn)
        or ""
    )


def existing_skip_reason(
    *,
    public_id: str,
    conversation_urn: str,
    member_urn: str = "",
) -> str:
    linkedin_url = public_id_to_url(public_id) if public_id else ""
    if linkedin_url and Lead.objects.filter(linkedin_url=linkedin_url).exists():
        return "existing lead"
    if public_id and Lead.objects.filter(public_identifier=public_id).exists():
        return "existing lead"
    if member_urn and Lead.objects.filter(description__contains=member_urn).exists():
        return "existing lead"
    if conversation_urn and Message.objects.filter(
        source=Message.Source.LINKEDIN,
        thread_external_id=conversation_urn,
    ).exists():
        return "existing thread"
    return ""


def _render_prompt(
    *,
    campaign: Campaign,
    profile: dict,
    parsed_messages: list[dict],
) -> str:
    from linkedin.conf import PROMPTS_DIR
    from linkedin.ml.profile_text import build_profile_text

    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(PROMPTS_DIR)))
    template = env.get_template("inbox_lead_relevance.j2")
    profile_text = build_profile_text({"profile": profile})
    messages = [
        {
            "timestamp": m.get("timestamp") or "",
            "sender": m.get("sender") or "",
            "text": m.get("text") or "",
        }
        for m in parsed_messages
    ]
    return template.render(
        destination_campaign=campaign.name,
        profile_text=profile_text,
        messages=messages,
    )


def classify_inbox_candidate(
    *,
    campaign: Campaign,
    profile: dict,
    parsed_messages: list[dict],
) -> InboxLeadDecision:
    from langchain_openai import ChatOpenAI

    from linkedin.conf import AI_MODEL, LLM_API_BASE, LLM_API_KEY

    if not LLM_API_KEY:
        raise CommandError("LLM_API_KEY is not set; cannot classify inbox leads.")
    llm = ChatOpenAI(
        model=AI_MODEL,
        temperature=0,
        api_key=LLM_API_KEY,
        base_url=LLM_API_BASE,
        timeout=60,
    )
    structured = llm.with_structured_output(InboxLeadDecision)
    return structured.invoke(
        _render_prompt(
            campaign=campaign,
            profile=profile,
            parsed_messages=parsed_messages,
        )
    )


def _normalize_icp(value: str) -> str:
    from linkedin.notifications.sheets import CSV_ICP_TO_LEAD_ICP, LEAD_ICP_BUCKETS

    raw = (value or "").strip()
    if raw in LEAD_ICP_BUCKETS:
        return raw
    return CSV_ICP_TO_LEAD_ICP.get(raw.lower(), "")


def _decision_icp(decision: InboxLeadDecision) -> str:
    return _normalize_icp(decision.icp) or _normalize_icp(decision.category)


def _store_profile_and_embedding(lead: Lead, public_id: str, profile: dict) -> None:
    from linkedin.db.leads import _update_lead_fields
    from linkedin.ml.embeddings import embed_profile

    _update_lead_fields(lead, profile)
    embed_profile(lead.pk, public_id, profile)


def apply_inbox_import(
    *,
    campaign: Campaign,
    public_id: str,
    profile: dict,
    parsed_messages: list[dict],
    conversation_urn: str,
    decision: InboxLeadDecision,
    sender: str,
) -> ApplyResult:
    icp = _decision_icp(decision)
    if not icp:
        return ApplyResult(status="skipped", reason="missing ICP")

    skip = existing_skip_reason(
        public_id=public_id,
        conversation_urn=conversation_urn,
        member_urn=profile.get("urn") or "",
    )
    if skip:
        return ApplyResult(status="skipped", reason=skip)

    linkedin_url = public_id_to_url(public_id)
    reason = f"Inbox discovery ({decision.category}): {decision.reason}".strip()

    with transaction.atomic():
        lead = Lead.objects.create(
            linkedin_url=linkedin_url,
            public_identifier=public_id,
            icp=icp,
        )
        _store_profile_and_embedding(lead, public_id, profile)
        deal = Deal.objects.create(
            lead=lead,
            campaign=campaign,
            state=ProfileState.CONNECTED,
            reason=reason,
        )

        messages_created = persist_thread(
            lead=lead,
            parsed=parsed_messages,
            thread_external_id=conversation_urn,
            outbound_senders={sender},
        )

        latest_inbound = Message.objects.filter(
            lead=lead,
            source=Message.Source.LINKEDIN,
            direction=Message.Direction.INBOUND,
        ).order_by("-sent_at").first()
        if latest_inbound:
            deal.last_reply_at = latest_inbound.sent_at
            deal.save(update_fields=["last_reply_at"])

    return ApplyResult(
        status="created",
        lead_id=lead.pk,
        deal_id=deal.pk,
        messages_created=messages_created,
    )


class Command(BaseCommand):
    help = "Discover relevant CRM leads from LinkedIn Messaging conversations."

    def add_arguments(self, parser):
        parser.add_argument(
            "--campaign",
            type=int,
            default=None,
            help="Campaign pk to write into. Required when multiple campaigns exist.",
        )
        parser.add_argument(
            "--account",
            choices=[label for label, _u, _p in ACCOUNTS],
            default=None,
            help="Restrict to one account slot ('primary' or 'backfill'). Default: all configured.",
        )
        parser.add_argument(
            "--since-days",
            type=int,
            default=90,
            help="Only inspect inbox conversations newer than this many days (default: 90).",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Cap new, non-duplicate conversations classified per account (0 = no cap).",
        )
        parser.add_argument(
            "--max-pages",
            type=int,
            default=10,
            help="Maximum inbox conversation pages to request per account.",
        )
        parser.add_argument(
            "--page-size",
            type=int,
            default=20,
            help="Requested LinkedIn conversations per page.",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Write qualified discoveries to Lead, Deal, and Message tables. Default is review-only.",
        )

    def handle(self, *args, **opts):
        from linkedin.notifications.slack import notify_on_error

        with notify_on_error(
            "discover_inbox_leads",
            context={
                "campaign": opts.get("campaign"),
                "account": opts.get("account"),
                "since_days": opts.get("since_days"),
                "apply": opts.get("apply", False),
            },
        ):
            self._handle_impl(*args, **opts)

    def _handle_impl(self, *args, **opts):
        campaign = _resolve_target_campaign(opts["campaign"])
        account = opts["account"]
        since_days = opts["since_days"]
        limit = opts["limit"]
        max_pages = opts["max_pages"]
        page_size = opts["page_size"]
        apply = opts["apply"]

        if since_days <= 0:
            raise CommandError("--since-days must be positive.")
        if max_pages <= 0:
            raise CommandError("--max-pages must be positive.")
        if page_size <= 0:
            raise CommandError("--page-size must be positive.")

        configured = _configured_accounts(account)
        self.stdout.write(
            f"Campaign: {campaign.name} (pk={campaign.pk}); mode: "
            f"{'APPLY' if apply else 'DRY RUN'}"
        )

        for label, env_user, env_pass in configured:
            username = os.getenv(env_user, "")
            self.stdout.write(f"\n=== {label} ({username}) ===")
            session = _make_session(label, env_user, env_pass)
            session.start()
            session.campaign = campaign
            self.stdout.write("Opening LinkedIn Messaging inbox.")
            _open_messaging_inbox(session)

            api = PlaywrightLinkedinAPI(session=session)
            self_urn = get_self_urn(api)
            sender = self._self_display_name(api)
            self.stdout.write(f"Logged in as {sender!r}.")

            self._run_account_pass(
                api=api,
                campaign=campaign,
                sender=sender,
                self_urn=self_urn,
                since_days=since_days,
                limit=limit,
                max_pages=max_pages,
                page_size=page_size,
                apply=apply,
            )

    @staticmethod
    def _self_display_name(api: PlaywrightLinkedinAPI) -> str:
        profile, _raw = api.get_profile(public_identifier="me")
        if not profile:
            raise CommandError("Could not fetch own profile via Voyager API.")
        return (
            profile.get("full_name")
            or f"{profile.get('first_name', '')} {profile.get('last_name', '')}".strip()
            or profile.get("public_identifier")
            or "unknown"
        )

    def _run_account_pass(
        self,
        *,
        api: PlaywrightLinkedinAPI,
        campaign: Campaign,
        sender: str,
        self_urn: str,
        since_days: int,
        limit: int,
        max_pages: int,
        page_size: int,
        apply: bool,
    ) -> None:
        seen = duplicate = classified = qualified = created = rejected = errors = 0

        conversations = iter_inbox_conversations(
            api,
            self_urn=self_urn,
            self_name=sender,
            since_days=since_days,
            max_pages=max_pages,
            page_size=page_size,
            page_delay_seconds=1.0,
        )

        for inbox_conv in conversations:
            seen += 1
            participant = inbox_conv.participant
            profile = _resolve_profile(api, participant)
            if not profile:
                errors += 1
                self.stdout.write(
                    self.style.WARNING(
                        f"  {inbox_conv.conversation_urn}: could not resolve participant profile"
                    )
                )
                continue

            public_id = _profile_public_id(profile, participant)
            if not public_id:
                errors += 1
                self.stdout.write(
                    self.style.WARNING(
                        f"  {inbox_conv.conversation_urn}: profile had no public identifier"
                    )
                )
                continue

            skip = existing_skip_reason(
                public_id=public_id,
                conversation_urn=inbox_conv.conversation_urn,
                member_urn=profile.get("urn") or participant.member_urn,
            )
            if skip:
                duplicate += 1
                self.stdout.write(f"  skip {public_id}: {skip}")
                continue

            if limit > 0 and classified >= limit:
                self.stdout.write(f"Limit reached ({limit}); stopping account pass.")
                break

            try:
                raw_messages = fetch_messages(api, inbox_conv.conversation_urn)
                parsed_messages = parse_messages(raw_messages)
                decision = classify_inbox_candidate(
                    campaign=campaign,
                    profile=profile,
                    parsed_messages=parsed_messages,
                )
            except Exception as exc:
                errors += 1
                logger.warning("Inbox discovery failed for %s: %s", public_id, exc)
                self.stdout.write(self.style.WARNING(f"  {public_id}: ERROR - {exc}"))
                continue

            classified += 1
            if not decision.should_import:
                rejected += 1
                self.stdout.write(
                    f"  reject {public_id}: {decision.category} - {decision.reason}"
                )
            else:
                icp = _decision_icp(decision)
                if not icp:
                    rejected += 1
                    self.stdout.write(
                        self.style.WARNING(
                            f"  reject {public_id}: missing ICP - {decision.reason}"
                        )
                    )
                    continue
                qualified += 1
                self.stdout.write(
                    f"  qualify {public_id}: {icp} - {decision.reason}"
                )
                if apply:
                    result = apply_inbox_import(
                        campaign=campaign,
                        public_id=public_id,
                        profile=profile,
                        parsed_messages=parsed_messages,
                        conversation_urn=inbox_conv.conversation_urn,
                        decision=decision,
                        sender=sender,
                    )
                    if result.status == "created":
                        created += 1
                        self.stdout.write(
                            f"    created lead={result.lead_id} deal={result.deal_id} "
                            f"messages={result.messages_created}"
                        )
                    else:
                        duplicate += 1
                        self.stdout.write(f"    skipped on apply: {result.reason}")

            if classified and (classified < limit or limit == 0):
                time.sleep(random.uniform(SLEEP_MIN_SECONDS, SLEEP_MAX_SECONDS))

        self.stdout.write(
            "Done. "
            f"seen={seen} duplicates={duplicate} classified={classified} "
            f"qualified={qualified} rejected={rejected} created={created} errors={errors}."
        )

        if apply:
            from linkedin.models import WorkflowRun
            from linkedin.operators import resolve_operator

            WorkflowRun.objects.create(
                name="discover-inbox-leads",
                operator=resolve_operator(sender),
                summary=(
                    f"seen={seen} duplicates={duplicate} classified={classified} "
                    f"qualified={qualified} rejected={rejected} created={created} errors={errors}"
                ),
                counts={
                    "seen": seen,
                    "duplicates": duplicate,
                    "classified": classified,
                    "qualified": qualified,
                    "rejected": rejected,
                    "created": created,
                    "errors": errors,
                },
            )
