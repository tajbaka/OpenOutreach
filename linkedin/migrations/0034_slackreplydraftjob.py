import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("linkedin", "0033_linkedinprofile_stop_requested")]

    operations = [
        migrations.CreateModel(
            name="SlackReplyDraftJob",
            fields=[
                ("request_key", models.CharField(max_length=64, primary_key=True, serialize=False)),
                ("operator", models.CharField(max_length=80)),
                ("thread_external_id", models.CharField(blank=True, default="", max_length=512)),
                ("view_id", models.CharField(max_length=128)),
                ("status", models.CharField(choices=[("running", "Running"), ("ready", "Ready"), ("failed", "Failed")], max_length=16)),
                ("lease_token", models.CharField(max_length=32)),
                ("lease_until", models.DateTimeField()),
                ("content", models.TextField(blank=True, default="")),
                ("error_code", models.CharField(blank=True, default="", max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("lead", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="crm.lead")),
            ],
        ),
    ]
