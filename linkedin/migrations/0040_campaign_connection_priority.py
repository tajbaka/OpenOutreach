from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("linkedin", "0039_alter_task_task_type_rb2b")]

    operations = [
        migrations.AddField(
            model_name="campaign",
            name="connection_priority",
            field=models.PositiveSmallIntegerField(
                default=0,
                db_default=0,
                help_text=(
                    "Higher values take earlier due connection slots for this sender. "
                    "0 preserves normal ordering; limits, hours and follow-ups are unchanged."
                ),
            ),
        ),
    ]
