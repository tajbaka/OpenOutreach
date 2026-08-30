import pytest
from django.utils import timezone

from crm.models import Lead, Message
from drip.manifest import validate_manifest
from drip.models import DripDelivery, DripEnrollment, DripLane
from drip.services.publication import publish_manifest
from drip.services.stops import stop_for_inbound_message
from linkedin.models import Task


pytestmark = pytest.mark.django_db


def _active_domain(valid_drip_payload):
    published = publish_manifest(validate_manifest(valid_drip_payload))
    lead = Lead.objects.create(
        first_name="Ada",
        email="ada@example.com",
        icp="CSPs",
    )
    enrollment = DripEnrollment.objects.create(
        campaign=published.campaign,
        campaign_version=published.version,
        lead=lead,
        frozen_icp="CSPs",
        status=DripEnrollment.Status.ACTIVE,
        activated_at=timezone.now(),
        enrolled_by="reviewer",
        plan_hash="a" * 64,
    )
    lane = DripLane.objects.create(
        enrollment=enrollment,
        channel=DripLane.Channel.GMAIL,
        operator="Arian",
        provider_account="arian_boundera",
        sender_identity="ariant@getboundera.com",
        recipient_identity="ada@example.com",
        status=DripLane.Status.ACTIVE,
    )
    task = Task.objects.create(
        task_type="drip_gmail",
        status=Task.Status.PENDING,
        scheduled_at=timezone.now(),
        payload={"delivery_id": 1, "operator": "Arian"},
    )
    delivery = DripDelivery.objects.create(
        lane=lane,
        theme_key="visibility_gap",
        theme_index=0,
        step_index=0,
        frozen_subject="Subject",
        frozen_body="Body",
        scheduled_at=timezone.now(),
        status=DripDelivery.Status.QUEUED,
        current_task=task,
        provider_account="arian_boundera",
    )
    return lead, enrollment, lane, delivery, task


def test_inbound_message_stops_domain_and_pending_delivery_task(valid_drip_payload):
    lead, enrollment, lane, delivery, task = _active_domain(valid_drip_payload)
    inbound = Message.objects.create(
        lead=lead,
        source=Message.Source.GMAIL,
        direction=Message.Direction.INBOUND,
        external_id="gmail-reply",
        sender="ada@example.com",
        body="Reply",
        sent_at=timezone.now(),
    )

    assert stop_for_inbound_message(inbound.pk) == 1
    assert stop_for_inbound_message(inbound.pk) == 0
    enrollment.refresh_from_db()
    lane.refresh_from_db()
    delivery.refresh_from_db()
    task.refresh_from_db()
    assert enrollment.status == DripEnrollment.Status.STOPPED
    assert enrollment.stop_trigger_message == inbound
    assert lane.status == DripLane.Status.STOPPED
    assert delivery.status == DripDelivery.Status.STOPPED
    assert task.status == Task.Status.COMPLETED
    assert "Inbound gmail" in task.error


def test_outbound_or_calendar_message_does_not_stop(valid_drip_payload):
    lead, enrollment, lane, _delivery, _task = _active_domain(valid_drip_payload)
    outbound = Message.objects.create(
        lead=lead,
        source=Message.Source.GMAIL,
        direction=Message.Direction.OUTBOUND,
        external_id="gmail-outbound",
        sender="ariant@getboundera.com",
        body="Outbound",
        sent_at=timezone.now(),
    )

    assert stop_for_inbound_message(outbound.pk) == 0
    enrollment.refresh_from_db()
    lane.refresh_from_db()
    assert enrollment.status == DripEnrollment.Status.ACTIVE
    assert lane.status == DripLane.Status.ACTIVE
