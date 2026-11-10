"""Tests for the realtime listener's missed-reply catch-up."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from crm.models import Deal, Lead, Message
from linkedin.enums import ProfileState
from linkedin.management.commands.discover_inbox_leads import (
    InboxConversation,
    ParticipantInfo,
)
from linkedin.models import Campaign
from linkedin.realtime import catchup
from linkedin.realtime.reply_notice import NOTICE_KEY, reply_notice_sent

SELF_URN = "urn:li:fsd_profile:ARIAN"
LEAD_URN = "urn:li:fsd_profile:LEAD1"
CONV = f"urn:li:msg_conversation:({SELF_URN},2-catchup)"
SESSION = object()


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _raw_messages(*messages: tuple[str, str, str, datetime]) -> dict:
    """LinkedIn messengerMessages payload for (urn, sender, text, delivered)."""
    elements = []
    for urn, sender, text, delivered in messages:
        first, _, last = sender.partition(" ")
        elements.append({
            "entityUrn": urn,
            "body": {"text": text},
            "deliveredAt": _ms(delivered),
            "sender": {"participantType": {"member": {
                "firstName": {"text": first}, "lastName": {"text": last},
            }}},
        })
    return {"data": {"messengerMessagesBySyncToken": {"elements": elements}}}


def _conversation(urn: str = CONV, member_urn: str = LEAD_URN) -> InboxConversation:
    participant = ParticipantInfo(
        member_urn=member_urn, public_id="waylonkrush",
        profile_url="https://www.linkedin.com/in/waylonkrush/",
        first_name="Waylon", last_name="Krush", full_name="Waylon Krush", headline="",
    )
    return InboxConversation(conversation_urn=urn, participant=participant,
                             latest_at=timezone.now(), raw={})


def _minute(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)


@pytest.fixture
def lead(db):
    return Lead.objects.create(
        first_name="Waylon", last_name="Krush",
        linkedin_url="https://www.linkedin.com/in/waylonkrush/",
        description=f'{{"urn": "{LEAD_URN}"}}',
    )


def _run(*, conversations, threads, notify=None, **kwargs):
    """Run the catch-up against faked LinkedIn reads.

    `threads` maps conversation URN → raw messages payload (or an exception).
    """
    def fake_fetch(_api, urn):
        value = threads[urn]
        if isinstance(value, Exception):
            raise value
        return value

    notify = notify or MagicMock(return_value=True)
    with patch("linkedin.realtime.catchup.PlaywrightLinkedinAPI"), \
         patch("linkedin.realtime.catchup.get_self_urn", return_value=SELF_URN), \
         patch("linkedin.realtime.catchup.iter_inbox_conversations",
               return_value=iter(conversations)) as pager, \
         patch("linkedin.realtime.catchup.fetch_messages", side_effect=fake_fetch), \
         patch("linkedin.notifications.slack.notify_message_received", notify):
        result = catchup.catch_up_missed_replies(
            SESSION, operator="Arian", pause_seconds=(0, 0), **kwargs,
        )
    return result, notify, pager


def test_new_reply_in_window_is_saved_stamped_and_notified(lead):
    user = User.objects.create_user(username="arian")
    campaign = Campaign.objects.create(name="Catch-up", user=user)
    deal = Deal.objects.create(lead=lead, campaign=campaign, state=ProfileState.PENDING)
    since = timezone.now() - timedelta(hours=8)
    replied = since + timedelta(hours=1)

    result, notify, pager = _run(
        conversations=[_conversation()],
        threads={CONV: _raw_messages(("urn:li:msg:r1", "Waylon Krush", "Let's talk", replied))},
        since=since,
    )

    msg = Message.objects.get(external_id="urn:li:msg:r1")
    assert msg.direction == Message.Direction.INBOUND
    assert msg.thread_external_id == CONV
    assert reply_notice_sent(msg)
    notify.assert_called_once()
    assert notify.call_args.kwargs["operator"] == "Arian"
    assert notify.call_args.kwargs["thread_external_id"] == CONV
    deal.refresh_from_db()
    assert deal.state == ProfileState.CONNECTED
    assert deal.last_reply_at == _minute(replied)
    assert pager.call_args.kwargs["cutoff"] == since
    assert (result.saved, result.notified) == (1, 1)


def test_reply_saved_silently_by_another_path_still_gets_its_notice(lead):
    """The Akanksha case: the sweep saved the reply but never posted Slack."""
    since = timezone.now() - timedelta(hours=8)
    replied = _minute(since + timedelta(hours=1))
    Message.objects.create(
        lead=lead, source=Message.Source.LINKEDIN, external_id="urn:li:msg:r1",
        direction=Message.Direction.INBOUND, sender="waylon krush", body="Let's talk",
        sent_at=replied, thread_external_id=CONV,
    )

    result, notify, _ = _run(
        conversations=[_conversation()],
        threads={CONV: _raw_messages(("urn:li:msg:r1", "Waylon Krush", "Let's talk", replied))},
        since=since,
    )

    notify.assert_called_once()
    assert (result.saved, result.notified) == (0, 1)
    assert Message.objects.filter(external_id="urn:li:msg:r1").count() == 1


def test_rerun_does_not_post_the_same_reply_twice(lead):
    since = timezone.now() - timedelta(hours=8)
    threads = {CONV: _raw_messages(
        ("urn:li:msg:r1", "Waylon Krush", "Let's talk", since + timedelta(hours=1)),
    )}
    notify = MagicMock(return_value=True)

    _run(conversations=[_conversation()], threads=threads, notify=notify, since=since)
    result, _, _ = _run(conversations=[_conversation()], threads=threads, notify=notify, since=since)

    notify.assert_called_once()
    assert (result.notified, result.already_notified) == (0, 1)


def test_messages_before_the_window_are_left_alone(lead):
    since = timezone.now() - timedelta(hours=8)
    result, notify, _ = _run(
        conversations=[_conversation()],
        threads={CONV: _raw_messages(
            ("urn:li:msg:old", "Waylon Krush", "Old reply", since - timedelta(hours=2)),
        )},
        since=since,
    )
    assert not Message.objects.filter(external_id="urn:li:msg:old").exists()
    notify.assert_not_called()
    assert result.messages_in_window == 0


def test_our_own_messages_in_window_are_saved_but_not_notified(lead):
    since = timezone.now() - timedelta(hours=8)
    result, notify, _ = _run(
        conversations=[_conversation()],
        threads={CONV: _raw_messages(
            ("urn:li:msg:out", "Arian Taj", "Following up", since + timedelta(hours=1)),
        )},
        since=since,
    )
    assert Message.objects.get(external_id="urn:li:msg:out").direction == Message.Direction.OUTBOUND
    notify.assert_not_called()
    assert result.notified == 0


def test_conversation_with_no_matching_lead_is_skipped(db):
    since = timezone.now() - timedelta(hours=8)
    result, notify, _ = _run(
        conversations=[_conversation(member_urn="urn:li:fsd_profile:GHOST")],
        threads={CONV: _raw_messages(
            ("urn:li:msg:g1", "Waylon Krush", "Hi", since + timedelta(hours=1)),
        )},
        since=since,
    )
    assert Message.objects.count() == 0
    notify.assert_not_called()
    assert result.unmatched == 1


def test_dry_run_writes_nothing_and_counts_what_it_would_post(lead):
    since = timezone.now() - timedelta(hours=8)
    silently_saved = Message.objects.create(
        lead=lead, source=Message.Source.LINKEDIN, external_id="urn:li:msg:r1",
        direction=Message.Direction.INBOUND, sender="waylon krush", body="First",
        sent_at=_minute(since + timedelta(hours=1)), thread_external_id=CONV,
    )

    result, notify, _ = _run(
        conversations=[_conversation()],
        threads={CONV: _raw_messages(
            ("urn:li:msg:r1", "Waylon Krush", "First", since + timedelta(hours=1)),
            ("urn:li:msg:r2", "Waylon Krush", "Second", since + timedelta(hours=2)),
        )},
        since=since,
        apply=False,
    )

    notify.assert_not_called()
    assert not Message.objects.filter(external_id="urn:li:msg:r2").exists()
    silently_saved.refresh_from_db()
    assert NOTICE_KEY not in (silently_saved.raw or {})
    assert result.would_notify == 2
    assert [p.text for p in result.pending] == ["First", "Second"]


def test_named_threads_skip_the_inbox_pager(lead):
    Message.objects.create(
        lead=lead, source=Message.Source.LINKEDIN, external_id="seed",
        direction=Message.Direction.OUTBOUND, sender="arian taj", body="Hi",
        sent_at=timezone.now() - timedelta(days=60), thread_external_id=CONV,
    )
    since = timezone.now() - timedelta(days=90)

    result, notify, pager = _run(
        conversations=[],
        threads={CONV: _raw_messages(
            ("urn:li:msg:r1", "Waylon Krush", "Let's talk", since + timedelta(days=1)),
        )},
        since=since,
        conversation_urns=[CONV],
    )

    pager.assert_not_called()
    notify.assert_called_once()
    assert result.notified == 1


def test_one_failed_thread_does_not_stop_the_others(lead):
    other_lead = Lead.objects.create(
        first_name="Jane", last_name="Buyer",
        linkedin_url="https://www.linkedin.com/in/janebuyer/",
        description='{"urn": "urn:li:fsd_profile:JANE"}',
    )
    other_conv = f"urn:li:msg_conversation:({SELF_URN},2-other)"
    since = timezone.now() - timedelta(hours=8)

    result, notify, _ = _run(
        conversations=[_conversation(), _conversation(other_conv, "urn:li:fsd_profile:JANE")],
        threads={
            CONV: IOError("Messaging API 500"),
            other_conv: _raw_messages(("urn:li:msg:j1", "Jane Buyer", "Yes", since + timedelta(hours=1))),
        },
        since=since,
    )

    assert Message.objects.get(external_id="urn:li:msg:j1").lead == other_lead
    notify.assert_called_once()
    assert len(result.errors) == 1
    assert CONV in result.errors[0]


def test_stops_at_the_conversation_cap(lead):
    since = timezone.now() - timedelta(hours=8)
    convs = [_conversation(f"urn:li:msg_conversation:({SELF_URN},2-{i})") for i in range(3)]
    threads = {c.conversation_urn: _raw_messages() for c in convs}

    result, _, _ = _run(conversations=convs, threads=threads, since=since, max_conversations=2)

    assert result.conversations == 2
    assert result.truncated is True


# ── Window from the listener heartbeat ──


def test_no_heartbeat_means_no_known_window():
    with patch("linkedin.realtime.catchup.read_heartbeat", return_value=None):
        assert catchup.catchup_window("arian@x.com") is None


def test_window_starts_just_before_the_last_heartbeat():
    now = datetime(2026, 11, 10, 12, 0, tzinfo=dt_timezone.utc)
    last = now - timedelta(hours=8)
    with patch("linkedin.realtime.catchup.read_heartbeat", return_value=last):
        window = catchup.catchup_window("arian@x.com", now=now)
    assert window.since == last - timedelta(minutes=catchup.CATCHUP_OVERLAP_MINUTES)
    assert window.capped is False


def test_long_outage_is_capped():
    now = datetime(2026, 11, 10, 12, 0, tzinfo=dt_timezone.utc)
    with patch("linkedin.realtime.catchup.read_heartbeat", return_value=now - timedelta(days=9)):
        window = catchup.catchup_window("arian@x.com", now=now)
    assert window.since == now - timedelta(hours=catchup.CATCHUP_MAX_HOURS)
    assert window.capped is True


# ── Daemon-side scheduling ──


def _listener_catchup(**kwargs):
    return catchup.ListenerCatchup(session=SESSION, username="arian@x.com", operator="Arian", **kwargs)


def test_requested_window_runs_once():
    job = _listener_catchup()
    since = timezone.now() - timedelta(hours=8)
    with patch("linkedin.realtime.catchup.catch_up_missed_replies",
               return_value=catchup.CatchupResult(since=since)) as run:
        job.request(catchup.CatchupWindow(since=since, capped=False))
        job.run_pending()
        job.run_pending()
    run.assert_called_once_with(SESSION, operator="Arian", since=since)


def test_runs_are_throttled_and_skipped_windows_merge():
    clock = iter([0.0, 30.0, 700.0])
    job = _listener_catchup(clock=lambda: next(clock))
    first = timezone.now() - timedelta(hours=8)
    second = timezone.now() - timedelta(minutes=2)
    with patch("linkedin.realtime.catchup.catch_up_missed_replies",
               side_effect=lambda *_a, since, **_k: catchup.CatchupResult(since=since)) as run:
        job.request(catchup.CatchupWindow(since=second, capped=False))
        job.run_pending()                      # t=0: runs
        job.request(catchup.CatchupWindow(since=first, capped=False))
        job.run_pending()                      # t=30: throttled, kept
        job.run_pending()                      # t=700: runs the kept window
    assert [c.kwargs["since"] for c in run.call_args_list] == [second, first]


def test_unexpected_error_is_reported_not_raised():
    job = _listener_catchup()
    with patch("linkedin.realtime.catchup.catch_up_missed_replies",
               side_effect=RuntimeError("boom")), \
         patch("linkedin.realtime.catchup.notify_error") as notify_error:
        job.request(catchup.CatchupWindow(since=timezone.now(), capped=False))
        job.run_pending()  # must not raise
    notify_error.assert_called_once()
    assert notify_error.call_args.args[0] == "daemon:listener_catchup"


def test_failed_run_keeps_its_window_for_a_limited_number_of_retries():
    ticks = iter(float(t) for t in range(0, 6000, 700))
    job = _listener_catchup(clock=lambda: next(ticks))
    since = timezone.now() - timedelta(hours=8)
    with patch("linkedin.realtime.catchup.catch_up_missed_replies",
               side_effect=IOError("browser offline")) as run, \
         patch("linkedin.realtime.catchup.notify_error"):
        job.request(catchup.CatchupWindow(since=since, capped=False))
        for _ in range(catchup.CATCHUP_MAX_ATTEMPTS + 2):
            job.run_pending()
    assert run.call_count == catchup.CATCHUP_MAX_ATTEMPTS
    assert all(c.kwargs["since"] == since for c in run.call_args_list)


def test_incomplete_run_alerts_ops():
    job = _listener_catchup()
    since = timezone.now() - timedelta(days=3)
    result = catchup.CatchupResult(since=since, errors=["conv: IOError"], truncated=True)
    with patch("linkedin.realtime.catchup.catch_up_missed_replies", return_value=result), \
         patch("linkedin.realtime.catchup.notify_degraded") as degraded:
        job.request(catchup.CatchupWindow(since=since, capped=True))
        job.run_pending()
    degraded.assert_called_once()
    detail = degraded.call_args.kwargs["detail"]
    assert "catch_up_replies" in detail
    assert degraded.call_args.kwargs["sender"] == "Arian"


def test_clean_run_does_not_alert():
    job = _listener_catchup()
    since = timezone.now() - timedelta(hours=8)
    with patch("linkedin.realtime.catchup.catch_up_missed_replies",
               return_value=catchup.CatchupResult(since=since, notified=2)), \
         patch("linkedin.realtime.catchup.notify_degraded") as degraded:
        job.request(catchup.CatchupWindow(since=since, capped=False))
        job.run_pending()
    degraded.assert_not_called()
