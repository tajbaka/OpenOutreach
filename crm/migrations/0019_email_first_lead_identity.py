from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("crm", "0018_opportunity_action_target_lead"),
    ]

    operations = [
        migrations.AlterField(
            model_name="lead",
            name="linkedin_url",
            field=models.URLField(blank=True, db_index=True, default="", max_length=200),
        ),
        migrations.AddConstraint(
            model_name="lead",
            constraint=models.UniqueConstraint(
                condition=~models.Q(linkedin_url=""),
                fields=("linkedin_url",),
                name="unique_nonblank_lead_linkedin_url",
            ),
        ),
    ]
