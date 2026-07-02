from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("linkedin", "0016_merge_0014_slackleadcontextartifact_0015_rename_indexes"),
    ]

    operations = [
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
                ],
                max_length=20,
            ),
        ),
    ]
