"""Bounded invitation recovery remains fair without restarting held email lanes."""
from __future__ import annotations

import pytest

from gmail.handoff import _maybe_schedule_gmail_sequence, recover_invitation_gmail_sequences
from linkedin.models import Task, WorkflowRun
from tests.gmail.test_invitation_start import invitation_deal, invitation_environment  # noqa: F401


pytestmark = pytest.mark.django_db


def test_missing_copy_does_not_starve_later_work_and_cursor_wraps(monkeypatch):
    held = invitation_deal(with_copy=False)
    later = invitation_deal()
    scope = [later.campaign_id, held.campaign_id]

    assert recover_invitation_gmail_sequences(operator="Arian", campaign_ids=scope, limit=1) == 0
    assert not Task.objects.exists()
    assert recover_invitation_gmail_sequences(operator="Arian", campaign_ids=reversed(scope), limit=1) == 1
    assert Task.objects.get().payload["deal_id"] == later.pk

    seen = []
    monkeypatch.setattr("gmail.handoff.maybe_schedule_gmail_sequence", lambda *, deal, operator: seen.append(deal.pk))
    assert recover_invitation_gmail_sequences(operator="Arian", campaign_ids=scope, limit=1) == 0
    assert seen == [held.pk]
    runs = list(WorkflowRun.objects.order_by("pk"))
    assert [run.counts["last_deal_id"] for run in runs] == [held.pk, later.pk, held.pk]
    assert [run.counts["scheduled"] for run in runs] == [0, 1, 0]


@pytest.mark.parametrize("terminal", [Task.Status.COMPLETED, Task.Status.FAILED])
def test_completed_lookup_is_a_hold_not_scheduled_work(terminal):
    held = invitation_deal(email="not an address")
    later = invitation_deal()
    lookup = _maybe_schedule_gmail_sequence(deal=held, operator="Arian")
    Task.objects.filter(pk=lookup.pk).update(status=terminal)
    scope = [held.campaign_id, later.campaign_id]

    assert recover_invitation_gmail_sequences(operator="Arian", campaign_ids=scope, limit=1) == 0
    assert recover_invitation_gmail_sequences(operator="Arian", campaign_ids=scope, limit=1) == 1
    assert Task.objects.filter(task_type=Task.TaskType.ENRICH_EMAIL).count() == 1
    lookup.refresh_from_db()
    assert lookup.status == terminal


def test_cursor_is_scoped_by_operator_and_exact_campaign_set():
    arian = invitation_deal(with_copy=False)
    chuka = invitation_deal(operator="Chuka")
    scope = [arian.campaign_id, chuka.campaign_id]
    WorkflowRun.objects.create(
        name="invitation-gmail-recovery", operator="Arian",
        counts={"campaign_ids": [arian.campaign_id], "last_deal_id": chuka.pk + 10},
    )
    assert recover_invitation_gmail_sequences(operator="Arian", campaign_ids=scope, limit=1) == 0
    assert recover_invitation_gmail_sequences(operator="Chuka", campaign_ids=scope, limit=1) == 1
    assert Task.objects.get().payload["operator"] == "Chuka"
    runs = WorkflowRun.objects.filter(counts__campaign_ids=scope)
    assert set(runs.values_list("operator", flat=True)) == {"Arian", "Chuka"}


@pytest.mark.parametrize("operator,scope", [("Eddy", [1]), ("", [1]), ("Arian", [True]), ("Arian", [-1]), ("Arian", ["1"])])
def test_recovery_rejects_ambiguous_scope_before_writes(operator, scope):
    with pytest.raises(ValueError):
        recover_invitation_gmail_sequences(operator=operator, campaign_ids=scope)
    assert not Task.objects.exists()
    assert not WorkflowRun.objects.exists()


def test_no_mapping_or_no_candidates_leaves_no_cursor(monkeypatch):
    assert recover_invitation_gmail_sequences(operator="Arian", campaign_ids=[]) == 0
    assert recover_invitation_gmail_sequences(operator="Arian", campaign_ids=[123]) == 0
    deal = invitation_deal()
    monkeypatch.setattr("gmail.handoff._operator_can_send_gmail", lambda operator: False)
    assert recover_invitation_gmail_sequences(operator="Arian", campaign_ids=[deal.campaign_id]) == 0
    assert not Task.objects.exists()
    assert not WorkflowRun.objects.exists()
