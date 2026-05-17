"""Task.objects.claim_next(operator=...) — operator-scoping filter tests.

The Travis incident (2026-05-12): daemon logged in as Arian popped a
follow_up Task for one of Chuka's connections and would have DM'd from
the wrong account. The fix has two layers:
  1. Enqueue stamps `payload.operator` with the canonical handle of
     whichever LinkedIn account owns the thread.
  2. claim_next filters follow_up Tasks to those matching the daemon's
     operator (with empty/missing payload.operator passing through for
     legacy Tasks enqueued before this field existed).

These tests cover layer 2.
"""
from datetime import datetime, timedelta, timezone

import pytest
from django.utils import timezone as dj_tz

from linkedin.models import Task


def _mk(task_type: str, *, operator: str | None = None, payload_extra: dict | None = None,
        scheduled_offset_s: int = 0) -> Task:
    payload = {"campaign_id": 1, "public_id": "alice"}
    if operator is not None:
        payload["operator"] = operator
    if payload_extra:
        payload.update(payload_extra)
    return Task.objects.create(
        task_type=task_type,
        status=Task.Status.PENDING,
        scheduled_at=dj_tz.now() + timedelta(seconds=scheduled_offset_s),
        payload=payload,
    )


@pytest.mark.django_db
def test_claim_next_without_operator_arg_returns_anything():
    """No operator filter → behavior unchanged. Legacy callers (tests,
    one-off scripts) still get the oldest due task."""
    _mk(Task.TaskType.FOLLOW_UP, operator="Chuka")
    t = Task.objects.claim_next()
    assert t is not None
    assert t.task_type == Task.TaskType.FOLLOW_UP


@pytest.mark.django_db
def test_claim_next_with_operator_filters_to_matching_followup():
    # Both tasks scheduled in the past so `due()` returns both. Order
    # would normally be Chuka first (earlier scheduled_at) under no
    # filter — the operator filter is what swaps which one pops.
    chuka_t = _mk(Task.TaskType.FOLLOW_UP, operator="Chuka", scheduled_offset_s=-10)
    arian_t = _mk(Task.TaskType.FOLLOW_UP, operator="Arian", scheduled_offset_s=-5)

    # Daemon logged in as Arian should never see Chuka's follow_up.
    claimed = Task.objects.claim_next(operator="Arian")
    assert claimed is not None
    assert claimed.pk == arian_t.pk

    # And vice-versa.
    claimed = Task.objects.claim_next(operator="Chuka")
    assert claimed is not None
    assert claimed.pk == chuka_t.pk


@pytest.mark.django_db
def test_claim_next_with_operator_lets_legacy_unstamped_tasks_through():
    """Tasks enqueued before payload.operator existed (or by code paths
    that don't stamp it yet) shouldn't get stranded — they fall through
    the filter so the daemon still picks them up. The in-handler
    `lead_outbound_operators` guard catches cross-account leaks among
    these."""
    legacy = Task.objects.create(
        task_type=Task.TaskType.FOLLOW_UP,
        status=Task.Status.PENDING,
        scheduled_at=dj_tz.now(),
        payload={"campaign_id": 1, "public_id": "alice"},  # no operator key
    )
    claimed = Task.objects.claim_next(operator="Arian")
    assert claimed.pk == legacy.pk


@pytest.mark.django_db
def test_claim_next_with_operator_lets_empty_string_operator_through():
    """payload.operator='' (set by enqueue_follow_up when caller didn't
    supply one) also falls through — same rationale as missing key."""
    t = _mk(Task.TaskType.FOLLOW_UP, operator="")
    claimed = Task.objects.claim_next(operator="Arian")
    assert claimed.pk == t.pk


@pytest.mark.django_db
def test_claim_next_with_operator_does_not_filter_other_task_types():
    """connect / sweep_connections Tasks are account-agnostic — they
    don't carry per-lead identity, so the operator filter shouldn't
    apply. (Only the daemon running them needs to be the right one;
    that's controlled by which LinkedInProfile is active, not the
    task payload.)"""
    sweep_t = _mk(Task.TaskType.SWEEP_CONNECTIONS, scheduled_offset_s=-1)
    chuka_followup = _mk(Task.TaskType.FOLLOW_UP, operator="Chuka")  # noqa: F841

    # Arian's daemon shouldn't get Chuka's follow-up, but SWEEP comes
    # through. SWEEP is scheduled earlier so it pops first.
    claimed = Task.objects.claim_next(operator="Arian")
    assert claimed.pk == sweep_t.pk


@pytest.mark.django_db
def test_seconds_to_next_respects_operator_filter():
    """Sleep wait should also be scoped — otherwise the daemon would
    snooze waiting on a Task it would never claim. Chuka's task is
    earlier in the future than Arian's; the unfiltered call would return
    Chuka's wait, the Arian-scoped call should return Arian's."""
    _mk(Task.TaskType.FOLLOW_UP, operator="Chuka", scheduled_offset_s=30)
    _mk(Task.TaskType.FOLLOW_UP, operator="Arian", scheduled_offset_s=600)

    arian_wait = Task.objects.seconds_to_next(operator="Arian")
    chuka_wait = Task.objects.seconds_to_next(operator="Chuka")
    assert arian_wait is not None and chuka_wait is not None
    # Operator scoping should give different waits — Arian's is the
    # 600s task, Chuka's is the 30s task.
    assert chuka_wait < 100
    assert arian_wait > 500


@pytest.mark.django_db
def test_claim_next_excludes_enrich_phone():
    """The outbound loop must never claim an enrichment task — that is the
    EnrichmentWorker's job."""
    enrich = Task.objects.create(
        task_type=Task.TaskType.ENRICH_PHONE,
        status=Task.Status.PENDING,
        scheduled_at=dj_tz.now() - timedelta(seconds=60),
        payload={"lead_id": 1},
    )
    # Even with nothing else queued and the task overdue, claim_next skips it.
    assert Task.objects.claim_next() is None
    assert Task.objects.claim_next(operator="Arian") is None
    # And it does not dictate the outbound loop's sleep.
    assert Task.objects.seconds_to_next() is None
    # But the dedicated query finds it.
    assert Task.objects.next_enrichment().pk == enrich.pk


@pytest.mark.django_db
def test_next_enrichment_returns_none_when_not_due():
    Task.objects.create(
        task_type=Task.TaskType.ENRICH_PHONE,
        status=Task.Status.PENDING,
        scheduled_at=dj_tz.now() + timedelta(hours=1),
        payload={"lead_id": 1},
    )
    assert Task.objects.next_enrichment() is None
