"""Task.objects.claim_next(operator=..., campaign_ids=...) — task-scoping tests.

A daemon must only claim work for the LinkedIn account it is logged in
as. Two real leaks motivated this scoping:

  - Travis incident (2026-05-12): a daemon logged in as Arian popped a
    follow_up Task for one of Chuka's connections.
  - Cross-account connect / legacy-followup leak (Apr–May 2026): a daemon
    logged in as Arian ran 418 connect Tasks + 32 operator-less follow_up
    Tasks for Chuka's campaign, sending invites/DMs from the wrong
    account.

`claim_next` / `seconds_to_next` filter the queue via `_operator_scope_q`:
  - follow_up  → payload.operator must match;
  - connect    → payload.campaign_id must be one the daemon owns;
  - sweep_connections → account-agnostic.
"""
from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone as dj_tz

from linkedin.models import Task, _operator_scope_q


def _mk(task_type, *, operator=None, payload_extra=None, scheduled_offset_s=0):
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
def test_claim_next_filters_followup_to_matching_operator():
    chuka_t = _mk(Task.TaskType.FOLLOW_UP, operator="Chuka", scheduled_offset_s=-10)
    arian_t = _mk(Task.TaskType.FOLLOW_UP, operator="Arian", scheduled_offset_s=-5)

    # A daemon never sees another operator's follow_up.
    claimed = Task.objects.claim_next(operator="Arian", campaign_ids=[1])
    assert claimed is not None and claimed.pk == arian_t.pk

    claimed = Task.objects.claim_next(operator="Chuka", campaign_ids=[1])
    assert claimed is not None and claimed.pk == chuka_t.pk


@pytest.mark.django_db
def test_connect_task_scoped_to_owning_campaign():
    """A connect Task is claimable only by a daemon that owns its
    campaign — the invite goes out from that account. This is the
    418-invite cross-account leak (Apr–May 2026)."""
    mine = _mk(Task.TaskType.CONNECT, payload_extra={"campaign_id": 3},
               scheduled_offset_s=-10)
    # A foreign campaign's connect Task, scheduled earlier — must be skipped.
    _mk(Task.TaskType.CONNECT, payload_extra={"campaign_id": 99},
        scheduled_offset_s=-20)

    claimed = Task.objects.claim_next(operator="Arian", campaign_ids=[3])
    assert claimed is not None and claimed.pk == mine.pk


@pytest.mark.django_db
def test_connect_task_for_unowned_campaign_is_never_claimed():
    _mk(Task.TaskType.CONNECT, payload_extra={"campaign_id": 99}, scheduled_offset_s=-5)
    assert Task.objects.claim_next(operator="Chuka", campaign_ids=[1]) is None


@pytest.mark.django_db
def test_claim_next_can_be_restricted_to_followups_only():
    """After-hours follow-up catch-up can wake the daemon without letting
    unrelated connect tasks run before the next active window."""
    followup = _mk(
        Task.TaskType.FOLLOW_UP,
        operator="Arian",
        scheduled_offset_s=-10,
    )
    _mk(Task.TaskType.CONNECT, payload_extra={"campaign_id": 1}, scheduled_offset_s=-20)

    claimed = Task.objects.claim_next(
        operator="Arian",
        campaign_ids=[1],
        task_types={Task.TaskType.FOLLOW_UP},
    )
    assert claimed is not None and claimed.pk == followup.pk


@pytest.mark.django_db
def test_sweep_connections_stays_account_agnostic():
    """sweep_connections has no per-account identity — its handler only
    touches the claiming daemon's own campaigns — so it passes the
    filter for any operator."""
    sweep_t = _mk(Task.TaskType.SWEEP_CONNECTIONS, scheduled_offset_s=-1)
    _mk(Task.TaskType.FOLLOW_UP, operator="Chuka")  # not Arian's — filtered out

    claimed = Task.objects.claim_next(operator="Arian", campaign_ids=[2])
    assert claimed.pk == sweep_t.pk


@pytest.mark.django_db
def test_seconds_to_next_respects_operator_filter():
    """Sleep wait is scoped too — the daemon must not snooze on a Task it
    would never claim."""
    _mk(Task.TaskType.FOLLOW_UP, operator="Chuka", scheduled_offset_s=30)
    _mk(Task.TaskType.FOLLOW_UP, operator="Arian", scheduled_offset_s=600)

    arian_wait = Task.objects.seconds_to_next(operator="Arian", campaign_ids=[1])
    chuka_wait = Task.objects.seconds_to_next(operator="Chuka", campaign_ids=[1])
    assert arian_wait is not None and chuka_wait is not None
    assert chuka_wait < 100
    assert arian_wait > 500


@pytest.mark.django_db
def test_seconds_to_next_scopes_connect_to_owned_campaign():
    """The sleep wait ignores connect Tasks for campaigns we don't own."""
    _mk(Task.TaskType.CONNECT, payload_extra={"campaign_id": 99}, scheduled_offset_s=30)
    _mk(Task.TaskType.CONNECT, payload_extra={"campaign_id": 3}, scheduled_offset_s=600)
    wait = Task.objects.seconds_to_next(operator="Arian", campaign_ids=[3])
    assert wait is not None and wait > 500


@pytest.mark.django_db
def test_claim_next_excludes_enrich_phone_but_claims_manual_reply():
    """The outbound loop claims manual replies, but never enrichment tasks."""
    enrich = Task.objects.create(
        task_type=Task.TaskType.ENRICH_PHONE,
        status=Task.Status.PENDING,
        scheduled_at=dj_tz.now() - timedelta(seconds=60),
        payload={"lead_id": 1},
    )
    manual = Task.objects.create(
        task_type=Task.TaskType.MANUAL_REPLY,
        status=Task.Status.PENDING,
        scheduled_at=dj_tz.now() - timedelta(seconds=120),
        payload={"lead_id": 1, "operator": "Arian", "message": "Manual reply"},
    )
    assert Task.objects.claim_next().pk == manual.pk
    assert Task.objects.claim_next(operator="Arian", campaign_ids=[1]).pk == manual.pk
    assert Task.objects.seconds_to_next() == 0
    assert Task.objects.next_enrichment().pk == enrich.pk


@pytest.mark.django_db
def test_manual_reply_claims_before_older_followup():
    followup = _mk(
        Task.TaskType.FOLLOW_UP,
        operator="Arian",
        scheduled_offset_s=-120,
    )
    manual = Task.objects.create(
        task_type=Task.TaskType.MANUAL_REPLY,
        status=Task.Status.PENDING,
        scheduled_at=dj_tz.now() - timedelta(seconds=10),
        payload={"lead_id": 1, "operator": "Arian", "message": "Manual reply"},
    )

    claimed = Task.objects.claim_next(operator="Arian", campaign_ids=[1])
    assert claimed.pk == manual.pk
    assert claimed.pk != followup.pk


@pytest.mark.django_db
def test_manual_reply_scopes_to_matching_operator_when_enabled():
    chuka = Task.objects.create(
        task_type=Task.TaskType.MANUAL_REPLY,
        status=Task.Status.PENDING,
        scheduled_at=dj_tz.now() - timedelta(seconds=60),
        payload={"lead_id": 1, "operator": "Chuka", "message": "Reply from Chuka"},
    )
    Task.objects.create(
        task_type=Task.TaskType.MANUAL_REPLY,
        status=Task.Status.PENDING,
        scheduled_at=dj_tz.now() - timedelta(seconds=120),
        payload={"lead_id": 2, "operator": "Arian", "message": "Reply from Arian"},
    )

    scoped = Task.objects.filter(_operator_scope_q("Chuka", [1]))
    assert list(scoped) == [chuka]


@pytest.mark.django_db
def test_next_enrichment_returns_none_when_not_due():
    Task.objects.create(
        task_type=Task.TaskType.ENRICH_PHONE,
        status=Task.Status.PENDING,
        scheduled_at=dj_tz.now() + timedelta(hours=1),
        payload={"lead_id": 1},
    )
    assert Task.objects.next_enrichment() is None


@pytest.mark.django_db
def test_pending_followup_requires_non_empty_operator():
    with pytest.raises(ValidationError):
        Task.objects.create(
            task_type=Task.TaskType.FOLLOW_UP,
            status=Task.Status.PENDING,
            scheduled_at=dj_tz.now(),
            payload={"campaign_id": 1, "public_id": "alice"},
        )


@pytest.mark.django_db
def test_pending_connect_requires_campaign_id():
    with pytest.raises(ValidationError):
        Task.objects.create(
            task_type=Task.TaskType.CONNECT,
            status=Task.Status.PENDING,
            scheduled_at=dj_tz.now(),
            payload={},
        )


@pytest.mark.django_db
def test_pending_manual_reply_requires_lead_operator_and_message():
    with pytest.raises(ValidationError):
        Task.objects.create(
            task_type=Task.TaskType.MANUAL_REPLY,
            status=Task.Status.PENDING,
            scheduled_at=dj_tz.now(),
            payload={"lead_id": 1, "operator": "Arian", "message": "   "},
        )
