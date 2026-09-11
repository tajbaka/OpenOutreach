import pytest
from django.contrib.auth.models import User
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from crm.models import Deal, Lead
from linkedin.models import Campaign, CampaignMessageEnrollment, MessageProgram, MessageProgramVersion, OutboundDelivery, Task


@pytest.mark.django_db(transaction=True)
def test_migration_preserves_existing_campaigns_and_frozen_work():
    owner = User.objects.create(username="mode-migration")
    program = MessageProgram.objects.create(key="mode-migration", name="Migration fixture")
    version = MessageProgramVersion.objects.create(
        program=program, version=1, payload={}, content_hash="a" * 64, published_by="qa",
    )
    campaign = Campaign.objects.create(
        name="Existing migration campaign", user=owner, active_message_version=version,
        status="disabled", seed_public_ids=["preserve"],
    )
    deal = Deal.objects.create(campaign=campaign, lead=Lead.objects.create(first_name="QA"))
    enrollment = CampaignMessageEnrollment.objects.create(
        deal=deal, message_version=version, audience_key="keep-exact", operator="Arian",
    )
    due = timezone.now()
    task = Task.objects.create(
        task_type="gmail_follow_up", scheduled_at=due,
        payload={"campaign_id": campaign.pk, "lead_id": deal.lead_id, "operator": "Arian", "step_index": 0},
    )
    delivery = OutboundDelivery.objects.create(
        enrollment=enrollment, channel="gmail", step_key="email-1", step_index=0,
        variant_key="control", operator="Arian", scheduled_at=due, frozen_subject="Unchanged",
        frozen_body="Frozen copy", render_hash="b" * 64, task=task,
    )
    snapshots = {
        model: list(model.objects.filter(pk=record.pk).values())
        for model, record in ((Campaign, campaign), (Deal, deal), (CampaignMessageEnrollment, enrollment), (Task, task), (OutboundDelivery, delivery))
    }
    executor = MigrationExecutor(connection)
    leaves = executor.loader.graph.leaf_nodes()
    try:
        executor.migrate([("linkedin", "0030_message_program_versions")])
        old_apps = executor.loader.project_state([("linkedin", "0030_message_program_versions")]).apps
        old_campaign = old_apps.get_model("linkedin", "Campaign")
        assert "gmail_start_mode" not in {field.name for field in old_campaign._meta.fields}
        assert old_campaign.objects.get(pk=campaign.pk).status == "disabled"
        executor = MigrationExecutor(connection)
        executor.migrate(leaves)
        for model, expected in snapshots.items():
            assert list(model.objects.filter(pk=expected[0]["id"]).values()) == expected
        assert not Campaign.objects.exclude(gmail_start_mode="post_acceptance").exists()
    finally:
        MigrationExecutor(connection).migrate(leaves)
