from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("crm", "0016_canonical_sales_crm"),
    ]

    operations = [
        migrations.AddField(
            model_name="opportunityaction",
            name="channel",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
        migrations.AddField(
            model_name="opportunityaction",
            name="draft",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="opportunityaction",
            name="human_revision",
            field=models.PositiveBigIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="opportunityaction",
            name="sheet_human_snapshot",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="opportunityaction",
            name="sheet_published_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
