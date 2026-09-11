import pytest
from django.contrib.auth.models import User
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from linkedin.models import LinkedInProfile


@pytest.mark.django_db(transaction=True)
def test_restart_migration_defaults_false_and_preserves_existing_profile_fields():
    records = [
        LinkedInProfile.objects.create(
            user=User.objects.create(username=f"migration-sender-{index}"),
            linkedin_username=f"migration-sender-{index}@example.invalid",
            linkedin_password="synthetic-migration-password",
            active=active,
            subscribe_newsletter=False,
            legal_accepted=True,
            newsletter_processed=True,
            connect_daily_limit=7 + index,
            connect_weekly_limit=31 + index,
            follow_up_daily_limit=9 + index,
        )
        for index, active in enumerate((False, True))
    ]
    expected = list(LinkedInProfile.objects.filter(pk__in=[profile.pk for profile in records]).order_by("pk").values())
    executor = MigrationExecutor(connection)
    leaves = executor.loader.graph.leaf_nodes()
    previous = [("linkedin", "0031_campaign_gmail_start_mode")]

    try:
        executor.migrate(previous)
        old_apps = executor.loader.project_state(previous).apps
        old_profile = old_apps.get_model("linkedin", "LinkedInProfile")
        assert "restart_requested" not in {field.name for field in old_profile._meta.fields}
        assert list(old_profile.objects.order_by("pk").values()) == [
            {key: value for key, value in profile.items() if key not in {"restart_requested", "stop_requested"}}
            for profile in expected
        ]

        MigrationExecutor(connection).migrate(leaves)

        assert list(LinkedInProfile.objects.order_by("pk").values()) == expected
        assert not LinkedInProfile.objects.exclude(restart_requested=False).exists()
    finally:
        MigrationExecutor(connection).migrate(leaves)
