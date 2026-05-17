from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("linkedin", "0005_campaign_user_fk"),
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
                ],
                max_length=20,
            ),
        ),
    ]
