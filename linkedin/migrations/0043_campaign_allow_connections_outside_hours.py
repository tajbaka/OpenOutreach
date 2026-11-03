from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("linkedin", "0042_linkedinpost")]

    operations = [
        migrations.AddField(
            model_name="campaign",
            name="allow_connections_outside_hours",
            field=models.BooleanField(
                "Allow connections outside working hours",
                default=False,
                db_default=False,
                help_text="Allow LinkedIn invitations outside the sender's working hours and on rest days.",
            ),
        ),
    ]
