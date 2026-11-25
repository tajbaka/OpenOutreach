"""Receipt fields for the scheduled contextual CRM review."""

from collections.abc import Mapping
from datetime import datetime, timezone


def review_checkpoint(receipt: Mapping, *, phase: str) -> dict:
    """Keep a fresh or ongoing run open while recording its current phase."""
    if receipt.get("status") not in (None, "in_progress") or receipt.get("completed_at") is not None:
        raise ValueError("A checkpoint requires a fresh or in-progress review receipt")
    return dict(
        receipt,
        status="in_progress",
        checkpoint_phase=phase,
        checkpoint_at=datetime.now(timezone.utc).isoformat(),
        completed_at=None,
        current_review_complete=False,
        full_review_complete=False,
    )


def complete_with_holds(
    receipt: Mapping,
    *,
    checks: Mapping,
    verification: Mapping,
    historical_holds: int,
    historical_outcomes: int,
    unavailable_required_references: int,
) -> dict:
    """Finalize the current run using the results of its completion checks."""
    if (
        receipt.get("status") != "in_progress"
        or receipt.get("completed_at") is not None
        or receipt.get("blockers")
    ):
        raise ValueError("Completion requires this run's unblocked, in-progress receipt")
    required_checks = (
        "fresh_discovery_complete",
        "available_sources_reviewed",
        "independent_work_complete",
        "locked_readback_verified",
    )
    if any(checks.get(key) is not True for key in required_checks):
        raise ValueError("Current review checks are incomplete")
    for key in ("actionable_pending", "uncertain_writes", "blocking_failures"):
        count = checks.get(key)
        if type(count) is not int or count != 0:
            raise ValueError(f"Current review completion requires {key} to be zero")
    if any(
        verification.get(key) is not True
        for key in ("publisher_lock_held", "passed", "all_six_tabs_verified")
    ):
        raise ValueError("Current review requires successful locked readback of all six tabs")

    counts = {
        "historical_holds": historical_holds,
        "historical_outcomes": historical_outcomes,
        "unavailable_required_references": unavailable_required_references,
    }
    if any(type(count) is not int or count < 0 for count in counts.values()):
        raise ValueError("Advisory counts must be nonnegative integers")
    return dict(
        receipt,
        status="completed_with_holds",
        completed_at=datetime.now(timezone.utc).isoformat(),
        current_review_complete=True,
        full_review_complete=False,
        completion_scope="available_current_evidence",
        completion_checks=dict(checks),
        verification=dict(verification),
        advisories={
            **counts,
            "holds": historical_holds + unavailable_required_references,
            "retained": True,
        },
    )
