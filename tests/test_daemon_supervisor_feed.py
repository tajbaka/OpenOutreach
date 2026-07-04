from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import daemon_supervisor


def test_feed_collection_due_today_after_configured_local_time(monkeypatch):
    monkeypatch.setenv("ENABLE_LINKEDIN_FEED_COLLECTOR", "true")
    monkeypatch.setenv("LINKEDIN_FEED_COLLECTION_HOUR", "17")
    monkeypatch.setenv("LINKEDIN_FEED_COLLECTION_MINUTE", "0")

    before = datetime(2026, 7, 3, 16, 59, tzinfo=ZoneInfo("America/Toronto"))
    after = datetime(2026, 7, 3, 17, 0, tzinfo=ZoneInfo("America/Toronto"))

    assert daemon_supervisor._feed_collection_due_today(before) is False
    assert daemon_supervisor._feed_collection_due_today(after) is True


def test_feed_collection_due_today_respects_feature_flag(monkeypatch):
    monkeypatch.setenv("ENABLE_LINKEDIN_FEED_COLLECTOR", "false")

    now = datetime(2026, 7, 3, 18, 0, tzinfo=ZoneInfo("America/Toronto"))

    assert daemon_supervisor._feed_collection_due_today(now) is False
