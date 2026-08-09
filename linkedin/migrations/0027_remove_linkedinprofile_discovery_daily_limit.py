from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("linkedin", "0026_alter_task_task_type_feed_like"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="linkedinprofile",
            name="discovery_daily_limit",
        ),
    ]
