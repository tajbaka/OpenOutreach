"""Pure lifecycle policy for CRM action surfaces.

This module deliberately does not read Google Sheets or infer sales stages.
Callers provide canonical opportunity/action facts and receive a deterministic
placement decision.  Re-evaluating with a later ``today`` is enough to move a
relationship from daily work to Recovery and then archive without any new
message arriving.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date


DAILY_MAX_AGE_DAYS = 21
RECOVERY_MAX_AGE_DAYS = 60
MEETING_PREP_DAYS = 7

SURFACE_DAILY = "daily"
SURFACE_RECOVERY = "recovery"
SURFACE_ARCHIVE = "archive"
SURFACE_WAITING = "waiting"
SURFACE_EXCLUDED = "excluded"
SURFACE_NONE = "none"

TERMINAL_STAGES = frozenset({"closed_won", "closed_lost"})
OPEN_ACTION_STATUSES = frozenset({"open", "waiting"})


@dataclass(frozen=True)
class OpportunityActionFacts:
    stage: str
    last_meaningful_activity_on: date | None = None
    action_status: str = ""
    action_kind: str = ""
    due_on: date | None = None
    waiting_until: date | None = None
    needs_response: bool = False
    fresh_trigger_on: date | None = None
    upcoming_meeting_on: date | None = None
    unresolved_post_meeting: bool = False
    missing_next_action: bool = False
    explicit_current_action: bool = False
    manual_pin: bool = False
    lead_disqualified: bool = False
    dont_send: bool = False
    failed_non_actionable: bool = False
    polite_decline: bool = False
    unroutable_action: bool = False
    routing_conflict: bool = False
    manual_pin_unresolved: bool = False
    meeting_prep_has_real_meeting: bool = False


@dataclass(frozen=True)
class ActionPlacement:
    surface: str
    category: str
    reason: str
    inactivity_days: int | None

    @property
    def is_daily(self) -> bool:
        return self.surface == SURFACE_DAILY


def place_action(
    facts: OpportunityActionFacts,
    *,
    today: date,
) -> ActionPlacement:
    """Place one opportunity/action on a CRM work surface.

    Explicit waiting dates suppress daily work until due.  Due work, a real
    upcoming meeting, a fresh inbound trigger, or a manual pin can override
    ordinary inactivity aging.  A polite decline is review-only and never an
    automatic lost outcome.
    """
    age = _age_days(facts.last_meaningful_activity_on, today=today)

    if facts.stage in TERMINAL_STAGES:
        return _placement(SURFACE_EXCLUDED, "terminal", facts.stage, age)
    if facts.dont_send:
        return _placement(SURFACE_EXCLUDED, "dont_send", "do_not_contact", age)
    if facts.lead_disqualified:
        return _placement(SURFACE_EXCLUDED, "disqualified", "lead_disqualified", age)
    if facts.failed_non_actionable:
        return _placement(SURFACE_EXCLUDED, "failed", "non_actionable_failure", age)
    if facts.routing_conflict:
        return _placement(
            SURFACE_RECOVERY,
            "manual_review",
            "inbound_target_conflicts_with_current_action",
            age,
        )
    if facts.manual_pin_unresolved:
        return _placement(
            SURFACE_RECOVERY,
            "manual_review",
            "manual_pin_needs_target",
            age,
        )
    if facts.unroutable_action:
        return _placement(
            SURFACE_RECOVERY,
            "manual_review",
            "unresolved_action_target",
            age,
        )
    if facts.action_kind == "meeting_prep" and not facts.meeting_prep_has_real_meeting:
        return _placement(
            SURFACE_EXCLUDED,
            "invalid_meeting",
            "meeting_prep_without_real_meeting",
            age,
        )
    if facts.polite_decline:
        return _placement(
            SURFACE_RECOVERY,
            "manual_review",
            "polite_decline_needs_human_review",
            age,
        )

    # A human-selected waiting date is authoritative.  It remains canonical
    # on Opportunities but cannot clutter a sender queue before that date.
    if facts.waiting_until is not None and facts.waiting_until > today:
        return _placement(
            SURFACE_WAITING,
            "waiting",
            f"waiting_until:{facts.waiting_until.isoformat()}",
            age,
        )

    has_open_action = facts.action_status in OPEN_ACTION_STATUSES
    if has_open_action and facts.due_on is not None:
        if facts.due_on < today:
            return _placement(
                SURFACE_DAILY,
                "overdue_next_action",
                f"due:{facts.due_on.isoformat()}",
                age,
            )
        if facts.due_on == today:
            return _placement(SURFACE_DAILY, "due_today", "due_today", age)

    if (
        has_open_action
        and facts.waiting_until is not None
        and facts.waiting_until <= today
    ):
        return _placement(
            SURFACE_DAILY,
            "waiting_due",
            f"waiting_due:{facts.waiting_until.isoformat()}",
            age,
        )

    # A real calendar date is required.  Merely carrying a legacy
    # "Meeting Booked" label is intentionally insufficient.
    if facts.upcoming_meeting_on is not None:
        days_to_meeting = (facts.upcoming_meeting_on - today).days
        if 0 <= days_to_meeting <= MEETING_PREP_DAYS:
            return _placement(
                SURFACE_DAILY,
                "meeting_prep",
                f"meeting_in:{days_to_meeting}_days",
                age,
            )
        if facts.action_kind == "meeting_prep" and days_to_meeting > MEETING_PREP_DAYS:
            return _placement(
                SURFACE_WAITING,
                "meeting_scheduled",
                f"meeting_in:{days_to_meeting}_days",
                age,
            )

    fresh_response = facts.needs_response and _is_fresh(
        facts.fresh_trigger_on,
        today=today,
    )
    if fresh_response:
        return _placement(SURFACE_DAILY, "needs_response", "fresh_inbound", age)

    if facts.manual_pin:
        return _placement(SURFACE_DAILY, "manual_pin", "manual_pin", age)

    # A future due date is visible on Opportunities but is not due-now work.
    if has_open_action and facts.due_on is not None and facts.due_on > today:
        return _placement(
            SURFACE_WAITING,
            "scheduled",
            f"due:{facts.due_on.isoformat()}",
            age,
        )

    category = _required_action_category(
        facts,
        has_open_action=has_open_action,
        fresh_response=fresh_response,
    )
    if category:
        # A deliberately maintained current action may remain daily through
        # Recovery age (22-60 days).  Beyond 60 days it needs one of the
        # explicit exceptions handled above: due, fresh inbound, real meeting,
        # or manual pin.  Merely leaving an undated row open is not evergreen.
        if facts.explicit_current_action and (
            age is None or age <= RECOVERY_MAX_AGE_DAYS
        ):
            return _placement(
                SURFACE_DAILY,
                category,
                "explicit_current_action",
                age,
            )
        if age is None or age <= DAILY_MAX_AGE_DAYS:
            return _placement(SURFACE_DAILY, category, "active_action_required", age)
        if age <= RECOVERY_MAX_AGE_DAYS:
            return _placement(SURFACE_RECOVERY, category, "inactive_22_to_60_days", age)
        return _placement(SURFACE_ARCHIVE, category, "inactive_over_60_days", age)

    # Recovery is a separate review surface for quiet canonical
    # opportunities, not a second daily followup list.
    if age is not None and age > RECOVERY_MAX_AGE_DAYS:
        return _placement(SURFACE_ARCHIVE, "nurture", "inactive_over_60_days", age)
    if age is not None and age > DAILY_MAX_AGE_DAYS:
        return _placement(SURFACE_RECOVERY, "review", "inactive_22_to_60_days", age)
    return _placement(SURFACE_NONE, "none", "no_action_required", age)


def _required_action_category(
    facts: OpportunityActionFacts,
    *,
    has_open_action: bool,
    fresh_response: bool,
) -> str:
    if fresh_response:
        return "needs_response"
    if facts.unresolved_post_meeting:
        return "post_meeting_commitment"
    if facts.missing_next_action:
        return "missing_next_action"
    if has_open_action:
        return facts.action_kind or "next_step"
    return ""


def _age_days(activity_on: date | None, *, today: date) -> int | None:
    if activity_on is None:
        return None
    return max(0, (today - activity_on).days)


def _is_fresh(trigger_on: date | None, *, today: date) -> bool:
    if trigger_on is None:
        return False
    return 0 <= (today - trigger_on).days <= DAILY_MAX_AGE_DAYS


def _placement(
    surface: str,
    category: str,
    reason: str,
    inactivity_days: int | None,
) -> ActionPlacement:
    return ActionPlacement(
        surface=surface,
        category=category,
        reason=reason,
        inactivity_days=inactivity_days,
    )
