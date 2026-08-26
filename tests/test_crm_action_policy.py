from datetime import date, timedelta

import pytest

from linkedin.crm_action_policy import (
    SURFACE_ARCHIVE,
    SURFACE_DAILY,
    SURFACE_EXCLUDED,
    SURFACE_RECOVERY,
    SURFACE_WAITING,
    OpportunityActionFacts,
    place_action,
)


TODAY = date(2026, 8, 26)


@pytest.mark.parametrize(
    ("age", "surface"),
    [(21, SURFACE_DAILY), (22, SURFACE_RECOVERY), (60, SURFACE_RECOVERY), (61, SURFACE_ARCHIVE)],
)
def test_open_action_ages_between_surfaces(age, surface):
    result = place_action(
        OpportunityActionFacts(
            stage="discovery",
            last_meaningful_activity_on=TODAY - timedelta(days=age),
            action_status="open",
            action_kind="followup",
        ),
        today=TODAY,
    )

    assert result.surface == surface


def test_due_action_overrides_age_limit():
    result = place_action(
        OpportunityActionFacts(
            stage="evaluation",
            last_meaningful_activity_on=TODAY - timedelta(days=120),
            action_status="open",
            due_on=TODAY,
        ),
        today=TODAY,
    )

    assert result.surface == SURFACE_DAILY
    assert result.category == "due_today"


def test_future_waiting_disappears_then_reappears_when_due():
    facts = OpportunityActionFacts(
        stage="evaluation",
        last_meaningful_activity_on=TODAY,
        action_status="waiting",
        waiting_until=TODAY + timedelta(days=1),
        due_on=TODAY + timedelta(days=1),
        manual_pin=True,
    )

    assert place_action(facts, today=TODAY).surface == SURFACE_WAITING
    assert place_action(facts, today=TODAY + timedelta(days=1)).surface == SURFACE_DAILY


def test_waiting_date_reappears_when_due_even_without_separate_due_date():
    facts = OpportunityActionFacts(
        stage="evaluation",
        last_meaningful_activity_on=TODAY - timedelta(days=100),
        action_status="waiting",
        waiting_until=TODAY + timedelta(days=1),
    )

    assert place_action(facts, today=TODAY).surface == SURFACE_WAITING
    due = place_action(facts, today=TODAY + timedelta(days=1))
    assert due.surface == SURFACE_DAILY
    assert due.category == "waiting_due"


def test_upcoming_real_meeting_overrides_inactivity():
    result = place_action(
        OpportunityActionFacts(
            stage="demo_planning",
            last_meaningful_activity_on=TODAY - timedelta(days=100),
            upcoming_meeting_on=TODAY + timedelta(days=2),
        ),
        today=TODAY,
    )

    assert result.surface == SURFACE_DAILY
    assert result.category == "meeting_prep"


def test_old_meeting_label_without_real_date_is_not_evergreen():
    result = place_action(
        OpportunityActionFacts(
            stage="demo_planning",
            last_meaningful_activity_on=TODAY - timedelta(days=100),
        ),
        today=TODAY,
    )

    assert result.surface == SURFACE_ARCHIVE


def test_fresh_inbound_requires_a_fresh_trigger_date():
    stale = place_action(
        OpportunityActionFacts(
            stage="discovery",
            last_meaningful_activity_on=TODAY - timedelta(days=80),
            needs_response=True,
            fresh_trigger_on=TODAY - timedelta(days=80),
        ),
        today=TODAY,
    )
    fresh = place_action(
        OpportunityActionFacts(
            stage="discovery",
            last_meaningful_activity_on=TODAY,
            needs_response=True,
            fresh_trigger_on=TODAY,
        ),
        today=TODAY,
    )

    assert stale.surface == SURFACE_ARCHIVE
    assert fresh.surface == SURFACE_DAILY
    assert fresh.category == "needs_response"


@pytest.mark.parametrize(
    "facts",
    [
        OpportunityActionFacts(stage="closed_won"),
        OpportunityActionFacts(stage="closed_lost"),
        OpportunityActionFacts(stage="discovery", dont_send=True),
        OpportunityActionFacts(stage="discovery", lead_disqualified=True),
        OpportunityActionFacts(stage="discovery", failed_non_actionable=True),
    ],
)
def test_terminal_and_suppressed_records_are_excluded(facts):
    assert place_action(facts, today=TODAY).surface == SURFACE_EXCLUDED


def test_polite_decline_is_review_not_closed_lost():
    result = place_action(
        OpportunityActionFacts(stage="discovery", polite_decline=True),
        today=TODAY,
    )

    assert result.surface == SURFACE_RECOVERY
    assert result.category == "manual_review"


def test_manual_pin_overrides_age_but_not_future_wait():
    result = place_action(
        OpportunityActionFacts(
            stage="discovery",
            last_meaningful_activity_on=TODAY - timedelta(days=400),
            manual_pin=True,
        ),
        today=TODAY,
    )

    assert result.surface == SURFACE_DAILY


def test_undated_explicit_action_stops_overriding_after_sixty_days():
    recovery_age = place_action(
        OpportunityActionFacts(
            stage="discovery",
            last_meaningful_activity_on=TODAY - timedelta(days=60),
            action_status="open",
            action_kind="next_step",
            explicit_current_action=True,
        ),
        today=TODAY,
    )
    archive_age = place_action(
        OpportunityActionFacts(
            stage="discovery",
            last_meaningful_activity_on=TODAY - timedelta(days=61),
            action_status="open",
            action_kind="next_step",
            explicit_current_action=True,
        ),
        today=TODAY,
    )

    assert recovery_age.surface == SURFACE_DAILY
    assert recovery_age.reason == "explicit_current_action"
    assert archive_age.surface == SURFACE_ARCHIVE


@pytest.mark.parametrize(
    "facts",
    [
        OpportunityActionFacts(
            stage="discovery",
            last_meaningful_activity_on=TODAY - timedelta(days=120),
            action_status="open",
            due_on=TODAY,
        ),
        OpportunityActionFacts(
            stage="discovery",
            last_meaningful_activity_on=TODAY - timedelta(days=120),
            needs_response=True,
            fresh_trigger_on=TODAY,
        ),
        OpportunityActionFacts(
            stage="discovery",
            last_meaningful_activity_on=TODAY - timedelta(days=120),
            upcoming_meeting_on=TODAY + timedelta(days=1),
        ),
        OpportunityActionFacts(
            stage="discovery",
            last_meaningful_activity_on=TODAY - timedelta(days=120),
            manual_pin=True,
        ),
    ],
)
def test_only_explicit_archive_age_exceptions_stay_daily(facts):
    assert place_action(facts, today=TODAY).surface == SURFACE_DAILY


def test_meeting_prep_requires_a_real_meeting_and_waits_until_prep_window():
    invalid = place_action(
        OpportunityActionFacts(
            stage="evaluation",
            action_status="open",
            action_kind="meeting_prep",
        ),
        today=TODAY,
    )
    scheduled = place_action(
        OpportunityActionFacts(
            stage="evaluation",
            action_status="open",
            action_kind="meeting_prep",
            upcoming_meeting_on=TODAY + timedelta(days=14),
            meeting_prep_has_real_meeting=True,
        ),
        today=TODAY,
    )

    assert invalid.surface == SURFACE_EXCLUDED
    assert invalid.reason == "meeting_prep_without_real_meeting"
    assert scheduled.surface == SURFACE_WAITING
    assert scheduled.category == "meeting_scheduled"


def test_unroutable_and_cross_contact_conflicts_go_to_manual_recovery():
    unroutable = place_action(
        OpportunityActionFacts(
            stage="discovery",
            action_status="open",
            due_on=TODAY,
            unroutable_action=True,
        ),
        today=TODAY,
    )
    conflict = place_action(
        OpportunityActionFacts(
            stage="discovery",
            action_status="open",
            needs_response=True,
            fresh_trigger_on=TODAY,
            routing_conflict=True,
        ),
        today=TODAY,
    )

    assert unroutable.surface == SURFACE_RECOVERY
    assert unroutable.reason == "unresolved_action_target"
    assert conflict.surface == SURFACE_RECOVERY
    assert conflict.reason == "inbound_target_conflicts_with_current_action"


def test_unresolved_manual_pin_overrides_archive_age_into_recovery():
    result = place_action(
        OpportunityActionFacts(
            stage="discovery",
            last_meaningful_activity_on=TODAY - timedelta(days=400),
            manual_pin_unresolved=True,
        ),
        today=TODAY,
    )

    assert result.surface == SURFACE_RECOVERY
    assert result.category == "manual_review"
    assert result.reason == "manual_pin_needs_target"
