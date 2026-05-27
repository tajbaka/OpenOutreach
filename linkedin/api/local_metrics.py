from __future__ import annotations

from django.db.models import Count
from django.http import JsonResponse
from django.utils import timezone

from linkedin.conf import ACTIVE_TIMEZONE
from linkedin.models import ActionLog, LinkedInProfile, active_day_start

DEFAULT_CONNECT_COUNT_HANDLES = ("ariantajbakh", "chukyjack", "leiliash2011")


def _requested_handles(raw_handles: str) -> list[str]:
    if not raw_handles:
        return list(DEFAULT_CONNECT_COUNT_HANDLES)
    if raw_handles.strip().lower() == "all":
        return list(
            LinkedInProfile.objects.select_related("user")
            .filter(active=True)
            .order_by("user__username")
            .values_list("user__username", flat=True)
        )
    return [handle.strip() for handle in raw_handles.split(",") if handle.strip()]


def connect_counts_today(request):
    handles = _requested_handles(request.GET.get("handles", ""))
    start = active_day_start()
    profiles = list(
        LinkedInProfile.objects.select_related("user")
        .filter(user__username__in=handles)
        .order_by("user__username")
    )
    counts = {
        row["linkedin_profile__user__username"]: row["count"]
        for row in ActionLog.objects.filter(
            action_type=ActionLog.ActionType.CONNECT,
            created_at__gte=start,
            linkedin_profile__user__username__in=handles,
        )
        .values("linkedin_profile__user__username")
        .annotate(count=Count("id"))
    }
    by_handle = {
        profile.user.username: {
            "handle": profile.user.username,
            "linkedin_username": profile.linkedin_username,
            "active": profile.active,
            "connects_today": counts.get(profile.user.username, 0),
            "connect_daily_limit": profile.connect_daily_limit,
        }
        for profile in profiles
    }

    return JsonResponse(
        {
            "action_type": ActionLog.ActionType.CONNECT,
            "active_timezone": ACTIVE_TIMEZONE,
            "active_day_start": start.isoformat(),
            "generated_at": timezone.now().isoformat(),
            "total": sum(item["connects_today"] for item in by_handle.values()),
            "profiles": [by_handle[handle] for handle in handles if handle in by_handle],
            "missing_handles": [handle for handle in handles if handle not in by_handle],
        }
    )
