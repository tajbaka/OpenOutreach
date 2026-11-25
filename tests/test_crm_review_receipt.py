import json
from datetime import datetime

import pytest

from crm.review_receipt import complete_with_holds, review_checkpoint


@pytest.fixture
def active_review():
    return review_checkpoint(
        {
            "started_at": "2026-01-01T14:00:00+00:00",
            "full_review_watermark": "2025-12-01T14:00:00+00:00",
            "gmail_history_skipped": {"gmail_history_message_and_thread_not_found": 2},
            "blockers": [],
        },
        phase="source_discovery",
    )


@pytest.fixture
def verified_completion():
    return {
        "checks": {
            "fresh_discovery_complete": True,
            "available_sources_reviewed": True,
            "independent_work_complete": True,
            "locked_readback_verified": True,
            "actionable_pending": 0,
            "uncertain_writes": 0,
            "blocking_failures": 0,
        },
        "verification": {
            "publisher_lock_held": True,
            "passed": True,
            "all_six_tabs_verified": True,
        },
        "historical_holds": 9,
        "historical_outcomes": 28,
        "unavailable_required_references": 3,
    }


def test_discovery_checkpoint_remains_in_progress(active_review):
    checkpoint = review_checkpoint(
        dict(active_review, source_progress={"required": 757, "verified": 600}),
        phase="gmail_full_thread_review",
    )

    assert checkpoint["status"] == "in_progress"
    assert checkpoint["checkpoint_phase"] == "gmail_full_thread_review"
    assert checkpoint["source_progress"] == {"required": 757, "verified": 600}
    assert checkpoint["completed_at"] is None
    assert checkpoint["current_review_complete"] is False
    assert checkpoint["full_review_complete"] is False
    assert datetime.fromisoformat(checkpoint["checkpoint_at"]).utcoffset().total_seconds() == 0


def test_completed_receipt_includes_total_advisory_count(active_review, verified_completion):
    receipt = json.loads(json.dumps(complete_with_holds(active_review, **verified_completion)))

    assert receipt["status"] == "completed_with_holds"
    assert receipt["current_review_complete"] is True
    assert receipt["full_review_complete"] is False
    assert receipt["completion_scope"] == "available_current_evidence"
    assert receipt["completion_checks"] == verified_completion["checks"]
    assert receipt["verification"] == verified_completion["verification"]
    assert receipt["advisories"] == {
        "holds": 12,
        "historical_holds": 9,
        "historical_outcomes": 28,
        "unavailable_required_references": 3,
        "retained": True,
    }
    assert receipt["gmail_history_skipped"] == active_review["gmail_history_skipped"]
    assert receipt["full_review_watermark"] == active_review["full_review_watermark"]
    assert receipt["started_at"] == active_review["started_at"]
    assert datetime.fromisoformat(receipt["completed_at"]) >= datetime.fromisoformat(
        active_review["checkpoint_at"]
    )
    assert active_review["status"] == "in_progress"


@pytest.mark.parametrize(
    "section,key,value",
    [
        ("checks", "fresh_discovery_complete", False),
        ("checks", "available_sources_reviewed", False),
        ("checks", "independent_work_complete", False),
        ("checks", "locked_readback_verified", False),
        ("checks", "actionable_pending", 1),
        ("checks", "uncertain_writes", 1),
        ("checks", "blocking_failures", 1),
        ("verification", "publisher_lock_held", False),
        ("verification", "passed", False),
        ("verification", "all_six_tabs_verified", False),
    ],
)
def test_failed_checks_cannot_finalize(active_review, verified_completion, section, key, value):
    verified_completion[section][key] = value

    with pytest.raises(ValueError):
        complete_with_holds(active_review, **verified_completion)

    assert active_review["completed_at"] is None


def test_blocked_receipt_cannot_be_reopened(active_review, verified_completion):
    active_review["status"] = "blocked"

    with pytest.raises(ValueError):
        review_checkpoint(active_review, phase="verification")
    with pytest.raises(ValueError):
        complete_with_holds(active_review, **verified_completion)


def test_open_receipt_with_a_blocker_cannot_finalize(active_review, verified_completion):
    active_review["blockers"] = [{"phase": "source_review"}]

    with pytest.raises(ValueError, match="unblocked"):
        complete_with_holds(active_review, **verified_completion)


@pytest.mark.parametrize("count", [-1, True, None])
def test_invalid_hold_counts_cannot_finalize(active_review, verified_completion, count):
    verified_completion["unavailable_required_references"] = count

    with pytest.raises(ValueError, match="Advisory counts"):
        complete_with_holds(active_review, **verified_completion)
