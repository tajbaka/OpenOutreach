from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('linkedin', '0036_linkedinrepost_scheduled_for'),
    ]

    operations = [
        migrations.AddField(
            model_name='linkedinrepost',
            name='retry_count',
            field=models.PositiveSmallIntegerField(db_default=0, default=0),
        ),
    ]
