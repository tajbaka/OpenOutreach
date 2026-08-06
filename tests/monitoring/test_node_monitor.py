"""Tests for peer-node liveness monitoring (linkedin/monitoring/node_monitor.py).

Covers the heartbeat read/write/clear helpers and `check_peers` — including
the atomic `down_alerted_at` claim that makes exactly one peer alert per
outage and re-alert only after the cooldown.
"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from crm.models import Deal, Lead
from linkedin.enums import ProfileState
from linkedin.models import ActionLog, Campaign, DaemonHeartbeat, LinkedInProfile, Task
from linkedin.monitoring import node_monitor as nm


@pytest.fixture
def patched_notify():
    """Patch the Slack post so tests assert on calls, not network."""
    with patch("linkedin.monitoring.node_monitor.notify_degraded") as m:
        yield m


class TestWriteHeartbeat:
    def test_creates_row(self, db):
        nm.write_heartbeat("Arian")
        row = DaemonHeartbeat.objects.get(sender="Arian")
        assert row.last_alive is not None
        assert row.down_alerted_at is None

    def test_updates_and_clears_down_marker(self, db):
        old = timezone.now() - timedelta(hours=2)
        DaemonHeartbeat.objects.create(
            sender="Arian", last_alive=old, down_alerted_at=old,
        )
        nm.write_heartbeat("Arian")
        row = DaemonHeartbeat.objects.get(sender="Arian")
        assert row.last_alive > old
        # A beating node is alive — any prior down-claim is voided.
        assert row.down_alerted_at is None


class TestClearHeartbeat:
    def test_nulls_last_alive(self, db):
        DaemonHeartbeat.objects.create(
            sender="Arian", last_alive=timezone.now(),
        )
        nm.clear_heartbeat("Arian")
        row = DaemonHeartbeat.objects.get(sender="Arian")
        assert row.last_alive is None
        assert row.down_alerted_at is None


class TestCheckPeers:
    def _peer(self, sender, age_minutes=None, down_alerted_at=None):
        """Create a peer row. age_minutes=None ⇒ last_alive NULL (stopped)."""
        last_alive = (
            None if age_minutes is None
            else timezone.now() - timedelta(minutes=age_minutes)
        )
        return DaemonHeartbeat.objects.create(
            sender=sender, last_alive=last_alive, down_alerted_at=down_alerted_at,
        )

    def test_alerts_on_stale_peer(self, db, patched_notify):
        self._peer("Chuka", age_minutes=120)
        nm.check_peers("Arian")
        patched_notify.assert_called_once()
        assert patched_notify.call_args.kwargs["sender"] == "Chuka"

    def test_no_alert_on_fresh_peer(self, db, patched_notify):
        self._peer("Chuka", age_minutes=1)
        nm.check_peers("Arian")
        patched_notify.assert_not_called()

    def test_ignores_self(self, db, patched_notify):
        # Our own row may be stale mid-write — never alert on self.
        self._peer("Arian", age_minutes=120)
        nm.check_peers("Arian")
        patched_notify.assert_not_called()

    def test_ignores_stopped_peer(self, db, patched_notify):
        # last_alive=NULL ⇒ intentionally stopped (clean exit).
        self._peer("Chuka", age_minutes=None)
        nm.check_peers("Arian")
        patched_notify.assert_not_called()

    def test_claim_dedupes_repeat_alert(self, db, patched_notify):
        self._peer("Chuka", age_minutes=120)
        nm.check_peers("Arian")
        nm.check_peers("Arian")  # still stale, but already claimed
        patched_notify.assert_called_once()
        assert DaemonHeartbeat.objects.get(sender="Chuka").down_alerted_at is not None

    def test_realerts_after_cooldown(self, db, patched_notify):
        peer = self._peer("Chuka", age_minutes=120)
        nm.check_peers("Arian")
        # Push the claim marker past DEGRADED_REALERT_HOURS.
        DaemonHeartbeat.objects.filter(pk=peer.pk).update(
            down_alerted_at=timezone.now() - timedelta(hours=24),
        )
        nm.check_peers("Arian")
        assert patched_notify.call_count == 2


class TestCheckExpectedSenderActivity:
    def _sender(self, username="chukyjack", linkedin_username="chukyjack@gmail.com"):
        user = User.objects.create_user(username=username)
        profile = LinkedInProfile.objects.create(
            user=user,
            linkedin_username=linkedin_username,
            linkedin_password="x",
        )
        campaign = Campaign.objects.create(name=f"{username} campaign", user=user)
        lead = Lead.objects.create(
            first_name="Test",
            last_name="Lead",
            linkedin_url=f"https://www.linkedin.com/in/{username}-lead/",
            public_identifier=f"{username}-lead",
        )
        Deal.objects.create(
            lead=lead,
            campaign=campaign,
            state=ProfileState.READY_TO_CONNECT,
        )
        return profile, campaign

    def test_alerts_when_expected_sender_has_fresh_heartbeat_but_no_activity(
        self, db, monkeypatch, patched_notify,
    ):
        _profile, campaign = self._sender()
        now = timezone.now()
        DaemonHeartbeat.objects.create(sender="Chuka", last_alive=now)
        Task.objects.create(
            task_type=Task.TaskType.CONNECT,
            status=Task.Status.PENDING,
            scheduled_at=now - timedelta(hours=2),
            payload={"campaign_id": campaign.pk},
        )
        monkeypatch.setattr(nm.conf, "EXPECTED_OUTBOUND_SENDERS", ("Chuka",))
        monkeypatch.setattr(nm.conf, "SENDER_ACTIVITY_GRACE_MINUTES", 0)
        monkeypatch.setattr(nm.conf, "SENDER_ACTIVITY_STALE_MINUTES", 30)
        monkeypatch.setattr(
            nm, "_activity_check_window", lambda _now: now - timedelta(hours=3),
        )

        nm.check_expected_sender_activity("Arian")

        patched_notify.assert_called_once()
        assert patched_notify.call_args.kwargs["sender"] == "Chuka"
        assert "outbound activity looks stuck" in patched_notify.call_args.kwargs["title"]
        assert DaemonHeartbeat.objects.get(sender="Chuka").activity_alerted_at is not None

    def test_no_alert_when_expected_sender_has_recent_activity(
        self, db, monkeypatch, patched_notify,
    ):
        profile, campaign = self._sender()
        now = timezone.now()
        DaemonHeartbeat.objects.create(sender="Chuka", last_alive=now)
        Task.objects.create(
            task_type=Task.TaskType.CONNECT,
            status=Task.Status.PENDING,
            scheduled_at=now - timedelta(minutes=5),
            payload={"campaign_id": campaign.pk},
        )
        ActionLog.objects.create(
            linkedin_profile=profile,
            campaign=campaign,
            action_type=ActionLog.ActionType.CONNECT,
        )
        monkeypatch.setattr(nm.conf, "EXPECTED_OUTBOUND_SENDERS", ("Chuka",))
        monkeypatch.setattr(nm.conf, "SENDER_ACTIVITY_GRACE_MINUTES", 0)
        monkeypatch.setattr(nm.conf, "SENDER_ACTIVITY_STALE_MINUTES", 30)
        monkeypatch.setattr(
            nm, "_activity_check_window", lambda _now: now - timedelta(hours=3),
        )

        nm.check_expected_sender_activity("Arian")

        patched_notify.assert_not_called()

    def test_rate_limited_sender_alerts_as_limited_not_stuck(
        self, db, monkeypatch, patched_notify,
    ):
        _profile, campaign = self._sender()
        now = timezone.now()
        DaemonHeartbeat.objects.create(sender="Chuka", last_alive=now)
        Task.objects.create(
            task_type=Task.TaskType.CONNECT,
            status=Task.Status.PENDING,
            scheduled_at=now - timedelta(hours=2),
            payload={"campaign_id": campaign.pk},
        )

        def fake_can_execute(self, action_type):
            return action_type != ActionLog.ActionType.CONNECT

        monkeypatch.setattr(LinkedInProfile, "can_execute", fake_can_execute)
        monkeypatch.setattr(nm.conf, "EXPECTED_OUTBOUND_SENDERS", ("Chuka",))
        monkeypatch.setattr(nm.conf, "SENDER_ACTIVITY_GRACE_MINUTES", 0)
        monkeypatch.setattr(nm.conf, "SENDER_ACTIVITY_STALE_MINUTES", 30)
        monkeypatch.setattr(
            nm, "_activity_check_window", lambda _now: now - timedelta(hours=3),
        )

        nm.check_expected_sender_activity("Arian")

        patched_notify.assert_called_once()
        kwargs = patched_notify.call_args.kwargs
        assert kwargs["sender"] == "Chuka"
        assert "hit a rate limit" in kwargs["title"]
        assert "not treated as a stuck outbound lane" in kwargs["detail"]
        assert "outbound activity looks stuck" not in kwargs["title"]

    def test_activity_alert_cooldown_survives_a_healthy_peer_view(
        self, db, monkeypatch, patched_notify,
    ):
        profile, campaign = self._sender()
        now = timezone.now()
        DaemonHeartbeat.objects.create(sender="Chuka", last_alive=now)
        Task.objects.create(
            task_type=Task.TaskType.CONNECT,
            status=Task.Status.PENDING,
            scheduled_at=now - timedelta(minutes=5),
            payload={"campaign_id": campaign.pk},
        )
        ActionLog.objects.create(
            linkedin_profile=profile,
            campaign=campaign,
            action_type=ActionLog.ActionType.CONNECT,
        )
        blocked = True

        def fake_can_execute(self, action_type):
            return not blocked or action_type != ActionLog.ActionType.CONNECT

        monkeypatch.setattr(LinkedInProfile, "can_execute", fake_can_execute)
        monkeypatch.setattr(nm.conf, "EXPECTED_OUTBOUND_SENDERS", ("Chuka",))
        monkeypatch.setattr(nm.conf, "SENDER_ACTIVITY_GRACE_MINUTES", 0)
        monkeypatch.setattr(
            nm, "_activity_check_window", lambda _now: now - timedelta(hours=3),
        )

        nm.check_expected_sender_activity("Arian")
        first_alerted_at = DaemonHeartbeat.objects.get(
            sender="Chuka",
        ).activity_alerted_at

        blocked = False
        nm.check_expected_sender_activity("Chuka")
        blocked = True
        nm.check_expected_sender_activity("Arian")

        assert patched_notify.call_count == 1
        assert first_alerted_at is not None
        assert (
            DaemonHeartbeat.objects.get(sender="Chuka").activity_alerted_at
            == first_alerted_at
        )

    def test_explicit_expected_sender_without_profile_alerts(
        self, db, monkeypatch, patched_notify,
    ):
        DaemonHeartbeat.objects.create(sender="Missing", last_alive=timezone.now())
        monkeypatch.setattr(nm.conf, "EXPECTED_OUTBOUND_SENDERS", ("Missing",))
        monkeypatch.setattr(nm.conf, "SENDER_ACTIVITY_GRACE_MINUTES", 0)
        monkeypatch.setattr(
            nm,
            "_activity_check_window",
            lambda _now: timezone.now() - timedelta(hours=3),
        )

        nm.check_expected_sender_activity("Arian")

        patched_notify.assert_called_once()
        assert patched_notify.call_args.kwargs["sender"] == "Missing"
        assert "not configured" in patched_notify.call_args.kwargs["title"]
