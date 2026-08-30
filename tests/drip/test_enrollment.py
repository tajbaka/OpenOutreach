import json
import stat

import pytest
from django.core.management import call_command

from crm.models import Deal, Lead
from drip.exceptions import EnrollmentPlanError
from drip.manifest import validate_manifest
from drip.models import DripEnrollment, DripLane
from drip.services.enrollment import (
    apply_reviewed_plan,
    build_enrollment_plan,
    validate_reviewed_plan,
    write_enrollment_plan,
)
from drip.services.publication import publish_manifest
from linkedin.enums import ProfileState
from linkedin.models import Campaign
from tests.factories import UserFactory


pytestmark = pytest.mark.django_db


def _eligible_lead():
    user = UserFactory(username="arian")
    current_campaign = Campaign.objects.create(name="Current outbound", user=user)
    lead = Lead.objects.create(
        first_name="Ada",
        last_name="Lovelace",
        company_name="Analytical Engines",
        linkedin_url="https://www.linkedin.com/in/ada-lovelace/",
        public_identifier="ada-lovelace",
        email="ADA@EXAMPLE.COM",
        icp="CSPs",
    )
    Deal.objects.create(
        lead=lead,
        campaign=current_campaign,
        state=ProfileState.CONNECTED,
        invitation_sender="Arian",
    )
    return lead


def test_reviewed_plan_is_explicit_private_and_applies_atomically(
    valid_drip_payload,
    tmp_path,
):
    published = publish_manifest(validate_manifest(valid_drip_payload))
    lead = _eligible_lead()
    plan = build_enrollment_plan(
        campaign_key=published.campaign.key,
        operator="Arian",
        lead_ids=[lead.pk],
    )
    target = write_enrollment_plan(plan, tmp_path / "review.json")

    assert plan["leads"][0]["eligible"] is True
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    result = apply_reviewed_plan(
        campaign_key=published.campaign.key,
        plan=plan,
        reviewed_by="human-reviewer",
    )

    enrollment = DripEnrollment.objects.get(pk=result.created_enrollment_ids[0])
    assert enrollment.lead == lead
    assert enrollment.enrolled_by == "human-reviewer"
    assert enrollment.plan_hash == plan["plan_hash"]
    assert set(enrollment.lanes.values_list("channel", flat=True)) == {
        DripLane.Channel.LINKEDIN,
        DripLane.Channel.GMAIL,
    }
    gmail_lane = enrollment.lanes.get(channel=DripLane.Channel.GMAIL)
    assert gmail_lane.provider_account == "arian_boundera"
    assert gmail_lane.sender_identity == "ariant@getboundera.com"
    assert gmail_lane.recipient_identity == "ada@example.com"


def test_review_artifact_refuses_overwrite(valid_drip_payload, tmp_path):
    published = publish_manifest(validate_manifest(valid_drip_payload))
    lead = _eligible_lead()
    plan = build_enrollment_plan(
        campaign_key=published.campaign.key,
        operator="Arian",
        lead_ids=[lead.pk],
    )
    target = write_enrollment_plan(plan, tmp_path / "review.json")

    with pytest.raises(EnrollmentPlanError, match="Refusing to overwrite"):
        write_enrollment_plan(plan, target)


def test_tampered_or_stale_plan_fails_closed(valid_drip_payload):
    published = publish_manifest(validate_manifest(valid_drip_payload))
    lead = _eligible_lead()
    plan = build_enrollment_plan(
        campaign_key=published.campaign.key,
        operator="Arian",
        lead_ids=[lead.pk],
    )
    tampered = json.loads(json.dumps(plan))
    tampered["operator"] = "Chuka"
    with pytest.raises(EnrollmentPlanError, match="plan hash is invalid"):
        validate_reviewed_plan(campaign_key=published.campaign.key, plan=tampered)

    lead.email = "changed@example.com"
    lead.save(update_fields={"email", "update_date"})
    with pytest.raises(EnrollmentPlanError, match="changed after review"):
        validate_reviewed_plan(campaign_key=published.campaign.key, plan=plan)


def test_known_reply_is_an_explicit_plan_blocker(valid_drip_payload):
    from crm.models import Message
    from django.utils import timezone

    published = publish_manifest(validate_manifest(valid_drip_payload))
    lead = _eligible_lead()
    Message.objects.create(
        lead=lead,
        source=Message.Source.GMAIL,
        direction=Message.Direction.INBOUND,
        external_id="known-reply",
        sender=lead.email,
        body="Not interested",
        sent_at=timezone.now(),
    )
    plan = build_enrollment_plan(
        campaign_key=published.campaign.key,
        operator="Arian",
        lead_ids=[lead.pk],
    )

    assert plan["leads"][0]["eligible"] is False
    assert "historical_inbound_reply" in plan["leads"][0]["blockers"]
    with pytest.raises(EnrollmentPlanError, match="not eligible"):
        apply_reviewed_plan(
            campaign_key=published.campaign.key,
            plan=plan,
            reviewed_by="reviewer",
        )


def test_plan_command_requires_explicit_leads_and_publish_is_dry_run_by_default(
    valid_drip_payload,
    tmp_path,
    capsys,
):
    manifest_path = tmp_path / "campaign.json"
    manifest_path.write_text(json.dumps(valid_drip_payload), encoding="utf-8")
    call_command("publish_drip_campaign", str(manifest_path))
    assert not DripEnrollment.objects.exists()
    from drip.models import DripCampaign

    assert not DripCampaign.objects.exists()
    call_command("publish_drip_campaign", str(manifest_path), apply=True)
    lead = _eligible_lead()
    output = tmp_path / "plan.json"
    call_command(
        "plan_drip_enrollments",
        valid_drip_payload["campaign_key"],
        operator="Arian",
        lead_ids=[lead.pk],
        output=str(output),
    )
    assert output.exists()
    assert "Dry run" in capsys.readouterr().out
