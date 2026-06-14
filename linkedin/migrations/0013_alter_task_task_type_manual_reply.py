from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("linkedin", "0012_daemonheartbeat_activity_alerted_at"),
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
                    ("manual_reply", "Manual Reply"),
                ],
                max_length=20,
            ),
        ),
    ]
