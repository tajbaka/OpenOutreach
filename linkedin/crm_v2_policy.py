"""Pure account-first admission and reminder policy for the sales CRM.

The policy consumes already-resolved account evidence.  It deliberately knows
nothing about ``Lead``/``Deal`` rows, Google Sheets, Gmail APIs, or LinkedIn
ingestion.  That boundary keeps outreach state (including do-not-outreach)
separate from the question this module answers: should this *account* be in the
active sales CRM, and what reminder state should it have?

Callers are responsible for resolving contacts and channel events to one stable
``account_key`` and for classifying inbound messages.  The policy then applies
one deterministic precedence order:

1. manual pins and active Sales Motion records are authoritative;
2. real external meetings and human Gmail engagement are primary evidence;
3. LinkedIn is secondary and must be substantively bidirectional;
4. weak, outbound-only, or stale evidence remains People-only.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum

from linkedin.crm_action_policy import MEETING_PREP_DAYS


__all__ = (
    "COMPLETED_MEETING_ADMISSION_MAX_AGE_DAYS",
    "COMPLETED_MEETING_ACTION_MAX_AGE_DAYS",
    "GMAIL_ADMISSION_MAX_AGE_DAYS",
    "GMAIL_ACTION_MAX_AGE_DAYS",
    "LINKEDIN_ADMISSION_MAX_AGE_DAYS",
    "LINKEDIN_ACTION_MAX_AGE_DAYS",
    "AccountPolicyDecision",
    "AccountPolicyFacts",
    "AdmissionReasonCode",
    "AdmissionStatus",
    "ConversationEvidence",
    "EvidenceTier",
    "PolicyFlag",
    "Priority",
    "ReminderReasonCode",
    "ReminderRecommendation",
    "ReminderState",
    "evaluate_account",
    "recommend_reminder",
)


# Admission windows are deliberately channel-specific.  Enterprise/FedRAMP
# opportunities can remain real for months after a meeting or email thread,
# while LinkedIn-only engagement needs tighter recency to avoid recreating the
# historical prospecting ledger as an active CRM.
COMPLETED_MEETING_ADMISSION_MAX_AGE_DAYS = 180
GMAIL_ADMISSION_MAX_AGE_DAYS = 120
LINKEDIN_ADMISSION_MAX_AGE_DAYS = 90
COMPLETED_MEETING_ACTION_MAX_AGE_DAYS = 30
GMAIL_ACTION_MAX_AGE_DAYS = 30
LINKEDIN_ACTION_MAX_AGE_DAYS = 21


class AdmissionStatus(str, Enum):
    ACTIVE_ACCOUNT = "active_account"
    PEOPLE_ONLY = "people_only"


class EvidenceTier(str, Enum):
    AUTHORITATIVE = "authoritative"
    PRIMARY = "primary"
    SECONDARY = "secondary"
    WEAK = "weak"
    NONE = "none"


class Priority(str, Enum):
    URGENT = "urgent"
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"
    NONE = "none"


class AdmissionReasonCode(str, Enum):
    MANUAL_PIN = "manual_pin"
    SALES_MOTION_ACTIVE = "sales_motion_active"
    HUMAN_MANAGED_OPPORTUNITY = "human_managed_opportunity"
    HUMAN_CURRENT_ACTION = "human_current_action"
    UPCOMING_EXTERNAL_MEETING = "upcoming_external_meeting"
    RECENT_COMPLETED_EXTERNAL_MEETING = "recent_completed_external_meeting"
    RECENT_GMAIL_BIDIRECTIONAL_THREAD = "recent_gmail_bidirectional_thread"
    RECENT_GMAIL_HUMAN_INBOUND = "recent_gmail_human_inbound"
    RECENT_LINKEDIN_SUBSTANTIVE_BIDIRECTIONAL = (
        "recent_linkedin_substantive_bidirectional"
    )

    STALE_EXTERNAL_MEETING = "stale_external_meeting"
    STALE_GMAIL_HUMAN_ENGAGEMENT = "stale_gmail_human_engagement"
    STALE_LINKEDIN_SUBSTANTIVE_BIDIRECTIONAL = (
        "stale_linkedin_substantive_bidirectional"
    )
    GMAIL_ACKNOWLEDGEMENT_ONLY = "gmail_acknowledgement_only"
    GMAIL_POLITE_DECLINE_ONLY = "gmail_polite_decline_only"
    GMAIL_AUTOMATED_ONLY = "gmail_automated_only"
    GMAIL_OUTBOUND_ONLY = "gmail_outbound_only"
    LINKEDIN_ACKNOWLEDGEMENT_ONLY = "linkedin_acknowledgement_only"
    LINKEDIN_POLITE_DECLINE_ONLY = "linkedin_polite_decline_only"
    LINKEDIN_AUTOMATED_ONLY = "linkedin_automated_only"
    LINKEDIN_CONNECTION_EVENT_ONLY = "linkedin_connection_event_only"
    LINKEDIN_NOT_BIDIRECTIONAL = "linkedin_not_bidirectional"
    LINKEDIN_OUTBOUND_ONLY = "linkedin_outbound_only"
    NO_QUALIFYING_EVIDENCE = "no_qualifying_evidence"


class PolicyFlag(str, Enum):
    DO_NOT_OUTREACH = "do_not_outreach"


class ReminderState(str, Enum):
    NONE = "none"
    WAITING = "waiting"
    OVERDUE_NEXT_ACTION = "overdue_next_action"
    DUE_TODAY = "due_today"
    NEEDS_RESPONSE = "needs_response"
    MEETING_PREP = "meeting_prep"
    POST_MEETING_FOLLOWUP = "post_meeting_followup"
    FOLLOW_UP_DUE = "follow_up_due"
    REVIEW = "review"
    SCHEDULED_NEXT_ACTION = "scheduled_next_action"
    DEFINE_NEXT_STEP = "define_next_step"


class ReminderReasonCode(str, Enum):
    ACCOUNT_NOT_ACTIVE = "account_not_active"
    WAITING_UNTIL = "waiting_until"
    NEXT_ACTION_OVERDUE = "next_action_overdue"
    NEXT_ACTION_DUE_TODAY = "next_action_due_today"
    UNANSWERED_GMAIL_HUMAN_INBOUND = "unanswered_gmail_human_inbound"
    UNANSWERED_LINKEDIN_SUBSTANTIVE_INBOUND = (
        "unanswered_linkedin_substantive_inbound"
    )
    EXTERNAL_MEETING_IN_PREP_WINDOW = "external_meeting_in_prep_window"
    EXTERNAL_MEETING_OUTSIDE_PREP_WINDOW = "external_meeting_outside_prep_window"
    POST_MEETING_FOLLOWUP_REQUIRED = "post_meeting_followup_required"
    WAITING_FOR_GMAIL_REPLY = "waiting_for_gmail_reply"
    GMAIL_FOLLOW_UP_DUE = "gmail_follow_up_due"
    WAITING_FOR_LINKEDIN_REPLY = "waiting_for_linkedin_reply"
    LINKEDIN_FOLLOW_UP_DUE = "linkedin_follow_up_due"
    OLD_GMAIL_CONVERSATION_REVIEW = "old_gmail_conversation_review"
    OLD_LINKEDIN_CONVERSATION_REVIEW = "old_linkedin_conversation_review"
    OLD_MEETING_FOLLOWUP_REVIEW = "old_meeting_followup_review"
    NEXT_ACTION_SCHEDULED = "next_action_scheduled"
    ACTIVE_ACCOUNT_MISSING_NEXT_STEP = "active_account_missing_next_step"


@dataclass(frozen=True)
class ConversationEvidence:
    """Account-aggregated facts for one message source.

    ``human_inbound_count`` excludes automated responses and connection-system
    events.  ``substantive_inbound_count``, acknowledgement, and decline counts
    are non-overlapping subsets of it.  A remaining human inbound is a real
    human response that is neither a bare acknowledgement nor a polite decline.

    LinkedIn admission specifically requires at least one substantive inbound
    and at least one outbound.  Gmail admission accepts a real human inbound;
    having outbound in the same exact contact/thread raises the evidence
    reason to bidirectional. ``bidirectional_thread_count`` is populated by
    the database evidence resolver; ``None`` preserves the simple constructor
    contract used by pure policy callers.
    """

    human_inbound_count: int = 0
    substantive_inbound_count: int = 0
    acknowledgement_inbound_count: int = 0
    polite_decline_inbound_count: int = 0
    automated_inbound_count: int = 0
    connection_event_count: int = 0
    outbound_count: int = 0
    bidirectional_thread_count: int | None = None
    # Datetimes preserve same-day message ordering. Date-only exports remain
    # valid inputs and are treated as midnight on that date.
    latest_human_inbound_on: date | datetime | None = None
    latest_substantive_inbound_on: date | datetime | None = None
    latest_outbound_on: date | datetime | None = None

    def __post_init__(self) -> None:
        counts = {
            "human_inbound_count": self.human_inbound_count,
            "substantive_inbound_count": self.substantive_inbound_count,
            "acknowledgement_inbound_count": self.acknowledgement_inbound_count,
            "polite_decline_inbound_count": self.polite_decline_inbound_count,
            "automated_inbound_count": self.automated_inbound_count,
            "connection_event_count": self.connection_event_count,
            "outbound_count": self.outbound_count,
        }
        negative = [name for name, value in counts.items() if value < 0]
        if negative:
            raise ValueError(f"Conversation counts cannot be negative: {negative}")
        if (
            self.bidirectional_thread_count is not None
            and self.bidirectional_thread_count < 0
        ):
            raise ValueError("bidirectional_thread_count cannot be negative")
        classified_human = (
            self.substantive_inbound_count
            + self.acknowledgement_inbound_count
            + self.polite_decline_inbound_count
        )
        if classified_human > self.human_inbound_count:
            raise ValueError(
                "Substantive, acknowledgement, and decline counts must be "
                "non-overlapping subsets of human_inbound_count."
            )
        if self.human_inbound_count and self.latest_human_inbound_on is None:
            raise ValueError(
                "latest_human_inbound_on is required when human inbound exists."
            )
        if (
            self.substantive_inbound_count
            and self.latest_substantive_inbound_on is None
        ):
            raise ValueError(
                "latest_substantive_inbound_on is required when substantive "
                "inbound exists."
            )
        if self.outbound_count and self.latest_outbound_on is None:
            raise ValueError(
                "latest_outbound_on is required when outbound messages exist."
            )

    @property
    def real_human_inbound_count(self) -> int:
        return self.human_inbound_count - (
            self.acknowledgement_inbound_count
            + self.polite_decline_inbound_count
        )

    @property
    def is_bidirectional(self) -> bool:
        if self.bidirectional_thread_count is not None:
            return self.bidirectional_thread_count > 0
        return self.real_human_inbound_count > 0 and self.outbound_count > 0

    @property
    def is_substantive_bidirectional(self) -> bool:
        if self.bidirectional_thread_count is not None:
            return (
                self.bidirectional_thread_count > 0
                and self.substantive_inbound_count > 0
            )
        return self.substantive_inbound_count > 0 and self.outbound_count > 0


@dataclass(frozen=True)
class AccountPolicyFacts:
    """Resolved evidence and human state for exactly one account."""

    account_key: str
    manual_pin: bool = False
    sales_motion_active: bool = False
    human_managed_opportunity: bool = False
    human_current_action: bool = False
    do_not_outreach: bool = False
    upcoming_external_meeting_on: date | None = None
    latest_completed_external_meeting_on: date | None = None
    gmail: ConversationEvidence = field(default_factory=ConversationEvidence)
    linkedin: ConversationEvidence = field(default_factory=ConversationEvidence)
    next_action_due_on: date | None = None
    waiting_until: date | None = None
    post_meeting_followup_required: bool = False

    def __post_init__(self) -> None:
        if not self.account_key.strip():
            raise ValueError("account_key must be a non-empty stable account identity.")


@dataclass(frozen=True)
class ReminderRecommendation:
    state: ReminderState
    reason_code: ReminderReasonCode
    should_create_reminder: bool
    due_on: date | None
    priority: Priority
    automated_outreach_allowed: bool


@dataclass(frozen=True)
class AccountPolicyDecision:
    account_key: str
    status: AdmissionStatus
    primary_reason_code: AdmissionReasonCode
    reason_codes: tuple[AdmissionReasonCode, ...]
    evidence_tier: EvidenceTier
    confidence_score: int
    priority: Priority
    automated_outreach_allowed: bool
    flags: tuple[PolicyFlag, ...]
    reminder: ReminderRecommendation

    @property
    def admitted(self) -> bool:
        return self.status == AdmissionStatus.ACTIVE_ACCOUNT


@dataclass(frozen=True)
class _EvidenceCandidate:
    reason: AdmissionReasonCode
    tier: EvidenceTier
    confidence: int
    observed_on: date | None


_QUALIFYING_REASON_ORDER = {
    reason: index
    for index, reason in enumerate((
        AdmissionReasonCode.MANUAL_PIN,
        AdmissionReasonCode.SALES_MOTION_ACTIVE,
        AdmissionReasonCode.HUMAN_MANAGED_OPPORTUNITY,
        AdmissionReasonCode.HUMAN_CURRENT_ACTION,
        AdmissionReasonCode.UPCOMING_EXTERNAL_MEETING,
        AdmissionReasonCode.RECENT_COMPLETED_EXTERNAL_MEETING,
        AdmissionReasonCode.RECENT_GMAIL_BIDIRECTIONAL_THREAD,
        AdmissionReasonCode.RECENT_GMAIL_HUMAN_INBOUND,
        AdmissionReasonCode.RECENT_LINKEDIN_SUBSTANTIVE_BIDIRECTIONAL,
    ))
}

_DIAGNOSTIC_REASON_ORDER = {
    reason: index
    for index, reason in enumerate((
        AdmissionReasonCode.STALE_EXTERNAL_MEETING,
        AdmissionReasonCode.STALE_GMAIL_HUMAN_ENGAGEMENT,
        AdmissionReasonCode.STALE_LINKEDIN_SUBSTANTIVE_BIDIRECTIONAL,
        AdmissionReasonCode.GMAIL_ACKNOWLEDGEMENT_ONLY,
        AdmissionReasonCode.GMAIL_POLITE_DECLINE_ONLY,
        AdmissionReasonCode.GMAIL_AUTOMATED_ONLY,
        AdmissionReasonCode.GMAIL_OUTBOUND_ONLY,
        AdmissionReasonCode.LINKEDIN_ACKNOWLEDGEMENT_ONLY,
        AdmissionReasonCode.LINKEDIN_POLITE_DECLINE_ONLY,
        AdmissionReasonCode.LINKEDIN_AUTOMATED_ONLY,
        AdmissionReasonCode.LINKEDIN_CONNECTION_EVENT_ONLY,
        AdmissionReasonCode.LINKEDIN_NOT_BIDIRECTIONAL,
        AdmissionReasonCode.LINKEDIN_OUTBOUND_ONLY,
        AdmissionReasonCode.NO_QUALIFYING_EVIDENCE,
    ))
}


def evaluate_account(
    facts: AccountPolicyFacts,
    *,
    today: date,
) -> AccountPolicyDecision:
    """Return one deterministic admission, priority, and reminder decision."""
    _validate_temporal_facts(facts, today=today)
    qualifying = _qualifying_candidates(facts, today=today)
    flags = (
        (PolicyFlag.DO_NOT_OUTREACH,)
        if facts.do_not_outreach
        else ()
    )
    automated_outreach_allowed = not facts.do_not_outreach

    if qualifying:
        qualifying.sort(key=lambda item: _QUALIFYING_REASON_ORDER[item.reason])
        primary = qualifying[0]
        reasons = tuple(candidate.reason for candidate in qualifying)
        reminder = recommend_reminder(
            facts,
            today=today,
            admitted=True,
            evidence_tier=primary.tier,
        )
        return AccountPolicyDecision(
            account_key=facts.account_key,
            status=AdmissionStatus.ACTIVE_ACCOUNT,
            primary_reason_code=primary.reason,
            reason_codes=reasons,
            evidence_tier=primary.tier,
            confidence_score=max(candidate.confidence for candidate in qualifying),
            priority=reminder.priority,
            automated_outreach_allowed=automated_outreach_allowed,
            flags=flags,
            reminder=reminder,
        )

    diagnostics = _diagnostic_candidates(facts, today=today)
    diagnostics.sort(key=lambda item: _DIAGNOSTIC_REASON_ORDER[item.reason])
    primary = diagnostics[0]
    reminder = recommend_reminder(
        facts,
        today=today,
        admitted=False,
        evidence_tier=primary.tier,
    )
    return AccountPolicyDecision(
        account_key=facts.account_key,
        status=AdmissionStatus.PEOPLE_ONLY,
        primary_reason_code=primary.reason,
        reason_codes=tuple(candidate.reason for candidate in diagnostics),
        evidence_tier=primary.tier,
        confidence_score=max(candidate.confidence for candidate in diagnostics),
        priority=Priority.NONE,
        automated_outreach_allowed=automated_outreach_allowed,
        flags=flags,
        reminder=reminder,
    )


def recommend_reminder(
    facts: AccountPolicyFacts,
    *,
    today: date,
    admitted: bool,
    evidence_tier: EvidenceTier,
) -> ReminderRecommendation:
    """Recommend one reminder state without mutating or routing anything.

    A future human waiting date is authoritative.  Otherwise explicit due
    dates win, followed by unanswered human inbound, meeting preparation,
    post-meeting follow-up, and finally the absence of a next step.
    """
    allowed = not facts.do_not_outreach
    if not admitted:
        return _reminder(
            ReminderState.NONE,
            ReminderReasonCode.ACCOUNT_NOT_ACTIVE,
            priority=Priority.NONE,
            allowed=allowed,
        )

    if facts.waiting_until is not None and facts.waiting_until > today:
        return _reminder(
            ReminderState.WAITING,
            ReminderReasonCode.WAITING_UNTIL,
            due_on=facts.waiting_until,
            priority=Priority.LOW,
            allowed=allowed,
        )

    if facts.next_action_due_on is not None:
        if facts.next_action_due_on < today:
            return _reminder(
                ReminderState.OVERDUE_NEXT_ACTION,
                ReminderReasonCode.NEXT_ACTION_OVERDUE,
                due_on=facts.next_action_due_on,
                priority=Priority.URGENT,
                allowed=allowed,
            )
        if facts.next_action_due_on == today:
            return _reminder(
                ReminderState.DUE_TODAY,
                ReminderReasonCode.NEXT_ACTION_DUE_TODAY,
                due_on=today,
                priority=Priority.HIGH,
                allowed=allowed,
            )

    conversation_reminder = _latest_conversation_reminder(
        gmail=facts.gmail,
        linkedin=facts.linkedin,
        today=today,
        allowed=allowed,
    )
    if (
        conversation_reminder is not None
        and conversation_reminder.state in {
            ReminderState.NEEDS_RESPONSE,
            ReminderState.FOLLOW_UP_DUE,
        }
    ):
        return conversation_reminder

    if facts.upcoming_external_meeting_on is not None:
        days_to_meeting = (facts.upcoming_external_meeting_on - today).days
        if days_to_meeting <= MEETING_PREP_DAYS:
            meeting_reminder = _reminder(
                ReminderState.MEETING_PREP,
                ReminderReasonCode.EXTERNAL_MEETING_IN_PREP_WINDOW,
                due_on=today,
                priority=Priority.HIGH,
                allowed=allowed,
            )
        else:
            meeting_reminder = _reminder(
                ReminderState.WAITING,
                ReminderReasonCode.EXTERNAL_MEETING_OUTSIDE_PREP_WINDOW,
                due_on=facts.upcoming_external_meeting_on - timedelta(
                    days=MEETING_PREP_DAYS,
                ),
                priority=Priority.LOW,
                allowed=allowed,
            )
        if (
            conversation_reminder is not None
            and conversation_reminder.state == ReminderState.WAITING
            and conversation_reminder.due_on is not None
            and meeting_reminder.due_on is not None
            and conversation_reminder.due_on < meeting_reminder.due_on
        ):
            return conversation_reminder
        return meeting_reminder

    if facts.post_meeting_followup_required:
        meeting_on = facts.latest_completed_external_meeting_on
        if (
            meeting_on is not None
            and (today - meeting_on).days <= COMPLETED_MEETING_ACTION_MAX_AGE_DAYS
        ):
            return _reminder(
                ReminderState.POST_MEETING_FOLLOWUP,
                ReminderReasonCode.POST_MEETING_FOLLOWUP_REQUIRED,
                due_on=today,
                priority=Priority.HIGH,
                allowed=allowed,
            )
        return _review_reminder(
            facts,
            today=today,
            state=ReminderState.REVIEW,
            reason=ReminderReasonCode.OLD_MEETING_FOLLOWUP_REVIEW,
            allowed=allowed,
        )

    if conversation_reminder is not None:
        if conversation_reminder.state == ReminderState.REVIEW:
            return _review_reminder(
                facts,
                today=today,
                state=conversation_reminder.state,
                reason=conversation_reminder.reason_code,
                allowed=allowed,
            )
        return conversation_reminder

    if facts.next_action_due_on is not None:
        return _reminder(
            ReminderState.SCHEDULED_NEXT_ACTION,
            ReminderReasonCode.NEXT_ACTION_SCHEDULED,
            due_on=facts.next_action_due_on,
            priority=Priority.LOW,
            allowed=allowed,
        )

    return _reminder(
        ReminderState.DEFINE_NEXT_STEP,
        ReminderReasonCode.ACTIVE_ACCOUNT_MISSING_NEXT_STEP,
        due_on=today,
        priority=(
            Priority.LOW
            if evidence_tier == EvidenceTier.SECONDARY
            else Priority.NORMAL
        ),
        allowed=allowed,
    )


def _qualifying_candidates(
    facts: AccountPolicyFacts,
    *,
    today: date,
) -> list[_EvidenceCandidate]:
    candidates: list[_EvidenceCandidate] = []
    if facts.manual_pin:
        candidates.append(_EvidenceCandidate(
            AdmissionReasonCode.MANUAL_PIN,
            EvidenceTier.AUTHORITATIVE,
            100,
            None,
        ))
    if facts.sales_motion_active:
        candidates.append(_EvidenceCandidate(
            AdmissionReasonCode.SALES_MOTION_ACTIVE,
            EvidenceTier.AUTHORITATIVE,
            100,
            None,
        ))
    if facts.human_managed_opportunity:
        candidates.append(_EvidenceCandidate(
            AdmissionReasonCode.HUMAN_MANAGED_OPPORTUNITY,
            EvidenceTier.AUTHORITATIVE,
            100,
            None,
        ))
    if facts.human_current_action:
        candidates.append(_EvidenceCandidate(
            AdmissionReasonCode.HUMAN_CURRENT_ACTION,
            EvidenceTier.AUTHORITATIVE,
            100,
            None,
        ))
    if facts.upcoming_external_meeting_on is not None:
        candidates.append(_EvidenceCandidate(
            AdmissionReasonCode.UPCOMING_EXTERNAL_MEETING,
            EvidenceTier.PRIMARY,
            98,
            facts.upcoming_external_meeting_on,
        ))
    if _is_recent(
        facts.latest_completed_external_meeting_on,
        today=today,
        max_age_days=COMPLETED_MEETING_ADMISSION_MAX_AGE_DAYS,
    ):
        candidates.append(_EvidenceCandidate(
            AdmissionReasonCode.RECENT_COMPLETED_EXTERNAL_MEETING,
            EvidenceTier.PRIMARY,
            95,
            facts.latest_completed_external_meeting_on,
        ))

    gmail = facts.gmail
    if gmail.real_human_inbound_count > 0 and _is_recent(
        gmail.latest_human_inbound_on,
        today=today,
        max_age_days=GMAIL_ADMISSION_MAX_AGE_DAYS,
    ):
        candidates.append(_EvidenceCandidate(
            (
                AdmissionReasonCode.RECENT_GMAIL_BIDIRECTIONAL_THREAD
                if gmail.is_bidirectional
                else AdmissionReasonCode.RECENT_GMAIL_HUMAN_INBOUND
            ),
            EvidenceTier.PRIMARY,
            93 if gmail.is_bidirectional else 90,
            gmail.latest_human_inbound_on,
        ))

    linkedin = facts.linkedin
    if linkedin.is_substantive_bidirectional and _is_recent(
        linkedin.latest_substantive_inbound_on,
        today=today,
        max_age_days=LINKEDIN_ADMISSION_MAX_AGE_DAYS,
    ):
        candidates.append(_EvidenceCandidate(
            AdmissionReasonCode.RECENT_LINKEDIN_SUBSTANTIVE_BIDIRECTIONAL,
            EvidenceTier.SECONDARY,
            72,
            linkedin.latest_substantive_inbound_on,
        ))
    return candidates


def _diagnostic_candidates(
    facts: AccountPolicyFacts,
    *,
    today: date,
) -> list[_EvidenceCandidate]:
    candidates: list[_EvidenceCandidate] = []
    if (
        facts.latest_completed_external_meeting_on is not None
        and not _is_recent(
            facts.latest_completed_external_meeting_on,
            today=today,
            max_age_days=COMPLETED_MEETING_ADMISSION_MAX_AGE_DAYS,
        )
    ):
        candidates.append(_EvidenceCandidate(
            AdmissionReasonCode.STALE_EXTERNAL_MEETING,
            EvidenceTier.PRIMARY,
            35,
            facts.latest_completed_external_meeting_on,
        ))

    gmail = facts.gmail
    if gmail.real_human_inbound_count > 0:
        if not _is_recent(
            gmail.latest_human_inbound_on,
            today=today,
            max_age_days=GMAIL_ADMISSION_MAX_AGE_DAYS,
        ):
            candidates.append(_EvidenceCandidate(
                AdmissionReasonCode.STALE_GMAIL_HUMAN_ENGAGEMENT,
                EvidenceTier.PRIMARY,
                35,
                gmail.latest_human_inbound_on,
            ))
    elif _is_acknowledgement_only(gmail):
        candidates.append(_weak(AdmissionReasonCode.GMAIL_ACKNOWLEDGEMENT_ONLY))
    elif _is_decline_only(gmail):
        candidates.append(_weak(AdmissionReasonCode.GMAIL_POLITE_DECLINE_ONLY))
    elif gmail.automated_inbound_count:
        candidates.append(_weak(AdmissionReasonCode.GMAIL_AUTOMATED_ONLY))
    elif gmail.outbound_count:
        candidates.append(_weak(AdmissionReasonCode.GMAIL_OUTBOUND_ONLY))

    linkedin = facts.linkedin
    if linkedin.is_substantive_bidirectional:
        if not _is_recent(
            linkedin.latest_substantive_inbound_on,
            today=today,
            max_age_days=LINKEDIN_ADMISSION_MAX_AGE_DAYS,
        ):
            candidates.append(_EvidenceCandidate(
                AdmissionReasonCode.STALE_LINKEDIN_SUBSTANTIVE_BIDIRECTIONAL,
                EvidenceTier.SECONDARY,
                20,
                linkedin.latest_substantive_inbound_on,
            ))
    elif _is_acknowledgement_only(linkedin):
        candidates.append(_weak(AdmissionReasonCode.LINKEDIN_ACKNOWLEDGEMENT_ONLY))
    elif _is_decline_only(linkedin):
        candidates.append(_weak(AdmissionReasonCode.LINKEDIN_POLITE_DECLINE_ONLY))
    elif linkedin.automated_inbound_count:
        candidates.append(_weak(AdmissionReasonCode.LINKEDIN_AUTOMATED_ONLY))
    elif linkedin.connection_event_count:
        candidates.append(_weak(AdmissionReasonCode.LINKEDIN_CONNECTION_EVENT_ONLY))
    elif linkedin.substantive_inbound_count:
        candidates.append(_weak(AdmissionReasonCode.LINKEDIN_NOT_BIDIRECTIONAL))
    elif linkedin.outbound_count:
        candidates.append(_weak(AdmissionReasonCode.LINKEDIN_OUTBOUND_ONLY))

    if not candidates:
        candidates.append(_EvidenceCandidate(
            AdmissionReasonCode.NO_QUALIFYING_EVIDENCE,
            EvidenceTier.NONE,
            0,
            None,
        ))
    return candidates


def _is_recent(
    observed_on: date | datetime | None,
    *,
    today: date,
    max_age_days: int,
) -> bool:
    if observed_on is None:
        return False
    return 0 <= (today - _event_date(observed_on)).days <= max_age_days


def _latest_conversation_reminder(
    *,
    gmail: ConversationEvidence,
    linkedin: ConversationEvidence,
    today: date,
    allowed: bool,
) -> ReminderRecommendation | None:
    candidates = []
    for source, evidence in (("gmail", gmail), ("linkedin", linkedin)):
        candidate = _conversation_reminder(
            evidence,
            today=today,
            source=source,
            allowed=allowed,
        )
        if candidate is not None:
            candidates.append(candidate)
    if not candidates:
        return None
    # The account's newest real conversation controls ball-on-court. Gmail is
    # the deterministic tie-breaker when only day-level evidence is available.
    candidates.sort(
        key=lambda item: (
            _event_order_key(item[0]),
            item[1] == "gmail",
        ),
        reverse=True,
    )
    return candidates[0][2]


def _conversation_reminder(
    evidence: ConversationEvidence,
    *,
    today: date,
    source: str,
    allowed: bool,
) -> tuple[date | datetime, str, ReminderRecommendation] | None:
    inbound = (
        evidence.latest_human_inbound_on
        if source == "gmail"
        else evidence.latest_substantive_inbound_on
    )
    if source == "gmail" and evidence.real_human_inbound_count <= 0:
        return None
    if source == "linkedin" and not evidence.is_substantive_bidirectional:
        return None
    if inbound is None:
        return None

    outbound = evidence.latest_outbound_on
    inbound_is_latest = outbound is None or _is_later(inbound, outbound)
    event = inbound if inbound_is_latest else outbound
    max_action_age = (
        GMAIL_ACTION_MAX_AGE_DAYS
        if source == "gmail"
        else LINKEDIN_ACTION_MAX_AGE_DAYS
    )
    if (today - _event_date(event)).days > max_action_age:
        reminder = _reminder(
            ReminderState.REVIEW,
            (
                ReminderReasonCode.OLD_GMAIL_CONVERSATION_REVIEW
                if source == "gmail"
                else ReminderReasonCode.OLD_LINKEDIN_CONVERSATION_REVIEW
            ),
            priority=Priority.LOW,
            allowed=allowed,
        )
        return event, source, reminder

    if inbound_is_latest:
        reminder = _reminder(
            ReminderState.NEEDS_RESPONSE,
            (
                ReminderReasonCode.UNANSWERED_GMAIL_HUMAN_INBOUND
                if source == "gmail"
                else ReminderReasonCode.UNANSWERED_LINKEDIN_SUBSTANTIVE_INBOUND
            ),
            due_on=today,
            priority=Priority.URGENT,
            allowed=allowed,
        )
        return event, source, reminder

    wait_days = 4 if source == "gmail" else 5
    due_on = _event_date(outbound) + timedelta(days=wait_days)
    if due_on <= today:
        reminder = _reminder(
            ReminderState.FOLLOW_UP_DUE,
            (
                ReminderReasonCode.GMAIL_FOLLOW_UP_DUE
                if source == "gmail"
                else ReminderReasonCode.LINKEDIN_FOLLOW_UP_DUE
            ),
            due_on=due_on,
            priority=Priority.HIGH,
            allowed=allowed,
        )
        return event, source, reminder
    reminder = _reminder(
        ReminderState.WAITING,
        (
            ReminderReasonCode.WAITING_FOR_GMAIL_REPLY
            if source == "gmail"
            else ReminderReasonCode.WAITING_FOR_LINKEDIN_REPLY
        ),
        due_on=due_on,
        priority=Priority.LOW,
        allowed=allowed,
    )
    return event, source, reminder


def _review_reminder(
    facts: AccountPolicyFacts,
    *,
    today: date,
    state: ReminderState,
    reason: ReminderReasonCode,
    allowed: bool,
) -> ReminderRecommendation:
    """Keep old relationships quiet unless recovery evidence is strong.

    A review task is warranted only when a human has made the account
    authoritative, or when two independent primary source types corroborate
    the relationship.  Meetings and real human Gmail are the only strong
    source types here; LinkedIn never widens recovery eligibility.
    """
    strong_source_count = _strong_recovery_source_count(facts)
    should_recover = (
        strong_source_count >= 1 and _has_authoritative_account_state(facts)
    ) or (
        strong_source_count >= 2
    )
    return _reminder(
        state,
        reason,
        due_on=today if should_recover else None,
        priority=Priority.LOW,
        allowed=allowed,
        should_create=should_recover,
    )


def _has_authoritative_account_state(facts: AccountPolicyFacts) -> bool:
    return bool(
        facts.manual_pin
        or facts.sales_motion_active
        or facts.human_managed_opportunity
        or facts.human_current_action
    )


def _strong_recovery_source_count(facts: AccountPolicyFacts) -> int:
    return sum((
        facts.latest_completed_external_meeting_on is not None,
        facts.gmail.real_human_inbound_count > 0,
    ))


def _event_date(value: date | datetime) -> date:
    return value.date() if isinstance(value, datetime) else value


def _event_order_key(value: date | datetime) -> tuple[int, int]:
    if isinstance(value, datetime):
        seconds = value.hour * 3600 + value.minute * 60 + value.second
        return value.date().toordinal(), seconds
    return value.toordinal(), 0


def _is_later(left: date | datetime, right: date | datetime) -> bool:
    return _event_order_key(left) > _event_order_key(right)


def _is_acknowledgement_only(evidence: ConversationEvidence) -> bool:
    return bool(
        evidence.human_inbound_count > 0
        and evidence.acknowledgement_inbound_count
        == evidence.human_inbound_count
    )


def _is_decline_only(evidence: ConversationEvidence) -> bool:
    return bool(
        evidence.human_inbound_count > 0
        and evidence.polite_decline_inbound_count == evidence.human_inbound_count
    )


def _weak(reason: AdmissionReasonCode) -> _EvidenceCandidate:
    return _EvidenceCandidate(reason, EvidenceTier.WEAK, 5, None)


def _validate_temporal_facts(facts: AccountPolicyFacts, *, today: date) -> None:
    if (
        facts.upcoming_external_meeting_on is not None
        and facts.upcoming_external_meeting_on < today
    ):
        raise ValueError(
            "upcoming_external_meeting_on cannot be before the evaluation date."
        )
    if (
        facts.latest_completed_external_meeting_on is not None
        and facts.latest_completed_external_meeting_on > today
    ):
        raise ValueError(
            "latest_completed_external_meeting_on cannot be after the evaluation date."
        )
    for source_name, evidence in (("gmail", facts.gmail), ("linkedin", facts.linkedin)):
        for field_name, value in (
            ("latest_human_inbound_on", evidence.latest_human_inbound_on),
            ("latest_substantive_inbound_on", evidence.latest_substantive_inbound_on),
            ("latest_outbound_on", evidence.latest_outbound_on),
        ):
            if value is not None and _event_date(value) > today:
                raise ValueError(
                    f"{source_name}.{field_name} cannot be after the evaluation date."
                )


def _reminder(
    state: ReminderState,
    reason: ReminderReasonCode,
    *,
    priority: Priority,
    allowed: bool,
    due_on: date | None = None,
    should_create: bool | None = None,
) -> ReminderRecommendation:
    if should_create is None:
        should_create = state not in {
            ReminderState.NONE,
            ReminderState.WAITING,
            ReminderState.REVIEW,
        }
    return ReminderRecommendation(
        state=state,
        reason_code=reason,
        should_create_reminder=should_create,
        due_on=due_on,
        priority=priority,
        automated_outreach_allowed=allowed,
    )
