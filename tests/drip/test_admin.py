from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.contrib.admin.sites import AdminSite
from django.utils import timezone

from crm.models import Lead
from drip.admin import DripCampaignAdmin, DripEnrollmentAdmin, DripLaneAdmin
from drip.manifest import validate_manifest
from drip.models import DripCampaign, DripDelivery, DripEnrollment, DripLane
from drip.services.publication import publish_manifest
from linkedin.models import Task


pytestmark = pytest.mark.django_db


def _domain(valid_drip_payload):
    published = publish_manifest(validate_manifest(valid_drip_payload))
    now = timezone.now()
    lead = Lead.objects.create(
        first_name="Ada",
        last_name="Lovelace",
        company_name="Analytical Engines",
        linkedin_url="https://www.linkedin.com/in/ada-lovelace/",
        email="ada@example.com",
        icp="CSPs",
    )
    enrollment = DripEnrollment.objects.create(
        campaign=published.campaign,
        campaign_version=published.version,
        lead=lead,
        frozen_icp="CSPs",
        status=DripEnrollment.Status.ACTIVE,
        activated_at=now,
        enrolled_by="reviewer",
        plan_hash="a" * 64,
    )
    gmail_lane = DripLane.objects.create(
        enrollment=enrollment,
        channel=DripLane.Channel.GMAIL,
        operator="Arian",
        provider_account="arian_boundera",
        sender_identity="ariant@getboundera.com",
        recipient_identity="ada@example.com",
        status=DripLane.Status.ACTIVE,
        handed_off_at=now,
    )
    linkedin_lane = DripLane.objects.create(
        enrollment=enrollment,
        channel=DripLane.Channel.LINKEDIN,
        operator="Arian",
        provider_account="arian",
        sender_identity="arian",
        recipient_identity="https://www.linkedin.com/in/ada-lovelace/",
        status=DripLane.Status.ACTIVE,
        handed_off_at=now,
    )
    return published.campaign, gmail_lane, linkedin_lane


def _delivery_with_task(
    lane,
    *,
    task_status=Task.Status.PENDING,
    delivery_status=DripDelivery.Status.QUEUED,
):
    now = timezone.now()
    delivery = DripDelivery.objects.create(
        lane=lane,
        theme_key="visibility_gap",
        theme_index=0,
        step_index=0,
        frozen_subject="Subject" if lane.channel == DripLane.Channel.GMAIL else "",
        frozen_body="Body",
        scheduled_at=now,
        status=delivery_status,
        provider_account=lane.provider_account,
    )
    task = Task.objects.create(
        task_type=(
            Task.TaskType.DRIP_GMAIL
            if lane.channel == DripLane.Channel.GMAIL
            else Task.TaskType.DRIP_LINKEDIN
        ),
        status=task_status,
        scheduled_at=now,
        started_at=now if task_status == Task.Status.RUNNING else None,
        payload={"delivery_id": delivery.pk, "operator": lane.operator},
    )
    delivery.current_task = task
    delivery.save(update_fields={"current_task", "updated_at"})
    return delivery, task


def _admin_request():
    return SimpleNamespace(
        user=SimpleNamespace(get_username=lambda: "reviewer"),
    )


def _admin(admin_class, model):
    instance = admin_class(model, AdminSite())
    instance.message_user = Mock()
    return instance


def test_campaign_pause_retires_pending_tasks_but_leaves_running_work_claimed(
    valid_drip_payload,
):
    campaign, gmail_lane, linkedin_lane = _domain(valid_drip_payload)
    pending_delivery, pending_task = _delivery_with_task(gmail_lane)
    running_delivery, running_task = _delivery_with_task(
        linkedin_lane,
        task_status=Task.Status.RUNNING,
    )

    campaign_admin = _admin(DripCampaignAdmin, DripCampaign)
    campaign_admin.pause_campaigns(
        _admin_request(),
        DripCampaign.objects.filter(pk=campaign.pk),
    )

    campaign.refresh_from_db()
    pending_delivery.refresh_from_db()
    pending_task.refresh_from_db()
    running_delivery.refresh_from_db()
    running_task.refresh_from_db()
    assert campaign.status == DripCampaign.Status.PAUSED
    assert pending_task.status == Task.Status.COMPLETED
    assert pending_task.completed_at is not None
    assert "campaign paused" in pending_task.error.lower()
    assert pending_delivery.status == DripDelivery.Status.PLANNED
    assert pending_delivery.current_task_id is None
    assert running_task.status == Task.Status.RUNNING
    assert running_delivery.status == DripDelivery.Status.QUEUED
    assert running_delivery.current_task_id == running_task.pk


def test_lane_pause_retires_pending_work_and_can_resume(valid_drip_payload):
    _campaign, gmail_lane, _linkedin_lane = _domain(valid_drip_payload)
    delivery, task = _delivery_with_task(gmail_lane)
    lane_admin = _admin(DripLaneAdmin, DripLane)

    lane_admin.pause_lanes(
        _admin_request(),
        DripLane.objects.filter(pk=gmail_lane.pk),
    )

    gmail_lane.refresh_from_db()
    delivery.refresh_from_db()
    task.refresh_from_db()
    assert gmail_lane.status == DripLane.Status.PAUSED
    assert task.status == Task.Status.COMPLETED
    assert delivery.status == DripDelivery.Status.PLANNED
    assert delivery.current_task_id is None

    lane_admin.resume_lanes(
        _admin_request(),
        DripLane.objects.filter(pk=gmail_lane.pk),
    )
    gmail_lane.refresh_from_db()
    assert gmail_lane.status == DripLane.Status.ACTIVE


def test_lane_with_unclear_delivery_requires_explicit_stop(valid_drip_payload):
    _campaign, gmail_lane, _linkedin_lane = _domain(valid_drip_payload)
    gmail_lane.status = DripLane.Status.PAUSED
    gmail_lane.save(update_fields={"status", "updated_at"})
    delivery, task = _delivery_with_task(
        gmail_lane,
        task_status=Task.Status.FAILED,
        delivery_status=DripDelivery.Status.UNCLEAR,
    )
    lane_admin = _admin(DripLaneAdmin, DripLane)

    lane_admin.resume_lanes(
        _admin_request(),
        DripLane.objects.filter(pk=gmail_lane.pk),
    )

    gmail_lane.refresh_from_db()
    assert gmail_lane.status == DripLane.Status.PAUSED

    lane_admin.stop_lanes(
        _admin_request(),
        DripLane.objects.filter(pk=gmail_lane.pk),
    )
    gmail_lane.refresh_from_db()
    delivery.refresh_from_db()
    task.refresh_from_db()
    assert gmail_lane.status == DripLane.Status.STOPPED
    assert delivery.status == DripDelivery.Status.UNCLEAR
    assert delivery.current_task_id == task.pk
    assert task.status == Task.Status.FAILED


def test_lane_stop_retires_pending_task_and_stops_delivery(valid_drip_payload):
    _campaign, gmail_lane, _linkedin_lane = _domain(valid_drip_payload)
    delivery, task = _delivery_with_task(gmail_lane)
    lane_admin = _admin(DripLaneAdmin, DripLane)

    lane_admin.stop_lanes(
        _admin_request(),
        DripLane.objects.filter(pk=gmail_lane.pk),
    )

    gmail_lane.refresh_from_db()
    delivery.refresh_from_db()
    task.refresh_from_db()
    assert gmail_lane.status == DripLane.Status.STOPPED
    assert delivery.status == DripDelivery.Status.STOPPED
    assert delivery.current_task_id is None
    assert task.status == Task.Status.COMPLETED
    assert "human takeover" in task.error


def test_enrollment_pause_resume_and_stop_manage_pending_work(valid_drip_payload):
    _campaign, gmail_lane, linkedin_lane = _domain(valid_drip_payload)
    enrollment = gmail_lane.enrollment
    delivery, task = _delivery_with_task(gmail_lane)
    enrollment_admin = _admin(DripEnrollmentAdmin, DripEnrollment)

    enrollment_admin.pause_enrollments(
        _admin_request(),
        DripEnrollment.objects.filter(pk=enrollment.pk),
    )
    enrollment.refresh_from_db()
    delivery.refresh_from_db()
    task.refresh_from_db()
    assert enrollment.status == DripEnrollment.Status.PAUSED
    assert delivery.status == DripDelivery.Status.PLANNED
    assert delivery.current_task_id is None
    assert task.status == Task.Status.COMPLETED

    enrollment_admin.resume_enrollments(
        _admin_request(),
        DripEnrollment.objects.filter(pk=enrollment.pk),
    )
    enrollment.refresh_from_db()
    assert enrollment.status == DripEnrollment.Status.ACTIVE

    enrollment_admin.stop_enrollments(
        _admin_request(),
        DripEnrollment.objects.filter(pk=enrollment.pk),
    )
    enrollment.refresh_from_db()
    gmail_lane.refresh_from_db()
    linkedin_lane.refresh_from_db()
    assert enrollment.status == DripEnrollment.Status.STOPPED
    assert enrollment.stop_reason == "admin_stop"
    assert gmail_lane.status == DripLane.Status.STOPPED
    assert linkedin_lane.status == DripLane.Status.STOPPED
