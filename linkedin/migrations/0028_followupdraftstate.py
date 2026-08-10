from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("crm", "0015_deal_invitation_sender_deal_invitation_sent_at_and_more"),
        ("linkedin", "0027_remove_linkedinprofile_discovery_daily_limit"),
    ]

    operations = [
        migrations.CreateModel(
            name="FollowupDraftState",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("operator", models.CharField(blank=True, db_index=True, default="", max_length=64)),
                ("context_fingerprint", models.CharField(blank=True, default="", max_length=64)),
                ("decision", models.JSONField(blank=True, default=dict)),
                ("active", models.BooleanField(db_index=True, default=True)),
                ("eligible", models.BooleanField(db_index=True, default=True)),
                ("reviewed_at", models.DateTimeField(auto_now=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("lead", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="followup_draft_state", to="crm.lead")),
            ],
            options={
                "indexes": [models.Index(fields=["active", "eligible", "operator"], name="linkedin_fo_active_c66a59_idx")],
            },
        ),
    ]
