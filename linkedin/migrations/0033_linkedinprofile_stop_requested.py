from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("linkedin", "0032_linkedinprofile_restart_requested")]

    operations = [
        migrations.AddField(
            model_name="linkedinprofile",
            name="stop_requested",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "Latch an emergency stop for this sender's supervised workers. "
                    "Remains set until explicitly cleared; clearing does not restart workers."
                ),
            ),
        ),
    ]
