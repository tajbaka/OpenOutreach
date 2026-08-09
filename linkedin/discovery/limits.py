"""Per-sender discovery save limits."""
from __future__ import annotations

from datetime import datetime

from linkedin import conf
from linkedin.discovery.config import discovery_day_bounds
from linkedin.models import LinkedInDiscoveryLead


def saved_today(operator: str, *, now: datetime | None = None) -> int:
    start, end = discovery_day_bounds(now)
    return LinkedInDiscoveryLead.objects.filter(
        stored_by_operator=operator,
        created_at__gte=start,
        created_at__lt=end,
    ).count()


def remaining_today(operator: str, *, now: datetime | None = None) -> int:
    return max(conf.DISCOVERY_DAILY_LIMIT - saved_today(operator, now=now), 0)
