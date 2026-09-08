import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, models, transaction
from django.db.models.deletion import PROTECT
from django.utils import timezone

from crm.models import Deal, Lead
from linkedin.models import (
    Campaign,
    CampaignMessageEnrollment,
    MessageProgram,
    MessageProgramVersion,
    OutboundDelivery,
    Task,
)
from tests.factories import UserFactory


pytestmark = pytest.mark.django_db


def _version(
    program: MessageProgram,
    *,
    version: int = 1,
    content_hash: str = "a" * 64,
    based_on_version: MessageProgramVersion | None = None,
) -> MessageProgramVersion:
    return MessageProgramVersion.objects.create(
        program=program,
        version=version,
        schema_version=1,
        payload={"audiences": {"csp": {"steps": []}}},
        content_hash=content_hash,
        published_by="reviewer@example.com",
        based_on_version=based_on_version,
    )


def _bound_deal(*, active_version: MessageProgramVersion) -> Deal:
    campaign = Campaign.objects.create(
        name=f"Campaign {active_version.program.key}",
        user=UserFactory(),
        active_message_version=active_version,
    )
    lead = Lead.objects.create(
        first_name="Ada",
        email=f"ada-{active_version.program.key}@example.com",
        icp="CSPs",
    )
    return Deal.objects.create(lead=lead, campaign=campaign)


def _enrollment(*, deal: Deal, version: MessageProgramVersion) -> CampaignMessageEnrollment:
    enrollment = CampaignMessageEnrollment(
        deal=deal,
        message_version=version,
        audience_key="csp-small-founder-rev5-maint",
        operator="Arian",
    )
    enrollment.full_clean()
    enrollment.save()
    return enrollment


def _delivery(
    *,
    enrollment: CampaignMessageEnrollment,
    channel: str = OutboundDelivery.Channel.GMAIL,
    step_key: str = "email-1",
    step_index: int = 0,
    variant_key: str = "control",
    task: Task | None = None,
) -> OutboundDelivery:
    subject = "FedRAMP timing" if channel == OutboundDelivery.Channel.GMAIL else ""
    delivery = OutboundDelivery(
        enrollment=enrollment,
        channel=channel,
        step_key=step_key,
        step_index=step_index,
        variant_key=variant_key,
        operator=enrollment.operator,
        scheduled_at=timezone.now(),
        frozen_subject=subject,
        frozen_body="A fully rendered message.",
        frozen_media=[{"kind": "link", "url": "https://example.com/brief"}],
        render_hash="d" * 64,
        task=task,
    )
    delivery.full_clean()
    delivery.save()
    return delivery


def test_message_program_versions_are_immutable_and_versioned():
    program = MessageProgram.objects.create(key="csp-founder", name="CSP Founder")
    first = _version(program)
    second = _version(
        program,
        version=2,
        content_hash="b" * 64,
        based_on_version=first,
    )

    assert second.based_on_version == first

    first.payload = {"changed": True}
    with pytest.raises(ValidationError, match="immutable"):
        first.save()
    with pytest.raises(ValidationError, match="immutable"):
        first.delete()


def test_message_program_version_constraints_and_lineage_validation():
    program = MessageProgram.objects.create(key="csp-grc", name="CSP GRC")
    first = _version(program)

    with pytest.raises(IntegrityError), transaction.atomic():
        _version(program, content_hash="b" * 64)
    with pytest.raises(IntegrityError), transaction.atomic():
        _version(program, version=2)
    with pytest.raises(IntegrityError), transaction.atomic():
        _version(program, version=0, content_hash="0" * 64)

    other_program = MessageProgram.objects.create(key="csp-cfo", name="CSP CFO")
    cross_program = MessageProgramVersion(
        program=other_program,
        version=2,
        schema_version=1,
        payload={},
        content_hash="c" * 64,
        published_by="reviewer@example.com",
        based_on_version=first,
    )
    with pytest.raises(ValidationError, match="another message program"):
        cross_program.full_clean()


def test_enrollment_must_freeze_the_deal_campaign_active_version():
    program = MessageProgram.objects.create(key="csp-security", name="CSP Security")
    active_version = _version(program)
    deal = _bound_deal(active_version=active_version)
    enrollment = _enrollment(deal=deal, version=active_version)

    assert enrollment.message_version == active_version
    assert enrollment.deal == deal

    other_program = MessageProgram.objects.create(key="csp-coo", name="CSP COO")
    wrong_version = _version(other_program, content_hash="b" * 64)
    other_deal = Deal.objects.create(
        lead=Lead.objects.create(first_name="Grace", email="grace@example.com", icp="CSPs"),
        campaign=deal.campaign,
    )
    mismatch = CampaignMessageEnrollment(
        deal=other_deal,
        message_version=wrong_version,
        audience_key="csp-small-coo-rev5-maint",
        operator="Arian",
    )
    with pytest.raises(ValidationError, match="not active"):
        mismatch.full_clean()

    with pytest.raises(IntegrityError), transaction.atomic():
        CampaignMessageEnrollment.objects.create(
            deal=deal,
            message_version=active_version,
            audience_key="duplicate",
            operator="Arian",
        )

    next_version = _version(
        program,
        version=2,
        content_hash="c" * 64,
        based_on_version=active_version,
    )
    deal.campaign.active_message_version = next_version
    deal.campaign.save(update_fields={"active_message_version"})
    enrollment.full_clean()

    enrollment.audience_key = "changed-audience"
    with pytest.raises(ValidationError, match="immutable"):
        enrollment.save()


def test_enrollment_rejects_blank_normalized_routing_fields():
    program = MessageProgram.objects.create(key="csp-blank", name="CSP Blank")
    version = _version(program)
    deal = _bound_deal(active_version=version)
    enrollment = CampaignMessageEnrollment(
        deal=deal,
        message_version=version,
        audience_key="   ",
        operator="   ",
    )

    with pytest.raises(ValidationError) as error:
        enrollment.full_clean()

    assert "audience_key" in error.value.message_dict
    assert "operator" in error.value.message_dict

    with pytest.raises(IntegrityError), transaction.atomic():
        CampaignMessageEnrollment.objects.create(
            deal=deal,
            message_version=version,
            audience_key="   ",
            operator="   ",
        )


def test_delivery_constraints_and_frozen_copy_immutability():
    program = MessageProgram.objects.create(key="csp-federal", name="CSP Federal")
    version = _version(program)
    enrollment = _enrollment(deal=_bound_deal(active_version=version), version=version)
    delivery = _delivery(enrollment=enrollment)

    delivery.status = OutboundDelivery.Status.QUEUED
    delivery.save(update_fields={"status", "updated_at"})
    delivery.frozen_body = "Mutated copy"
    with pytest.raises(ValidationError, match="immutable"):
        delivery.save()

    mismatched_operator = OutboundDelivery(
        enrollment=enrollment,
        channel=OutboundDelivery.Channel.LINKEDIN_CONNECT,
        step_key="connect",
        step_index=1,
        variant_key="control",
        operator="Chuka",
        scheduled_at=timezone.now(),
        frozen_body="Connection note",
        render_hash="e" * 64,
    )
    with pytest.raises(ValidationError, match="must match"):
        mismatched_operator.full_clean()

    with pytest.raises(IntegrityError), transaction.atomic():
        OutboundDelivery.objects.create(
            enrollment=enrollment,
            channel=OutboundDelivery.Channel.GMAIL,
            step_key=delivery.step_key,
            step_index=2,
            variant_key="alternate",
            operator="Arian",
            scheduled_at=timezone.now(),
            frozen_body="Duplicate keyed step",
            render_hash="f" * 64,
        )


def test_immutable_audit_relationships_use_protect_and_task_is_one_to_one():
    assert MessageProgramVersion._meta.get_field("program").remote_field.on_delete is PROTECT
    assert MessageProgramVersion._meta.get_field("based_on_version").remote_field.on_delete is PROTECT
    assert CampaignMessageEnrollment._meta.get_field("deal").remote_field.on_delete is PROTECT
    assert CampaignMessageEnrollment._meta.get_field("message_version").remote_field.on_delete is PROTECT
    assert OutboundDelivery._meta.get_field("enrollment").remote_field.on_delete is PROTECT
    assert OutboundDelivery._meta.get_field("task").remote_field.on_delete is PROTECT
    assert isinstance(OutboundDelivery._meta.get_field("task"), models.OneToOneField)

    program = MessageProgram.objects.create(key="csp-tech", name="CSP Technology")
    version = _version(program)
    enrollment = _enrollment(deal=_bound_deal(active_version=version), version=version)
    task = Task.objects.create(
        task_type=Task.TaskType.GMAIL_FOLLOW_UP,
        scheduled_at=timezone.now(),
        payload={"lead_id": enrollment.deal.lead_id, "operator": "Arian", "step_index": 0},
    )
    _delivery(enrollment=enrollment, task=task)

    with pytest.raises(IntegrityError), transaction.atomic():
        OutboundDelivery.objects.create(
            enrollment=enrollment,
            channel=OutboundDelivery.Channel.LINKEDIN_FOLLOWUP,
            step_key="linkedin-1",
            step_index=0,
            variant_key="control",
            operator="Arian",
            scheduled_at=timezone.now(),
            frozen_body="Follow-up",
            render_hash="9" * 64,
            task=task,
        )
