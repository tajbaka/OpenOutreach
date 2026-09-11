from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("linkedin", "0030_message_program_versions")]

    operations = [
        migrations.AddField(
            model_name="campaign",
            name="gmail_start_mode",
            field=models.CharField(
                choices=[
                    ("post_acceptance", "Post-acceptance: after LinkedIn acceptance"),
                    ("invitation_sent", "Pre-acceptance: after a confirmed invitation"),
                ],
                default="post_acceptance",
                help_text="Choose when the existing email sequence starts; LinkedIn follow-ups still wait for acceptance.",
                max_length=24,
            ),
        ),
        migrations.AddConstraint(
            model_name="campaign",
            constraint=models.CheckConstraint(
                condition=models.Q(gmail_start_mode__in=("post_acceptance", "invitation_sent")),
                name="linkedin_campaign_gmail_start_mode_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="campaign",
            constraint=models.CheckConstraint(
                condition=~models.Q(gmail_start_mode="invitation_sent")
                | models.Q(active_message_version__isnull=False),
                name="linkedin_campaign_invite_email_bound",
            ),
        ),
    ]
