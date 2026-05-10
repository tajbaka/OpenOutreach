"""Unit tests for linkedin.notifications.gmail_threads.

Network-free — we feed pre-shaped MCP-like payloads into persist_gmail_threads
and assert what landed in the DB. The actual MCP calls happen in the
orchestration layer, which is intentionally out of scope here.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from django.utils import timezone as dj_tz

from crm.models import Lead, Message
from linkedin.notifications import gmail_threads


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


HOST = "eddy@tryfedrampgpt.com"


@pytest.fixture
def lead(db):
    return Lead.objects.create(
        first_name="Sarah", last_name="Lange",
        linkedin_url="https://www.linkedin.com/in/sarah-lange/",
        email="sarah.lange@prescientsecurity.com",
    )


def _gmail_msg(*, id, from_, body, when):
    """MCP-style message dict with RFC2822 headers list."""
    return {
        "id": id,
        "headers": [
            {"name": "From", "value": from_},
            {"name": "Date", "value": when.strftime("%a, %d %b %Y %H:%M:%S +0000")},
        ],
        "snippet": body,
        "internalDate": str(int(when.timestamp() * 1000)),
    }


# ---------------------------------------------------------------------------
# persist_gmail_threads
# ---------------------------------------------------------------------------


def test_persist_creates_messages_with_direction_inferred(lead):
    out_when = datetime(2026, 4, 8, 16, 0, tzinfo=timezone.utc)
    in_when = datetime(2026, 4, 9, 9, 30, tzinfo=timezone.utc)
    threads = [{
        "id": "thread-1",
        "messages": [
            _gmail_msg(id="msg-out-1", from_=f"Chuka <{HOST}>",
                       body="Hi Sarah, thanks for the call.", when=out_when),
            _gmail_msg(id="msg-in-1", from_="Sarah Lange <sarah.lange@prescientsecurity.com>",
                       body="Sounds good — sharing slides this week.", when=in_when),
        ],
    }]
    created = gmail_threads.persist_gmail_threads(
        lead=lead, threads=threads, host_email=HOST,
    )
    assert created == 2

    msgs = list(lead.messages.order_by("sent_at"))
    assert msgs[0].source == Message.Source.GMAIL
    assert msgs[0].direction == Message.Direction.OUTBOUND
    assert msgs[0].external_id == "msg-out-1"
    assert msgs[0].thread_external_id == "thread-1"
    assert msgs[0].sender == HOST
    assert msgs[1].direction == Message.Direction.INBOUND
    assert "slides this week" in msgs[1].body


def test_persist_is_idempotent(lead):
    when = datetime(2026, 4, 8, 16, 0, tzinfo=timezone.utc)
    threads = [{
        "id": "t1",
        "messages": [
            _gmail_msg(id="dup", from_=HOST, body="hi", when=when),
        ],
    }]
    a = gmail_threads.persist_gmail_threads(lead=lead, threads=threads, host_email=HOST)
    b = gmail_threads.persist_gmail_threads(lead=lead, threads=threads, host_email=HOST)
    assert a == 1 and b == 0
    assert lead.messages.count() == 1


def test_persist_team_emails_count_as_outbound(lead):
    when = datetime(2026, 4, 8, 16, 0, tzinfo=timezone.utc)
    threads = [{
        "id": "t1",
        "messages": [
            _gmail_msg(id="m1", from_="Arian Taj <arian@tryfedrampgpt.com>",
                       body="follow-up", when=when),
        ],
    }]
    gmail_threads.persist_gmail_threads(
        lead=lead, threads=threads, host_email=HOST,
        team_emails=["arian@tryfedrampgpt.com"],
    )
    msg = lead.messages.get()
    assert msg.direction == Message.Direction.OUTBOUND


def test_persist_unknown_sender_is_inbound(lead):
    """Anyone not in the host/team set is treated as the lead replying.
    1:1 outreach threads only have two participants — same logic as the
    LinkedIn persist helper."""
    when = datetime(2026, 4, 8, 16, 0, tzinfo=timezone.utc)
    threads = [{
        "id": "t1",
        "messages": [_gmail_msg(id="m1", from_="someone@elsewhere.com",
                                body="text", when=when)],
    }]
    gmail_threads.persist_gmail_threads(lead=lead, threads=threads, host_email=HOST)
    assert lead.messages.get().direction == Message.Direction.INBOUND


def test_persist_skips_messages_without_id(lead):
    when = datetime(2026, 4, 8, 16, 0, tzinfo=timezone.utc)
    threads = [{
        "id": "t1",
        "messages": [
            {"headers": [{"name": "From", "value": HOST}], "snippet": "no id"},
            _gmail_msg(id="ok", from_=HOST, body="real", when=when),
        ],
    }]
    created = gmail_threads.persist_gmail_threads(
        lead=lead, threads=threads, host_email=HOST,
    )
    assert created == 1


def test_persist_raises_when_host_email_missing(lead, monkeypatch):
    """Without a known sender to mark outbound, direction inference is
    meaningless. Crash early per the project's error-handling rule.
    Monkeypatch the conf-level defaults so a populated dev .env doesn't
    silently rescue the test."""
    from linkedin.exceptions import SheetsError
    monkeypatch.setattr(gmail_threads, "HOST_EMAIL", "")
    monkeypatch.setattr(gmail_threads, "TEAM_EMAILS", ())
    threads = [{"id": "t1", "messages": []}]
    with pytest.raises(SheetsError):
        gmail_threads.persist_gmail_threads(
            lead=lead, threads=threads, host_email="", team_emails=[],
        )


def test_persist_handles_internalDate_only(lead):
    """Gmail-API responses sometimes omit Date header, only internalDate."""
    when_ms = int(datetime(2026, 4, 8, 16, 0, tzinfo=timezone.utc).timestamp() * 1000)
    threads = [{
        "id": "t1",
        "messages": [{
            "id": "m1",
            "headers": [{"name": "From", "value": HOST}],
            "snippet": "hello",
            "internalDate": str(when_ms),
        }],
    }]
    gmail_threads.persist_gmail_threads(lead=lead, threads=threads, host_email=HOST)
    msg = lead.messages.get()
    assert msg.sent_at == datetime(2026, 4, 8, 16, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# merged_timeline + classify_ball_on_court
# ---------------------------------------------------------------------------


def _make_msg(lead, *, source, direction, sent_at, external_id="x", body=""):
    return Message.objects.create(
        lead=lead, source=source, direction=direction,
        external_id=external_id, sent_at=sent_at, body=body,
    )


def test_merged_timeline_orders_across_sources(lead):
    li_old = datetime(2026, 4, 1, tzinfo=timezone.utc)
    gmail_mid = datetime(2026, 4, 5, tzinfo=timezone.utc)
    li_new = datetime(2026, 4, 10, tzinfo=timezone.utc)
    _make_msg(lead, source=Message.Source.LINKEDIN, external_id="li1",
              direction=Message.Direction.OUTBOUND, sent_at=li_old)
    _make_msg(lead, source=Message.Source.GMAIL, external_id="g1",
              direction=Message.Direction.INBOUND, sent_at=gmail_mid)
    _make_msg(lead, source=Message.Source.LINKEDIN, external_id="li2",
              direction=Message.Direction.OUTBOUND, sent_at=li_new)

    timeline = gmail_threads.merged_timeline(lead)
    assert [m.external_id for m in timeline] == ["li1", "g1", "li2"]


def test_classify_email_reply_overrides_linkedin_silence(lead):
    """Lead's last LinkedIn DM was outbound (from us, going cold). They
    replied via email afterwards. Ball is now on us — Gmail merge fixes
    what a LinkedIn-only classifier would miss."""
    li_when = dj_tz.now() - timedelta(days=10)
    gmail_when = dj_tz.now() - timedelta(days=2)
    _make_msg(lead, source=Message.Source.LINKEDIN, external_id="li1",
              direction=Message.Direction.OUTBOUND, sent_at=li_when)
    _make_msg(lead, source=Message.Source.GMAIL, external_id="g1",
              direction=Message.Direction.INBOUND, sent_at=gmail_when,
              body="Saw your demo, can we book a follow-up?")

    klass, latest, _ = gmail_threads.classify_ball_on_court(lead)
    assert klass == "ball_on_us"
    assert latest.source == Message.Source.GMAIL


def test_classify_no_messages(lead):
    klass, latest, msgs = gmail_threads.classify_ball_on_court(lead)
    assert klass == "no_messages" and latest is None and msgs == []


def test_classify_no_reply_yet(lead):
    when = dj_tz.now() - timedelta(days=3)
    _make_msg(lead, source=Message.Source.LINKEDIN, external_id="li1",
              direction=Message.Direction.OUTBOUND, sent_at=when)
    klass, _, _ = gmail_threads.classify_ball_on_court(lead)
    assert klass == "no_reply_yet"


def test_classify_active_in_flight_when_recent_outbound(lead):
    """We replied 1 day ago; they haven't responded yet. Don't surface for
    re-nudge — they need time."""
    inbound_when = dj_tz.now() - timedelta(days=5)
    outbound_when = dj_tz.now() - timedelta(days=1)
    _make_msg(lead, source=Message.Source.GMAIL, external_id="g1",
              direction=Message.Direction.INBOUND, sent_at=inbound_when)
    _make_msg(lead, source=Message.Source.GMAIL, external_id="g2",
              direction=Message.Direction.OUTBOUND, sent_at=outbound_when)
    klass, _, _ = gmail_threads.classify_ball_on_court(lead, nudge_after_days=5)
    assert klass == "active_in_flight"


def test_classify_cold_thread_when_outbound_old(lead):
    inbound_when = dj_tz.now() - timedelta(days=20)
    outbound_when = dj_tz.now() - timedelta(days=10)
    _make_msg(lead, source=Message.Source.LINKEDIN, external_id="li1",
              direction=Message.Direction.INBOUND, sent_at=inbound_when)
    _make_msg(lead, source=Message.Source.LINKEDIN, external_id="li2",
              direction=Message.Direction.OUTBOUND, sent_at=outbound_when)
    klass, _, _ = gmail_threads.classify_ball_on_court(lead, nudge_after_days=5)
    assert klass == "cold_thread"


def test_merged_timeline_since_days_clamps(lead):
    far = dj_tz.now() - timedelta(days=400)
    near = dj_tz.now() - timedelta(days=10)
    _make_msg(lead, source=Message.Source.LINKEDIN, external_id="old",
              direction=Message.Direction.OUTBOUND, sent_at=far)
    _make_msg(lead, source=Message.Source.LINKEDIN, external_id="new",
              direction=Message.Direction.OUTBOUND, sent_at=near)

    assert {m.external_id for m in gmail_threads.merged_timeline(lead)} == {"old", "new"}
    assert {m.external_id for m in gmail_threads.merged_timeline(lead, since_days=90)} == {"new"}
