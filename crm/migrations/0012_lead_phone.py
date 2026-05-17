from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("crm", "0011_lead_icp"),
    ]

    operations = [
        migrations.AddField(
            model_name="lead",
            name="phone",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
        migrations.AddField(
            model_name="lead",
            name="phone_enriched_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
