import re

from django.db import migrations, models
import django.db.models.deletion


def _public_identifier_from_url(url):
    match = re.search(r"/in/([^/?#]+)/?", url or "")
    return match.group(1) if match else ""


def backfill_withdrawal_records(apps, schema_editor):
    Deal = apps.get_model("crm", "Deal")
    ActionLog = apps.get_model("linkedin", "ActionLog")
    InvitationWithdrawalRecord = apps.get_model(
        "linkedin",
        "InvitationWithdrawalRecord",
    )
    LinkedInProfile = apps.get_model("linkedin", "LinkedInProfile")

    for deal in (
        Deal.objects.filter(invitation_withdrawn_at__isnull=False)
        .select_related("lead", "campaign")
        .order_by("invitation_withdrawn_at", "id")
    ):
        if InvitationWithdrawalRecord.objects.filter(deal_id=deal.pk).exists():
            continue
        action_log = (
            ActionLog.objects.filter(
                campaign_id=deal.campaign_id,
                action_type="withdraw_invite",
                created_at__gte=deal.invitation_withdrawn_at,
            )
            .order_by("created_at", "id")
            .first()
        )
        linkedin_profile_id = (
            action_log.linkedin_profile_id if action_log is not None else None
        )
        if linkedin_profile_id is None:
            linkedin_profile_id = (
                LinkedInProfile.objects.filter(user_id=deal.campaign.user_id)
                .values_list("id", flat=True)
                .first()
            )
        if linkedin_profile_id is None:
            continue

        lead = deal.lead
        public_identifier = (
            (lead.public_identifier or "").strip()
            or _public_identifier_from_url(lead.linkedin_url)
        )
        if not public_identifier:
            continue
        displayed_name = (
            f"{lead.first_name or ''} {lead.last_name or ''}".strip()
            or public_identifier
        )
        InvitationWithdrawalRecord.objects.create(
            linkedin_profile_id=linkedin_profile_id,
            deal_id=deal.pk,
            public_identifier=public_identifier,
            linkedin_url=lead.linkedin_url or "",
            displayed_name=displayed_name,
            sent_label="",
            source="backfill",
            withdrawn_at=deal.invitation_withdrawn_at,
        )


class Migration(migrations.Migration):

    dependencies = [
        ("crm", "0015_deal_invitation_sender_deal_invitation_sent_at_and_more"),
        ("linkedin", "0023_linkedindiscoverylead_and_discovery_limit"),
    ]

    operations = [
        migrations.CreateModel(
            name="InvitationWithdrawalRecord",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("public_identifier", models.CharField(db_index=True, max_length=200)),
                ("linkedin_url", models.URLField(blank=True, default="", max_length=500)),
                ("displayed_name", models.CharField(blank=True, default="", max_length=220)),
                ("sent_label", models.CharField(blank=True, default="", max_length=80)),
                (
                    "source",
                    models.CharField(
                        choices=[
                            ("date_based", "Date Based"),
                            ("crm_matched", "CRM Matched"),
                            ("backfill", "Backfill"),
                        ],
                        db_index=True,
                        default="date_based",
                        max_length=20,
                    ),
                ),
                ("withdrawn_at", models.DateTimeField(db_index=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "deal",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="invitation_withdrawal_records",
                        to="crm.deal",
                    ),
                ),
                (
                    "linkedin_profile",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="invitation_withdrawal_records",
                        to="linkedin.linkedinprofile",
                    ),
                ),
            ],
            options={
                "indexes": [
                    models.Index(
                        fields=["linkedin_profile", "withdrawn_at"],
                        name="linkedin_iwr_profile_time_idx",
                    ),
                    models.Index(
                        fields=["deal", "withdrawn_at"],
                        name="linkedin_iwr_deal_time_idx",
                    ),
                ],
            },
        ),
        migrations.RunPython(
            backfill_withdrawal_records,
            migrations.RunPython.noop,
        ),
    ]
