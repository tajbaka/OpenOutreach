from copy import deepcopy
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone

from crm.models import Deal, Lead
from drip.manifest import validate_manifest
from drip.models import DripDelivery, DripEnrollment, DripLane
from drip.services.publication import publish_manifest
from drip.services.reconciliation import reconcile_drips
from linkedin.models import Task, WorkflowRun
from linkedin.enums import ProfileState


pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _no_shared_stop(monkeypatch):
    monkeypatch.setattr(
        "linkedin.tasks.stop_checks.lead_automation_stop_reason",
        lambda lead: "",
    )


def _domain(valid_drip_payload, *, now):
    published = publish_manifest(validate_manifest(valid_drip_payload))
    lead = Lead.objects.create(
        first_name="Ada",
        last_name="Lovelace",
        company_name="Analytical Engines",
        linkedin_url="https://www.linkedin.com/in/ada-lovelace/",
        public_identifier="ada-lovelace",
        email="ada@example.com",
        icp="CSPs",
    )
    enrollment = DripEnrollment.objects.create(
        campaign=published.campaign,
        campaign_version=published.version,
        lead=lead,
        frozen_icp="CSPs",
        status=DripEnrollment.Status.ACTIVE,
        activated_at=now - timedelta(days=10),
        enrolled_by="reviewer",
        plan_hash="a" * 64,
    )
    gmail_lane = DripLane.objects.create(
        enrollment=enrollment,
        channel=DripLane.Channel.GMAIL,
        operator="Arian",
        provider_account="arian_boundera",
        sender_identity="ariant@getboundera.com",
        recipient_identity=lead.email,
        status=DripLane.Status.ACTIVE,
        current_sequence_status=DripLane.CurrentSequenceStatus.NOT_APPLICABLE,
        current_sequence_reviewed_at=now - timedelta(days=10),
        current_sequence_reviewed_by="reviewer",
        handed_off_at=now - timedelta(days=10),
        current_theme_index=0,
        current_theme_key="visibility_gap",
        theme_started_at=now - timedelta(days=10),
    )
    linkedin_lane = DripLane.objects.create(
        enrollment=enrollment,
        channel=DripLane.Channel.LINKEDIN,
        operator="Arian",
        provider_account="arian",
        sender_identity="arian",
        recipient_identity=lead.linkedin_url,
        status=DripLane.Status.COMPLETED,
        current_sequence_status=DripLane.CurrentSequenceStatus.NOT_APPLICABLE,
        current_sequence_reviewed_at=now - timedelta(days=10),
        current_sequence_reviewed_by="reviewer",
        handed_off_at=now - timedelta(days=10),
        current_theme_index=2,
        current_theme_key="",
        theme_started_at=now - timedelta(days=10),
    )
    return published, enrollment, gmail_lane, linkedin_lane


def test_dry_run_has_no_writes_and_apply_materializes_only_one_task(
    valid_drip_payload,
):
    now = timezone.now()
    _published, _enrollment, lane, _linkedin = _domain(valid_drip_payload, now=now)

    preview = reconcile_drips(apply=False, now=now)

    assert any(decision.action == "would_materialize" for decision in preview.decisions)
    assert not DripDelivery.objects.exists()
    assert not Task.objects.filter(task_type=Task.TaskType.DRIP_GMAIL).exists()
    assert not WorkflowRun.objects.filter(name="drip-reconcile").exists()

    reconcile_drips(apply=True, now=now)
    delivery = lane.deliveries.get()
    first_task_id = delivery.current_task_id
    assert delivery.status == DripDelivery.Status.QUEUED
    assert first_task_id is not None

    reconcile_drips(apply=True, now=now)
    assert lane.deliveries.count() == 1
    assert Task.objects.filter(task_type=Task.TaskType.DRIP_GMAIL).count() == 1
    delivery.refresh_from_db()
    assert delivery.current_task_id == first_task_id


def test_gmail_lane_advances_while_linkedin_independently_waits_for_connection(
    valid_drip_payload,
):
    now = timezone.now()
    _published, _enrollment, gmail_lane, linkedin_lane = _domain(
        valid_drip_payload,
        now=now,
    )
    linkedin_lane.status = DripLane.Status.WAITING_CURRENT
    linkedin_lane.current_sequence_status = DripLane.CurrentSequenceStatus.PENDING
    linkedin_lane.handed_off_at = None
    linkedin_lane.current_theme_index = 0
    linkedin_lane.current_theme_key = "visibility_gap"
    linkedin_lane.save(
        update_fields={
            "status",
            "current_sequence_status",
            "handed_off_at",
            "current_theme_index",
            "current_theme_key",
            "updated_at",
        },
    )

    result = reconcile_drips(apply=True, now=now)

    assert gmail_lane.deliveries.count() == 1
    assert linkedin_lane.deliveries.count() == 0
    assert "materialized" in {decision.action for decision in result.decisions}
    assert any(
        decision.channel == DripLane.Channel.LINKEDIN
        and decision.action == "waiting_handoff"
        and decision.detail == "linkedin_connection_not_accepted"
        for decision in result.decisions
    )


def test_handed_off_linkedin_lane_reactivates_when_connection_is_restored(
    valid_drip_payload,
    fake_session,
):
    now = timezone.now()
    _published, enrollment, _gmail_lane, linkedin_lane = _domain(
        valid_drip_payload,
        now=now,
    )
    linkedin_lane.status = DripLane.Status.WAITING_CONNECTION
    linkedin_lane.current_theme_index = 0
    linkedin_lane.current_theme_key = "visibility_gap"
    linkedin_lane.theme_started_at = now - timedelta(days=1)
    linkedin_lane.save(
        update_fields={
            "status",
            "current_theme_index",
            "current_theme_key",
            "theme_started_at",
            "updated_at",
        },
    )
    Deal.objects.create(
        lead=enrollment.lead,
        campaign=fake_session.campaign,
        state=ProfileState.CONNECTED,
        invitation_sender="Arian",
        connected_at=now - timedelta(days=5),
    )

    result = reconcile_drips(apply=True, now=now)

    linkedin_lane.refresh_from_db()
    assert linkedin_lane.status == DripLane.Status.ACTIVE
    assert any(
        decision.lane_id == linkedin_lane.pk
        and decision.action == "connection_restored"
        for decision in result.decisions
    )
    # Active-hour scheduling may make a zero-delay LinkedIn step immediately
    # queueable or place it at the next eligible send instant.
    linkedin_actions = {
        decision.action
        for decision in result.decisions
        if decision.lane_id == linkedin_lane.pk
    }
    assert linkedin_actions & {"materialized", "waiting_due"}


def test_terminal_task_link_is_detached_and_same_frozen_delivery_is_requeued(
    valid_drip_payload,
):
    now = timezone.now()
    _published, _enrollment, lane, _linkedin = _domain(valid_drip_payload, now=now)
    reconcile_drips(apply=True, now=now)
    delivery = lane.deliveries.get()
    old_task = delivery.current_task
    old_task.status = Task.Status.COMPLETED
    old_task.completed_at = now
    old_task.save(update_fields={"status", "completed_at"})

    result = reconcile_drips(apply=True, now=now)

    delivery.refresh_from_db()
    assert lane.deliveries.count() == 1
    assert delivery.current_task_id != old_task.pk
    assert delivery.status == DripDelivery.Status.QUEUED
    assert delivery.current_task.status == Task.Status.PENDING
    assert {decision.action for decision in result.decisions} >= {
        "detached_terminal_task",
        "rematerialized_task",
    }


def test_running_task_is_never_detached(valid_drip_payload):
    now = timezone.now()
    _published, _enrollment, lane, _linkedin = _domain(valid_drip_payload, now=now)
    reconcile_drips(apply=True, now=now)
    delivery = lane.deliveries.get()
    task = delivery.current_task
    task.status = Task.Status.RUNNING
    task.started_at = now
    task.save(update_fields={"status", "started_at"})
    delivery.status = DripDelivery.Status.PLANNED
    delivery.save(update_fields={"status", "updated_at"})

    reconcile_drips(apply=True, now=now)

    delivery.refresh_from_db()
    assert delivery.current_task_id == task.pk
    assert Task.objects.filter(task_type=Task.TaskType.DRIP_GMAIL).count() == 1


def test_inactive_controls_detach_terminal_task_but_do_not_requeue(
    valid_drip_payload,
):
    now = timezone.now()
    published, _enrollment, lane, _linkedin = _domain(valid_drip_payload, now=now)
    reconcile_drips(apply=True, now=now)
    delivery = lane.deliveries.get()
    old_task = delivery.current_task
    old_task.status = Task.Status.FAILED
    old_task.save(update_fields={"status"})
    published.campaign.status = published.campaign.Status.PAUSED
    published.campaign.save(update_fields={"status", "updated_at"})

    result = reconcile_drips(apply=True, now=now)

    delivery.refresh_from_db()
    assert delivery.status == DripDelivery.Status.PLANNED
    assert delivery.current_task_id is None
    assert Task.objects.filter(task_type=Task.TaskType.DRIP_GMAIL).count() == 1
    assert "planned_held" in {decision.action for decision in result.decisions}


def test_later_gmail_step_due_is_anchored_to_previous_successful_sent_at(
    valid_drip_payload,
):
    now = timezone.now()
    _published, _enrollment, lane, _linkedin = _domain(valid_drip_payload, now=now)
    predecessor_sent_at = now - timedelta(days=1)
    DripDelivery.objects.create(
        lane=lane,
        theme_key="visibility_gap",
        theme_index=0,
        step_index=0,
        frozen_subject="Original subject",
        frozen_body="First message",
        scheduled_at=now - timedelta(days=9),
        sent_at=predecessor_sent_at,
        status=DripDelivery.Status.SENT,
        provider_account=lane.provider_account,
    )

    result = reconcile_drips(apply=False, now=now)
    waiting = next(decision for decision in result.decisions if decision.action == "waiting_due")

    assert waiting.due_at == predecessor_sent_at + timedelta(days=3)
    assert lane.deliveries.count() == 1


def test_linkedin_fractional_due_is_exact_and_never_materializes_early(
    valid_drip_payload,
    monkeypatch,
):
    payload = deepcopy(valid_drip_payload)
    payload["audiences"]["CSPs"]["themes"][0]["senders"]["Arian"]["linkedin"][1][
        "delay_days"
    ] = 0.0015
    now = datetime(2026, 8, 31, 12, 1, tzinfo=ZoneInfo("UTC"))
    _published, _enrollment, gmail_lane, linkedin_lane = _domain(payload, now=now)
    gmail_lane.status = DripLane.Status.COMPLETED
    gmail_lane.current_theme_index = 2
    gmail_lane.current_theme_key = ""
    gmail_lane.save(
        update_fields={"status", "current_theme_index", "current_theme_key", "updated_at"},
    )
    predecessor_sent_at = datetime(2026, 8, 31, 12, 0, tzinfo=ZoneInfo("UTC"))
    linkedin_lane.status = DripLane.Status.ACTIVE
    linkedin_lane.current_theme_index = 0
    linkedin_lane.current_theme_key = "visibility_gap"
    linkedin_lane.theme_started_at = predecessor_sent_at
    linkedin_lane.save(
        update_fields={
            "status",
            "current_theme_index",
            "current_theme_key",
            "theme_started_at",
            "updated_at",
        },
    )
    DripDelivery.objects.create(
        lane=linkedin_lane,
        theme_key="visibility_gap",
        theme_index=0,
        step_index=0,
        frozen_body="First LinkedIn message",
        scheduled_at=predecessor_sent_at,
        sent_at=predecessor_sent_at,
        status=DripDelivery.Status.SENT,
        provider_account=linkedin_lane.provider_account,
    )
    monkeypatch.setattr("linkedin.tasks.follow_up.ENABLE_ACTIVE_HOURS", True)
    monkeypatch.setattr("linkedin.tasks.follow_up.ACTIVE_START_HOUR", 9)
    monkeypatch.setattr("linkedin.tasks.follow_up.ACTIVE_END_HOUR", 17)
    monkeypatch.setattr("linkedin.tasks.follow_up.ACTIVE_TIMEZONE", "UTC")
    monkeypatch.setattr("linkedin.tasks.follow_up.REST_DAYS", (5, 6))
    exact_due_at = predecessor_sent_at + timedelta(days=0.0015)

    preview = reconcile_drips(apply=False, now=now)
    waiting = next(
        decision
        for decision in preview.decisions
        if decision.lane_id == linkedin_lane.pk and decision.action == "waiting_due"
    )

    assert waiting.due_at == exact_due_at
    reconcile_drips(apply=True, now=now)
    assert linkedin_lane.deliveries.count() == 1
    assert not Task.objects.filter(task_type=Task.TaskType.DRIP_LINKEDIN).exists()

    reconcile_drips(apply=True, now=exact_due_at)
    delivery = linkedin_lane.deliveries.get(step_index=1)
    assert delivery.scheduled_at == exact_due_at
    assert delivery.current_task.task_type == Task.TaskType.DRIP_LINKEDIN


def test_next_theme_starts_fresh_at_prior_theme_completion(valid_drip_payload):
    now = timezone.now()
    _published, _enrollment, lane, _linkedin = _domain(valid_drip_payload, now=now)
    final_sent_at = now - timedelta(hours=1)
    DripDelivery.objects.create(
        lane=lane,
        theme_key="visibility_gap",
        theme_index=0,
        step_index=0,
        frozen_subject="Original subject",
        frozen_body="First message",
        scheduled_at=now - timedelta(days=8),
        sent_at=now - timedelta(days=4),
        status=DripDelivery.Status.SENT,
        provider_account=lane.provider_account,
    )
    DripDelivery.objects.create(
        lane=lane,
        theme_key="visibility_gap",
        theme_index=0,
        step_index=1,
        frozen_subject="Original subject",
        frozen_body="Second message",
        scheduled_at=now - timedelta(days=1),
        sent_at=final_sent_at,
        status=DripDelivery.Status.SENT,
        provider_account=lane.provider_account,
    )

    result = reconcile_drips(apply=False, now=now)
    materialize = next(
        decision for decision in result.decisions if decision.action == "would_materialize"
    )

    assert materialize.detail == "theme proof step 0"
    assert materialize.due_at == final_sent_at


def test_omitted_theme_starts_next_applicable_theme_at_transition_time(
    valid_drip_payload,
):
    payload = deepcopy(valid_drip_payload)
    del payload["audiences"]["CSPs"]["themes"][0]["senders"]["Arian"]["gmail"]
    now = timezone.now()
    _published, _enrollment, lane, _linkedin = _domain(payload, now=now)

    result = reconcile_drips(apply=False, now=now)

    materialize = next(
        decision for decision in result.decisions if decision.action == "would_materialize"
    )
    assert materialize.detail == "theme proof step 0"
    assert materialize.due_at == now
