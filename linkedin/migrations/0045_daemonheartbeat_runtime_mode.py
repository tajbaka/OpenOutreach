from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("linkedin", "0044_campaign_channel_settings")]

    operations = [
        migrations.AddField(
            model_name="daemonheartbeat",
            name="runtime_mode",
            field=models.CharField(
                choices=[("linkedin", "LinkedIn and Gmail"), ("email_only", "Email only")],
                default="linkedin", db_default="linkedin", max_length=16,
            ),
        ),
    ]
