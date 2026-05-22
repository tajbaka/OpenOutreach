from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("linkedin", "0008_task_ownership_hardening"),
    ]

    operations = [
        migrations.CreateModel(
            name="ConnectIssueLog",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("public_id", models.CharField(db_index=True, max_length=200)),
                ("profile_url", models.URLField(blank=True, default="", max_length=500)),
                ("issue_type", models.CharField(choices=[("connect_button_missing", "Connect Button Missing"), ("more_connect_no_surface", "More Connect No Surface"), ("note_ui_missing", "Note UI Missing"), ("note_textarea_missing", "Note Textarea Missing"), ("send_button_missing", "Send Button Missing"), ("skip_profile", "Skip Profile")], max_length=40)),
                ("reason", models.TextField(blank=True, default="")),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("campaign", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="connect_issue_logs", to="linkedin.campaign")),
                ("linkedin_profile", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="connect_issue_logs", to="linkedin.linkedinprofile")),
            ],
            options={
                "indexes": [
                    models.Index(fields=["linkedin_profile", "issue_type", "created_at"], name="linkedin_co_linkedi_114aa0_idx"),
                    models.Index(fields=["public_id", "created_at"], name="linkedin_co_public__4b8c93_idx"),
                ],
            },
        ),
    ]
