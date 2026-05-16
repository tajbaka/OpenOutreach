from django.conf import settings
from django.db import migrations, models


def _pick_owner(campaign, users_by_campaign_id, users_by_name):
    users = users_by_campaign_id.get(campaign.pk, [])
    if campaign.pk == 1:
        for user in users:
            if user.username == "chukyjack":
                return user
        chosen = users_by_name.get("chukyjack")
        if chosen is not None:
            return chosen
    return users[0] if users else None


def forwards(apps, schema_editor):
    Campaign = apps.get_model("linkedin", "Campaign")
    User = apps.get_model(settings.AUTH_USER_MODEL.split(".")[0], settings.AUTH_USER_MODEL.split(".")[1])

    through = Campaign.users.through
    campaigns = list(Campaign.objects.all().order_by("pk"))
    memberships = list(
        through.objects.select_related("user").order_by("campaign_id", "user_id")
    )

    users_by_campaign_id = {}
    for membership in memberships:
        users_by_campaign_id.setdefault(membership.campaign_id, []).append(membership.user)
    users_by_name = {user.username: user for user in User.objects.all()}

    for campaign in campaigns:
        owner = _pick_owner(campaign, users_by_campaign_id, users_by_name)
        if owner is None:
            raise RuntimeError(
                f"Campaign {campaign.pk} ({campaign.name}) has no assigned user; "
                "assign an owner before migrating to Campaign.user."
            )
        campaign.user_id = owner.pk
        campaign.save(update_fields=["user"])


class Migration(migrations.Migration):
    dependencies = [
        ("linkedin", "0004_remove_linkedinprofile_cookie_data"),
    ]

    operations = [
        migrations.AddField(
            model_name="campaign",
            name="user",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=models.deletion.CASCADE,
                related_name="campaigns",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.RunPython(forwards, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name="campaign",
            name="users",
        ),
        migrations.AlterField(
            model_name="campaign",
            name="user",
            field=models.ForeignKey(
                on_delete=models.deletion.CASCADE,
                related_name="campaigns",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
