"""Per-sender discovery save limits."""
from __future__ import annotations

from datetime import datetime

from linkedin.discovery.config import discovery_day_bounds
from linkedin.models import LinkedInDiscoveryLead


def saved_today(operator: str, *, now: datetime | None = None) -> int:
    start, end = discovery_day_bounds(now)
    return LinkedInDiscoveryLead.objects.filter(
        stored_by_operator=operator,
        created_at__gte=start,
        created_at__lt=end,
    ).count()


def remaining_today(profile, operator: str, *, now: datetime | None = None) -> int:
    profile.refresh_from_db(fields=["discovery_daily_limit"])
    return max(profile.discovery_daily_limit - saved_today(operator, now=now), 0)
