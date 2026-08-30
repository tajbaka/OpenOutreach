import json

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone


@pytest.mark.django_db(transaction=True)
def test_member_urn_migration_backfills_only_coherent_globally_unique_identity():
    executor = MigrationExecutor(connection)
    executor.migrate([("drip", "0001_initial")])
    old_apps = executor.loader.project_state([("drip", "0001_initial")]).apps
    Lead = old_apps.get_model("crm", "Lead")
    DripCampaign = old_apps.get_model("drip", "DripCampaign")
    DripCampaignVersion = old_apps.get_model("drip", "DripCampaignVersion")
    DripEnrollment = old_apps.get_model("drip", "DripEnrollment")
    DripLane = old_apps.get_model("drip", "DripLane")

    campaign = DripCampaign.objects.create(
        key="migration-identity",
        name="Migration identity",
    )
    version = DripCampaignVersion.objects.create(
        campaign=campaign,
        version=1,
        manifest={},
        content_hash="a" * 64,
    )

    def lane_for(public_id, urn, *, profile_public_id=None):
        lead = Lead.objects.create(
            linkedin_url=f"https://www.linkedin.com/in/{public_id}/",
            public_identifier=public_id,
            description=json.dumps(
                {
                    "url": f"https://www.linkedin.com/in/{profile_public_id or public_id}/",
                    "public_identifier": profile_public_id or public_id,
                    "urn": urn,
                },
            ),
            icp="CSPs",
        )
        enrollment = DripEnrollment.objects.create(
            campaign=campaign,
            campaign_version=version,
            lead=lead,
            frozen_icp="CSPs",
            enrolled_by="migration-test",
            plan_hash="b" * 64,
            activated_at=timezone.now(),
        )
        lane = DripLane.objects.create(
            enrollment=enrollment,
            channel="linkedin",
            operator="Arian",
            provider_account="arian",
            sender_identity="arian",
            recipient_identity=lead.linkedin_url,
        )
        return lead, lane

    _unique_lead, unique_lane = lane_for(
        "unique-person",
        "urn:li:fsd_profile:UNIQUE",
    )
    _mismatch_lead, mismatch_lane = lane_for(
        "mismatch-person",
        "urn:li:fsd_profile:WRONG",
        profile_public_id="someone-else",
    )
    _duplicate_lead, duplicate_lane = lane_for(
        "duplicate-person",
        "urn:li:fsd_profile:DUPLICATE",
    )
    Lead.objects.create(
        linkedin_url="https://www.linkedin.com/in/duplicate-shadow/",
        public_identifier="duplicate-shadow",
        description=json.dumps(
            {
                "url": "https://www.linkedin.com/in/duplicate-shadow/",
                "public_identifier": "duplicate-shadow",
                "urn": "urn:li:fsd_profile:DUPLICATE",
            },
        ),
    )

    executor = MigrationExecutor(connection)
    executor.migrate([("drip", "0002_freeze_linkedin_member_urn")])
    new_apps = executor.loader.project_state(
        [("drip", "0002_freeze_linkedin_member_urn")],
    ).apps
    NewLane = new_apps.get_model("drip", "DripLane")

    assert NewLane.objects.get(pk=unique_lane.pk).linkedin_member_urn == (
        "urn:li:fsd_profile:UNIQUE"
    )
    assert NewLane.objects.get(pk=mismatch_lane.pk).linkedin_member_urn == ""
    assert NewLane.objects.get(pk=duplicate_lane.pk).linkedin_member_urn == ""
