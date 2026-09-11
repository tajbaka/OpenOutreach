import pytest
from django.contrib.auth.models import User
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from linkedin.models import LinkedInProfile


@pytest.mark.django_db(transaction=True)
def test_stop_migration_defaults_false_and_preserves_existing_profile_and_restart_flags():
    records = [
        LinkedInProfile.objects.create(
            user=User.objects.create(username=f"stop-migration-{index}"),
            linkedin_username=f"stop-migration-{index}@example.invalid",
            linkedin_password="synthetic-only", active=active,
            restart_requested=active, subscribe_newsletter=False,
            legal_accepted=True, connect_daily_limit=7 + index,
        )
        for index, active in enumerate((False, True))
    ]
    expected = list(LinkedInProfile.objects.filter(pk__in=[row.pk for row in records]).order_by("pk").values())
    executor = MigrationExecutor(connection)
    leaves = executor.loader.graph.leaf_nodes()
    previous = [("linkedin", "0032_linkedinprofile_restart_requested")]

    try:
        executor.migrate(previous)
        old_apps = executor.loader.project_state(previous).apps
        old_profile = old_apps.get_model("linkedin", "LinkedInProfile")
        assert "stop_requested" not in {field.name for field in old_profile._meta.fields}
        assert list(old_profile.objects.order_by("pk").values()) == [
            {key: value for key, value in profile.items() if key != "stop_requested"}
            for profile in expected
        ]
        MigrationExecutor(connection).migrate(leaves)
        assert list(LinkedInProfile.objects.order_by("pk").values()) == expected
        assert not LinkedInProfile.objects.exclude(stop_requested=False).exists()
    finally:
        MigrationExecutor(connection).migrate(leaves)
