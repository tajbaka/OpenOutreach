from django.db import migrations, models
from django.utils import timezone


def retire_withdraw_invites_tasks(apps, schema_editor):
    Task = apps.get_model("linkedin", "Task")
    Task.objects.filter(
        task_type="withdraw_invites",
        status__in=["pending", "running"],
    ).update(
        status="failed",
        error=(
            "Automatic invitation withdrawal was retired; use the "
            "withdraw_invitations management command"
        ),
        completed_at=timezone.now(),
    )


class Migration(migrations.Migration):

    dependencies = [
        ("linkedin", "0021_alter_actionlog_action_type_alter_task_task_type"),
    ]

    operations = [
        migrations.RunPython(
            retire_withdraw_invites_tasks,
            reverse_code=migrations.RunPython.noop,
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
                ],
                max_length=20,
            ),
        ),
    ]
