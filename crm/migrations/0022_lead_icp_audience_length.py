from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("crm", "0021_lead_role_tag")]

    operations = [
        migrations.AlterField(
            model_name="lead",
            name="icp",
            field=models.CharField(max_length=160, blank=True, default="", db_index=True),
        ),
    ]
