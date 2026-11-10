"""Tests for `manage.py catch_up_replies` (CDP attach and LinkedIn reads mocked)."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone as dt_timezone
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from crm.models import Lead, Message
from linkedin.management.commands.catch_up_replies import lead_threads, parse_since
from linkedin.models import LinkedInProfile
from linkedin.realtime.catchup import CatchupResult, PendingNotice

SELF_URN = "urn:li:fsd_profile:ARIAN"
CONV = f"urn:li:msg_conversation:({SELF_URN},2-akanksha)"


def test_since_accepts_naive_iso_as_utc():
    assert parse_since(since="2026-09-10T00:00", hours=None) == datetime(
        2026, 9, 10, tzinfo=dt_timezone.utc,
    )


def test_hours_counts_back_from_now():
    now = datetime(2026, 11, 10, 12, tzinfo=dt_timezone.utc)
    assert parse_since(since=None, hours=12, now=now) == now - timedelta(hours=12)


@pytest.mark.parametrize("since,hours", [(None, None), ("2026-09-10", 3), ("nope", None), (None, -1)])
def test_bad_windows_are_rejected(since, hours):
    with pytest.raises(CommandError):
        parse_since(since=since, hours=hours)


@pytest.fixture
def lead(db):
    lead = Lead.objects.create(first_name="Akanksha", last_name="Test",
                               linkedin_url="https://www.linkedin.com/in/akanksha-test/")
    for external_id, thread in (("m1", CONV), ("m2", CONV),
                                ("other", "urn:li:msg_conversation:(urn:li:fsd_profile:EDDY,2-x)")):
        Message.objects.create(
            lead=lead, source=Message.Source.LINKEDIN, external_id=external_id,
            direction=Message.Direction.INBOUND, sender="akanksha test", body="hi",
            sent_at=timezone.now(), thread_external_id=thread,
        )
    return lead


def test_lead_threads_are_limited_to_this_mailbox(lead):
    assert lead_threads([lead.pk], self_urn=SELF_URN) == [CONV]


def test_command_reads_the_leads_thread_and_reports_what_it_would_post(lead):
    user = User.objects.create_user(username="arian")
    LinkedInProfile.objects.create(user=user, linkedin_username="arian@example.com")
    result = CatchupResult(since=timezone.now(), conversations=1, would_notify=1, pending=[
        PendingNotice(lead_id=lead.pk, name="Akanksha Test", sent_at="2026-09-10 08:17",
                      text="maybe let's connect", stored=True),
    ])

    @contextmanager
    def fake_page(_browser):
        yield MagicMock()

    playwright = MagicMock()
    playwright.__enter__.return_value.chromium.connect_over_cdp.return_value.contexts = [MagicMock()]
    out = StringIO()
    with patch("playwright.sync_api.sync_playwright", return_value=playwright), \
         patch("linkedin.feed_collection._feed_page", fake_page), \
         patch("linkedin.conf.get_daemon_handle", return_value="arian"), \
         patch("linkedin.api.messaging.utils.get_self_urn", return_value=SELF_URN), \
         patch("linkedin.realtime.catchup.catch_up_missed_replies", return_value=result) as run:
        call_command("catch_up_replies", "--since", "2026-09-10T00:00", "--lead", str(lead.pk), stdout=out)

    kwargs = run.call_args.kwargs
    assert kwargs["conversation_urns"] == [CONV]
    assert kwargs["apply"] is False
    assert kwargs["self_urn"] == SELF_URN
    assert kwargs["since"] == datetime(2026, 9, 10, tzinfo=dt_timezone.utc)
    text = out.getvalue()
    assert "PREVIEW" in text
    assert "saved earlier, never announced" in text
