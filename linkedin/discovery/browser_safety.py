"""Single-owner guard for standalone discovery browser commands."""
from __future__ import annotations

import os
from datetime import timedelta

from django.utils import timezone

from linkedin.browser.cookie_store import profile_dir_for
from linkedin.conf import PEER_STALE_MINUTES
from linkedin.exceptions import DiscoverySessionConflictError
from linkedin.models import DaemonHeartbeat


def assert_discovery_browser_available(
    *,
    operator: str,
    account_username: str,
) -> None:
    """Fail closed before a standalone command opens a sender's Chromium."""
    now = timezone.now()
    heartbeat = DaemonHeartbeat.objects.filter(sender=operator).first()
    if (
        heartbeat is not None
        and heartbeat.last_alive is not None
        and heartbeat.last_alive >= now - timedelta(minutes=PEER_STALE_MINUTES)
    ):
        raise DiscoverySessionConflictError(
            f"{operator}'s daemon heartbeat is fresh "
            f"({heartbeat.last_alive.isoformat()}); stop that daemon first",
        )

    profile_dir = profile_dir_for(account_username)
    markers = [
        profile_dir / "SingletonLock",
        profile_dir / "SingletonSocket",
        profile_dir / "SingletonCookie",
    ]
    present = [str(path) for path in markers if os.path.lexists(path)]
    if present:
        raise DiscoverySessionConflictError(
            "The sender's persistent Chromium profile is already owned: "
            + ", ".join(present),
        )

