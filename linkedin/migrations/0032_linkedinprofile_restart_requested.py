from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("linkedin", "0031_campaign_gmail_start_mode")]

    operations = [
        migrations.AddField(
            model_name="linkedinprofile",
            name="restart_requested",
            field=models.BooleanField(
                default=False,
                help_text="Request a one-shot sender worker restart at the next supervisor poll.",
            ),
        ),
    ]
