from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('linkedin', '0035_linkedinrepost'),
    ]

    operations = [
        migrations.AddField(
            model_name='linkedinrepost',
            name='scheduled_for',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddIndex(
            model_name='linkedinrepost',
            index=models.Index(fields=['operator', 'status', 'scheduled_for'], name='lrepost_op_due_idx'),
        ),
    ]
