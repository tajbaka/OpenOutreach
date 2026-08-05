import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("linkedin", "0024_invitationwithdrawalrecord"),
    ]

    operations = [
        migrations.CreateModel(
            name="LinkedInFeedComment",
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
                ("operator", models.CharField(db_index=True, max_length=80)),
                (
                    "account_username",
                    models.CharField(blank=True, default="", max_length=200),
                ),
                ("comment_text", models.TextField()),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("queued", "Queued"),
                            ("running", "Running"),
                            ("sent", "Sent"),
                            ("failed", "Failed"),
                            ("uncertain", "Uncertain"),
                            ("skipped", "Skipped"),
                        ],
                        db_index=True,
                        default="queued",
                        max_length=20,
                    ),
                ),
                (
                    "slack_channel_id",
                    models.CharField(blank=True, default="", max_length=80),
                ),
                (
                    "slack_message_ts",
                    models.CharField(blank=True, default="", max_length=80),
                ),
                (
                    "slack_response_url",
                    models.URLField(blank=True, default="", max_length=1000),
                ),
                (
                    "slack_user_id",
                    models.CharField(blank=True, default="", max_length=80),
                ),
                ("submit_attempted_at", models.DateTimeField(blank=True, null=True)),
                ("commented_at", models.DateTimeField(blank=True, null=True)),
                ("error", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "post",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="comments",
                        to="linkedin.linkedinfeedpost",
                    ),
                ),
                (
                    "task",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="feed_comments",
                        to="linkedin.task",
                    ),
                ),
            ],
            options={
                "indexes": [
                    models.Index(
                        fields=["post", "operator", "created_at"],
                        name="lfeed_comment_post_op_idx",
                    ),
                    models.Index(
                        fields=["operator", "status", "created_at"],
                        name="lfeed_comment_op_status_idx",
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
                    ("feed_comment", "Feed Comment"),
                    ("status_summary", "Status Summary"),
                    ("discovery", "Discovery"),
                ],
                max_length=20,
            ),
        ),
    ]
