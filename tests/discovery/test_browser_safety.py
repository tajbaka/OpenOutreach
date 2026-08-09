import os

import pytest
from django.utils import timezone

from linkedin.discovery.browser_safety import assert_discovery_browser_available
from linkedin.exceptions import DiscoverySessionConflictError
from linkedin.models import DaemonHeartbeat


@pytest.mark.django_db
def test_fresh_daemon_heartbeat_blocks_standalone_discovery(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "linkedin.discovery.browser_safety.profile_dir_for",
        lambda username: tmp_path,
    )
    DaemonHeartbeat.objects.create(sender="Athena", last_alive=timezone.now())

    with pytest.raises(DiscoverySessionConflictError, match="heartbeat is fresh"):
        assert_discovery_browser_available(
            operator="Athena",
            account_username="athena@example.com",
        )


@pytest.mark.django_db
def test_chromium_lock_marker_blocks_standalone_discovery(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "linkedin.discovery.browser_safety.profile_dir_for",
        lambda username: tmp_path,
    )
    lock = tmp_path / "SingletonLock"
    lock.symlink_to("active-process")
    assert os.path.lexists(lock)

    with pytest.raises(DiscoverySessionConflictError, match="already owned"):
        assert_discovery_browser_available(
            operator="Athena",
            account_username="athena@example.com",
        )


@pytest.mark.django_db
def test_no_heartbeat_or_lock_allows_standalone_discovery(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "linkedin.discovery.browser_safety.profile_dir_for",
        lambda username: tmp_path,
    )

    assert_discovery_browser_available(
        operator="Athena",
        account_username="athena@example.com",
    )
