# Generated manually for the Slack lead-context artifact store.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("crm", "0013_lead_multi_phone"),
        ("linkedin", "0013_alter_task_task_type_manual_reply"),
    ]

    operations = [
        migrations.CreateModel(
            name="SlackLeadContextArtifact",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("operator", models.CharField(blank=True, default="", max_length=80)),
                ("thread_external_id", models.CharField(blank=True, default="", max_length=512)),
                ("kind", models.CharField(choices=[("ai_summary", "Ai Summary"), ("draft_reply", "Draft Reply")], max_length=32)),
                ("content", models.TextField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("lead", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="slack_context_artifacts", to="crm.lead")),
            ],
            options={
                "indexes": [
                    models.Index(
                        fields=["lead", "operator", "thread_external_id"],
                        name="slack_lead_ctx_scope_idx",
                    ),
                ],
                "constraints": [
                    models.UniqueConstraint(fields=("lead", "operator", "thread_external_id", "kind"), name="uniq_slack_lead_context_artifact_scope"),
                ],
            },
        ),
    ]
