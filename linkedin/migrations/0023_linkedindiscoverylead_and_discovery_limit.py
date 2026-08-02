import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("linkedin", "0022_retire_withdraw_invites_task"),
    ]

    operations = [
        migrations.AddField(
            model_name="linkedinprofile",
            name="discovery_daily_limit",
            field=models.PositiveIntegerField(default=25),
        ),
        migrations.CreateModel(
            name="LinkedInDiscoveryLead",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("public_identifier", models.CharField(max_length=200, unique=True)),
                ("linkedin_url", models.URLField(max_length=500, unique=True)),
                (
                    "member_urn",
                    models.CharField(blank=True, db_index=True, default="", max_length=255),
                ),
                ("first_name", models.CharField(blank=True, default="", max_length=100)),
                ("last_name", models.CharField(blank=True, default="", max_length=100)),
                ("full_name", models.CharField(blank=True, default="", max_length=220)),
                ("headline", models.TextField(blank=True, default="")),
                ("company_name", models.CharField(blank=True, default="", max_length=300)),
                ("location", models.CharField(blank=True, default="", max_length=300)),
                ("profile_data", models.JSONField(default=dict)),
                ("stored_by_operator", models.CharField(db_index=True, max_length=80)),
                (
                    "stored_by_account_username",
                    models.CharField(db_index=True, max_length=200),
                ),
                ("potential_icp", models.CharField(db_index=True, max_length=100)),
                (
                    "last_seen_at",
                    models.DateTimeField(db_index=True, default=django.utils.timezone.now),
                ),
                ("last_profiled_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "indexes": [
                    models.Index(
                        fields=["stored_by_operator", "created_at"],
                        name="ldiscovery_sender_day_idx",
                    ),
                    models.Index(
                        fields=["potential_icp", "created_at"],
                        name="ldiscovery_icp_day_idx",
                    ),
                ],
            },
        ),
        migrations.AlterField(
            model_name="task",
            name="task_type",
            field=models.CharField(
                choices=[
                    ("connect", "Connect"),
                    ("check_pending", "Check Pending"),
                    ("follow_up", "Follow Up"),
                    ("sweep_connections", "Sweep Connections"),
                    ("enrich_phone", "Enrich Phone"),
                    ("enrich_email", "Enrich Email"),
                    ("gmail_follow_up", "Gmail Follow Up"),
                    ("manual_reply", "Manual Reply"),
                    ("status_summary", "Status Summary"),
                    ("discovery", "Discovery"),
                ],
                max_length=20,
            ),
        ),
    ]
