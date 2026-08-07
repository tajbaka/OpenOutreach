from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("linkedin", "0025_linkedinfeedcomment_feed_comment_task"),
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
                    ("feed_comment", "Feed Comment"),
                    ("feed_like", "Feed Like"),
                    ("status_summary", "Status Summary"),
                    ("discovery", "Discovery"),
                ],
                max_length=20,
            ),
        ),
    ]
