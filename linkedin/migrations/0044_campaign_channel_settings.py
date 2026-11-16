from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("linkedin", "0043_campaign_allow_connections_outside_hours")]

    operations = [
        migrations.AddField(
            model_name="campaign",
            name="enable_linkedin",
            field=models.BooleanField(
                default=True,
                db_default=True,
                help_text="Enable automated LinkedIn outreach for this campaign.",
            ),
        ),
        migrations.AddField(
            model_name="campaign",
            name="enable_gmail",
            field=models.BooleanField(
                default=True,
                db_default=True,
                help_text="Enable Gmail sequencing and email enrichment for this campaign.",
            ),
        ),
    ]
