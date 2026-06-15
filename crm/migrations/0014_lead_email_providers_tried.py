from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("crm", "0013_lead_multi_phone"),
    ]

    operations = [
        migrations.AddField(
            model_name="lead",
            name="email_providers_tried",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
