import uuid

import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("crm", "0020_opportunity_active_account_admission"),
    ]

    operations = [
        migrations.AddField(
            model_name="opportunity",
            name="pipeline_stage",
            field=models.CharField(
                blank=True,
                choices=[
                    ("triage", "Potential / Triage"),
                    ("discovery", "Discovery"),
                    ("demo_evaluation", "Demo / Evaluation"),
                    ("pilot_validation", "Pilot / Validation"),
                    ("commercial_procurement", "Commercial / Procurement"),
                    ("nurture_later", "Nurture / Later"),
                    ("closed_won", "Closed Won"),
                    ("closed_lost", "Closed Lost"),
                ],
                db_index=True,
                default="",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="opportunity",
            name="pipeline_stage_entered_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.CreateModel(
            name="OpportunityTrelloState",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("board_id", models.CharField(max_length=64)),
                ("card_id", models.CharField(max_length=64, unique=True)),
                ("list_id", models.CharField(max_length=64)),
                ("published_pipeline_stage", models.CharField(blank=True, default="", max_length=32)),
                ("published_card_snapshot", models.JSONField(blank=True, default=dict)),
                ("trello_date_last_activity", models.DateTimeField(blank=True, null=True)),
                ("last_synced_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("opportunity", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="trello_state", to="crm.opportunity")),
            ],
        ),
        migrations.CreateModel(
            name="OpportunityPipelineEvent",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("from_stage", models.CharField(blank=True, default="", max_length=32)),
                ("to_stage", models.CharField(blank=True, default="", max_length=32)),
                ("source", models.CharField(choices=[("trello", "Trello"), ("manual", "Manual"), ("system", "System")], max_length=16)),
                ("changed_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("opportunity", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="pipeline_events", to="crm.opportunity")),
            ],
            options={"ordering": ["changed_at", "created_at", "id"]},
        ),
        migrations.AddIndex(
            model_name="opportunitytrellostate",
            index=models.Index(fields=["board_id", "list_id"], name="crm_trello_board_list_idx"),
        ),
        migrations.AddIndex(
            model_name="opportunitypipelineevent",
            index=models.Index(fields=["opportunity", "-changed_at"], name="crm_pipeline_opp_time_idx"),
        ),
    ]
