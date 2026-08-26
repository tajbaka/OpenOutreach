from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("crm", "0019_email_first_lead_identity"),
    ]

    operations = [
        migrations.AddField(
            model_name="opportunity",
            name="active_account",
            field=models.BooleanField(db_index=True, default=True),
        ),
        migrations.AddField(
            model_name="opportunity",
            name="admission_evaluated_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="opportunity",
            name="admission_reason",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="opportunity",
            name="admission_reasons",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="opportunity",
            name="admission_tier",
            field=models.CharField(
                choices=[
                    ("authoritative", "Authoritative"),
                    ("primary", "Primary"),
                    ("secondary", "Secondary"),
                    ("weak", "Weak"),
                    ("none", "None"),
                ],
                db_index=True,
                default="none",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="opportunity",
            name="inactive_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="opportunity",
            name="inactive_reason",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddIndex(
            model_name="opportunity",
            index=models.Index(
                fields=["active_account", "admission_tier"],
                name="crm_opp_active_tier_idx",
            ),
        ),
    ]
