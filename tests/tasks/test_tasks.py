# tests/tasks/test_tasks.py
import pytest
from datetime import timedelta
from unittest.mock import patch, MagicMock

from django.utils import timezone

from linkedin.db.deals import set_profile_state
from linkedin.db.leads import create_enriched_lead, promote_lead_to_deal
from linkedin.models import ActionLog, Task
from linkedin.ml.qualifier import BayesianQualifier
from linkedin.enums import ProfileState
from linkedin.exceptions import SkipProfile, ReachedConnectionLimit
from linkedin.tasks.connect import ConnectStrategy, handle_connect, recommended_action_delay
from linkedin.tasks.follow_up import handle_follow_up
from linkedin.tasks.sweep_connections import handle_sweep_connections


SAMPLE_PROFILE = {
    "first_name": "Alice",
    "last_name": "Smith",
    "headline": "Engineer",
    "positions": [{"company_name": "Acme"}],
}


def _mock_strategy(candidate, qualifier=None):
    """Build a ConnectStrategy that returns a fixed candidate."""
    return ConnectStrategy(
        find_candidate=lambda s: candidate,
        pre_connect=None,
        delay=10,
        action_fraction=1.0,
        qualifier=qualifier or MagicMock(explain=lambda *a, **kw: ""),
    )


def _assert_deal_state(session, public_id, expected_state: ProfileState):
    from crm.models import Deal
    deal = Deal.objects.get(
        lead__linkedin_url=f"https://www.linkedin.com/in/{public_id}/",
        campaign=session.campaign,
    )
    assert deal.state == expected_state


def _make_qualified(session, public_id="alice"):
    url = f"https://www.linkedin.com/in/{public_id}/"
    create_enriched_lead(session, url, SAMPLE_PROFILE)
    promote_lead_to_deal(session, public_id)


def _make_pending(session, public_id="alice"):
    _make_qualified(session, public_id)
    set_profile_state(session, public_id, ProfileState.PENDING.value)


def _make_connected(session, public_id="alice"):
    _make_qualified(session, public_id)
    set_profile_state(session, public_id, ProfileState.CONNECTED.value)


def _make_old_deal(session, days):
    from crm.models import Deal
    deal = Deal.objects.filter(campaign=session.campaign).first()
    Deal.objects.filter(pk=deal.pk).update(
        update_date=timezone.now() - timedelta(days=days)
    )


def _make_task(task_type, payload, **kwargs):
    """Create a task and mark it RUNNING (matching daemon behavior)."""
    payload = dict(payload)
    if task_type == Task.TaskType.FOLLOW_UP and not payload.get("operator"):
        payload["operator"] = "Arian"
    return Task.objects.create(
        task_type=task_type,
        status=Task.Status.RUNNING,
        scheduled_at=kwargs.pop("scheduled_at", timezone.now()),
        started_at=timezone.now(),
        payload=payload,
        **kwargs,
    )


def _build_context(fake_session):
    """Build qualifiers dict for task handlers."""
    qualifier = BayesianQualifier(seed=42)
    qualifier.rank_profiles = lambda profiles, **kw: profiles
    return {fake_session.campaign.pk: qualifier}


# ── handle_connect tests ────────────────────────────────────────


@pytest.mark.django_db
@patch("linkedin.tasks.connect.ENABLE_CONNECT", True)
class TestHandleConnect:
    # Tests assume the connect lane is ON. .env may set ENABLE_CONNECT=false
    # during operator's follow-up-only test windows; the class-level patch
    # forces the gate open for these unit tests regardless of env.
    @pytest.fixture(autouse=True)
    def _db(self, embeddings_db):
        pass

    def _candidate(self):
        return {"public_identifier": "alice", "url": "https://www.linkedin.com/in/alice/", "profile": SAMPLE_PROFILE}

    @patch("linkedin.tasks.connect.strategy_for")
    @patch("linkedin.actions.connect.send_connection_request")
    @patch("linkedin.actions.status.get_connection_status")
    def test_sends_connection_and_records(self, mock_status, mock_send, mock_strategy, fake_session):
        _make_qualified(fake_session)
        mock_strategy.return_value = _mock_strategy(self._candidate())
        mock_status.return_value = ProfileState.QUALIFIED
        mock_send.return_value = ProfileState.PENDING

        task = _make_task(Task.TaskType.CONNECT, {"campaign_id": fake_session.campaign.pk})
        qualifiers = _build_context(fake_session)
        handle_connect(task, fake_session, qualifiers)

        _assert_deal_state(fake_session, "alice", ProfileState.PENDING)
        assert ActionLog.objects.filter(action_type=ActionLog.ActionType.CONNECT).count() == 1

    @patch("linkedin.tasks.connect.strategy_for")
    @patch("linkedin.actions.connect.send_connection_request")
    @patch("linkedin.actions.status.get_connection_status")
    def test_enqueues_sweep_after_connect(self, mock_status, mock_send, mock_strategy, fake_session):
        _make_qualified(fake_session)
        mock_strategy.return_value = _mock_strategy(self._candidate())
        mock_status.return_value = ProfileState.QUALIFIED
        mock_send.return_value = ProfileState.PENDING

        task = _make_task(Task.TaskType.CONNECT, {"campaign_id": fake_session.campaign.pk})
        qualifiers = _build_context(fake_session)
        handle_connect(task, fake_session, qualifiers)

        assert Task.objects.filter(
            task_type=Task.TaskType.SWEEP_CONNECTIONS,
            status=Task.Status.PENDING,
        ).exists()

    @patch("linkedin.tasks.connect.strategy_for")
    @patch("linkedin.actions.status.get_connection_status")
    def test_marks_preexisting_connected(self, mock_status, mock_strategy, fake_session):
        _make_qualified(fake_session)
        mock_strategy.return_value = _mock_strategy(self._candidate())
        mock_status.return_value = ProfileState.CONNECTED

        task = _make_task(Task.TaskType.CONNECT, {"campaign_id": fake_session.campaign.pk})
        qualifiers = _build_context(fake_session)
        handle_connect(task, fake_session, qualifiers)

        _assert_deal_state(fake_session, "alice", ProfileState.CONNECTED)
        # Should enqueue follow_up for already-connected profile
        assert Task.objects.filter(
            task_type=Task.TaskType.FOLLOW_UP,
            status=Task.Status.PENDING,
            payload__public_id="alice",
        ).exists()
        next_connect = Task.objects.filter(
            task_type=Task.TaskType.CONNECT,
            status=Task.Status.PENDING,
            payload__campaign_id=fake_session.campaign.pk,
        ).exclude(pk=task.pk).first()
        assert next_connect is not None
        assert next_connect.scheduled_at <= timezone.now() + timedelta(seconds=1)

    @patch("linkedin.tasks.connect.random.uniform", side_effect=lambda a, b: a)
    @patch("linkedin.tasks.connect._actions_sent_today", return_value=10)
    @patch("linkedin.tasks.connect._active_window_progress_seconds", return_value=(7200.0, 28800.0))
    @patch("linkedin.tasks.connect.CONNECT_DAILY_LIMIT", 50)
    def test_recommended_action_delay_adjusts_to_remaining_window(
        self, _window, _sent, _mock_uniform, fake_session,
    ):
        fake_session.linkedin_profile.connect_daily_limit = 50
        delay = recommended_action_delay(
            fake_session.linkedin_profile, ActionLog.ActionType.CONNECT,
        )

        # 2h remaining / 40 remaining sends = 180s, jitter lower bound 126s.
        # min_action_interval floor is 120, so we should land at 126.
        assert delay == 126

    @patch("linkedin.tasks.connect.random.uniform", side_effect=lambda a, b: a)
    @patch("linkedin.tasks.connect._actions_sent_today", return_value=0)
    @patch("linkedin.tasks.connect._active_window_progress_seconds", return_value=(28500.0, 28800.0))
    @patch("linkedin.tasks.connect.CONNECT_DAILY_LIMIT", 50)
    def test_recommended_action_delay_respects_full_window_floor(
        self, _window, _sent, _mock_uniform, fake_session,
    ):
        fake_session.linkedin_profile.connect_daily_limit = 50
        delay = recommended_action_delay(
            fake_session.linkedin_profile, ActionLog.ActionType.CONNECT,
        )

        # Dynamic average would be 570s, but the full-window average floor is
        # 576s, so the lower jitter bound becomes 403.2s.
        assert delay == pytest.approx(403.2)

    @patch("linkedin.tasks.connect.strategy_for")
    @patch("linkedin.actions.status.get_connection_status")
    def test_handles_rate_limit(self, mock_status, mock_strategy, fake_session):
        _make_qualified(fake_session)
        mock_strategy.return_value = _mock_strategy(self._candidate())
        mock_status.side_effect = ReachedConnectionLimit("weekly limit")

        task = _make_task(Task.TaskType.CONNECT, {"campaign_id": fake_session.campaign.pk})
        qualifiers = _build_context(fake_session)
        handle_connect(task, fake_session, qualifiers)

        assert ActionLog.ActionType.CONNECT in fake_session.linkedin_profile._exhausted

    @patch("linkedin.tasks.connect.strategy_for")
    @patch("linkedin.actions.connect.send_connection_request")
    @patch("linkedin.actions.status.get_connection_status")
    def test_handles_skip_profile(self, mock_status, mock_send, mock_strategy, fake_session):
        _make_qualified(fake_session)
        mock_strategy.return_value = _mock_strategy(self._candidate())
        mock_status.return_value = ProfileState.QUALIFIED
        mock_send.side_effect = SkipProfile("bad profile")

        task = _make_task(Task.TaskType.CONNECT, {"campaign_id": fake_session.campaign.pk})
        qualifiers = _build_context(fake_session)
        handle_connect(task, fake_session, qualifiers)

        _assert_deal_state(fake_session, "alice", ProfileState.FAILED)

    @patch("linkedin.tasks.connect.strategy_for")
    def test_reschedules_when_no_candidate(self, mock_strategy, fake_session):
        mock_strategy.return_value = _mock_strategy(None)

        task = _make_task(Task.TaskType.CONNECT, {"campaign_id": fake_session.campaign.pk})
        qualifiers = _build_context(fake_session)
        handle_connect(task, fake_session, qualifiers)

        # Should enqueue another connect with longer delay
        next_task = Task.objects.filter(
            task_type=Task.TaskType.CONNECT,
            status=Task.Status.PENDING,
            payload__campaign_id=fake_session.campaign.pk,
        ).exclude(pk=task.pk).first()
        assert next_task is not None

    @patch("linkedin.tasks.connect.strategy_for")
    @patch("linkedin.actions.connect.send_connection_request")
    @patch("linkedin.actions.status.get_connection_status")
    def test_self_reschedules_connect(self, mock_status, mock_send, mock_strategy, fake_session):
        _make_qualified(fake_session)
        mock_strategy.return_value = _mock_strategy(self._candidate())
        mock_status.return_value = ProfileState.QUALIFIED
        mock_send.return_value = ProfileState.PENDING

        task = _make_task(Task.TaskType.CONNECT, {"campaign_id": fake_session.campaign.pk})
        qualifiers = _build_context(fake_session)
        handle_connect(task, fake_session, qualifiers)

        # Should have enqueued next connect task
        next_connect = Task.objects.filter(
            task_type=Task.TaskType.CONNECT,
            status=Task.Status.PENDING,
            payload__campaign_id=fake_session.campaign.pk,
        ).exclude(pk=task.pk).first()
        assert next_connect is not None


# ── handle_sweep_connections tests ──────────────────────────────────


@pytest.mark.django_db
class TestHandleSweepConnections:
    @pytest.fixture(autouse=True)
    def _db(self, embeddings_db):
        pass

    @patch("linkedin.tasks.sweep_connections.scrape_connections")
    def test_marks_accepted_connections_and_enqueues_follow_up(
        self, mock_scrape, fake_session,
    ):
        from linkedin.actions.connections import ConnectionEntry
        _make_pending(fake_session, "alice")
        _make_pending(fake_session, "bob")

        mock_scrape.return_value = [
            ConnectionEntry(public_id="alice", name="Alice Smith", connected_on=None),
        ]

        task = _make_task(Task.TaskType.SWEEP_CONNECTIONS, {})
        qualifiers = _build_context(fake_session)
        handle_sweep_connections(task, fake_session, qualifiers)

        _assert_deal_state(fake_session, "alice", ProfileState.CONNECTED)
        _assert_deal_state(fake_session, "bob", ProfileState.PENDING)

        assert Task.objects.filter(
            task_type=Task.TaskType.FOLLOW_UP,
            payload__public_id="alice",
        ).exists()
        assert not Task.objects.filter(
            task_type=Task.TaskType.FOLLOW_UP,
            payload__public_id="bob",
        ).exists()

    @patch("linkedin.tasks.sweep_connections.scrape_connections")
    def test_self_reschedules(self, mock_scrape, fake_session):
        mock_scrape.return_value = []

        task = _make_task(Task.TaskType.SWEEP_CONNECTIONS, {})
        qualifiers = _build_context(fake_session)
        handle_sweep_connections(task, fake_session, qualifiers)

        assert Task.objects.filter(
            task_type=Task.TaskType.SWEEP_CONNECTIONS,
            status=Task.Status.PENDING,
        ).exclude(pk=task.pk).exists()

    @patch("linkedin.tasks.sweep_connections.notify_sweep_summary")
    @patch("linkedin.tasks.sweep_connections.scrape_connections")
    def test_posts_connect_runs_and_qualified_counts(
        self, mock_scrape, mock_notify, fake_session,
    ):
        mock_scrape.return_value = []
        _make_qualified(fake_session, "alice")
        _make_qualified(fake_session, "bob")

        today = timezone.now()
        Task.objects.create(
            task_type=Task.TaskType.CONNECT,
            status=Task.Status.COMPLETED,
            scheduled_at=today,
            started_at=today,
            completed_at=today,
            payload={"campaign_id": fake_session.campaign.pk},
        )
        Task.objects.create(
            task_type=Task.TaskType.CONNECT,
            status=Task.Status.COMPLETED,
            scheduled_at=today,
            started_at=today,
            completed_at=today,
            payload={"campaign_id": fake_session.campaign.pk},
        )

        task = _make_task(Task.TaskType.SWEEP_CONNECTIONS, {})
        qualifiers = _build_context(fake_session)
        handle_sweep_connections(task, fake_session, qualifiers)

        mock_notify.assert_called_once()
        kwargs = mock_notify.call_args.kwargs
        assert kwargs["connect_runs_today"] == 2
        assert kwargs["qualified"] == 2


# ── handle_follow_up tests ─────────────────────────────────────
#
# Daemon's follow_up Task now sends the rigid ICP DM only — collapsed
# from the older POST_ACCEPT_VIDEO_LINK / LLM-agent split on 2026-05-12.
# Tests cover: send happy-path, skip-on-reply, missing-deal noop, rate-
# limit reschedule, kill-switch.


@pytest.mark.django_db
class TestHandleFollowUp:
    @patch("linkedin.actions.message.send_media_message", return_value=True)
    @patch("linkedin.actions.message.send_raw_message", return_value=True)
    @patch("linkedin.actions.conversations.get_conversation", return_value=None)
    def test_sends_icp_dm_when_no_reply(
        self, mock_conversation, mock_send, mock_send_media, fake_session,
        monkeypatch,
    ):
        from crm.models import Lead, Message
        # Pin {our_company_name} / {our_website_url} to a known test brand
        # so the assertion below doesn't drift with .env edits. `fill_message`
        # does a fresh `from linkedin.conf import OUR_COMPANY_NAME` inside its
        # body each call, so patching `linkedin.conf.*` is what reaches the
        # substitution; an `icp_outbound.*` patch wouldn't (no such attr).
        monkeypatch.setattr("linkedin.conf.OUR_COMPANY_NAME", "BrandCo")
        monkeypatch.setattr("linkedin.conf.OUR_WEBSITE_URL", "https://brand.co/")
        # icp_messages.json is keyed by operator (sender) at the top level
        # and has no shared default — give the daemon's account a username
        # that `resolve_operator` maps to a real sender block ("Arian"), so
        # `fill_for_lead` finds templates instead of raising SheetsError.
        fake_session.linkedin_profile.linkedin_username = "ariant@tryfedrampgpt.com"
        _make_connected(fake_session)
        # Seed an outbound so the "no-thread" guard doesn't short-circuit.
        # We're testing the happy path: connection note was sent, lead never
        # replied, now we follow up with the ICP DM.
        lead = Lead.objects.get(linkedin_url="https://www.linkedin.com/in/alice/")
        Message.objects.create(
            lead=lead, source=Message.Source.LINKEDIN, external_id="urn:li:msg:note",
            direction=Message.Direction.OUTBOUND,
            sender=fake_session.linkedin_profile.linkedin_username,
            body="(connection note)",
            sent_at=timezone.now() - timedelta(days=5),
        )

        task = _make_task(
            Task.TaskType.FOLLOW_UP,
            {"campaign_id": fake_session.campaign.pk, "public_id": "alice"},
        )
        qualifiers = _build_context(fake_session)
        handle_follow_up(task, fake_session, qualifiers)

        _assert_deal_state(fake_session, "alice", ProfileState.COMPLETED)
        assert ActionLog.objects.filter(action_type=ActionLog.ActionType.FOLLOW_UP).count() == 1
        # Template has `{add demo.gif}` so the send routes through
        # send_media_message when the file exists in assets/follow_up/.
        # In the test env that path resolves, so we expect the media send.
        # If demo.gif is missing, the placeholder is stripped and the
        # send falls back to send_raw_message — handle either.
        sent_message = (
            mock_send_media.call_args.args[2] if mock_send_media.called
            else mock_send.call_args.args[2]
        )
        assert "BrandCo" in sent_message  # {our_company_name} substituted
        assert "Alice" in sent_message

    @patch("linkedin.actions.message.send_raw_message")
    @patch("linkedin.actions.conversations.get_conversation")
    def test_skips_when_lead_already_replied(
        self, mock_conversation, mock_send, fake_session,
    ):
        _make_connected(fake_session)
        mock_conversation.return_value = [
            {"text": "Sounds interesting", "sender": "Alice Smith", "timestamp": "2026-03-30 10:05"},
        ]

        task = _make_task(
            Task.TaskType.FOLLOW_UP,
            {"campaign_id": fake_session.campaign.pk, "public_id": "alice"},
        )
        qualifiers = _build_context(fake_session)
        handle_follow_up(task, fake_session, qualifiers)

        # Reply detected → mark Completed without sending. The followup-
        # sheet workflow picks the thread up under REPLIED / Ball-on-us.
        _assert_deal_state(fake_session, "alice", ProfileState.COMPLETED)
        assert ActionLog.objects.filter(action_type=ActionLog.ActionType.FOLLOW_UP).count() == 0
        mock_send.assert_not_called()

    @patch("linkedin.actions.message.send_media_message", return_value=True)
    @patch("linkedin.actions.message.send_raw_message", return_value=True)
    @patch("linkedin.actions.conversations.get_conversation", return_value=None)
    def test_skips_when_same_operator_already_followed_up(
        self, mock_conversation, mock_send, mock_send_media, fake_session,
    ):
        """A Lead this operator already daemon-followed-up (e.g. from
        another campaign — leads can hold Deals in >1 campaign) must not
        be DM'd again by the same operator. `daemon-send:` external_ids
        are the dedup marker."""
        from crm.models import Lead, Message
        _make_connected(fake_session)
        lead = Lead.objects.get(linkedin_url="https://www.linkedin.com/in/alice/")
        # Prior daemon follow-up by THIS operator (another campaign).
        Message.objects.create(
            lead=lead, source=Message.Source.LINKEDIN,
            external_id=f"daemon-send:{lead.pk}:1778800000",
            direction=Message.Direction.OUTBOUND,
            sender=fake_session.linkedin_profile.linkedin_username,
            body="(prior follow-up DM)",
            sent_at=timezone.now() - timedelta(days=1),
        )

        task = _make_task(
            Task.TaskType.FOLLOW_UP,
            {"campaign_id": fake_session.campaign.pk, "public_id": "alice"},
        )
        qualifiers = _build_context(fake_session)
        handle_follow_up(task, fake_session, qualifiers)

        # Deduped: marked Completed, no second DM, no rate-limit action logged.
        _assert_deal_state(fake_session, "alice", ProfileState.COMPLETED)
        mock_send.assert_not_called()
        mock_send_media.assert_not_called()
        assert ActionLog.objects.filter(
            action_type=ActionLog.ActionType.FOLLOW_UP
        ).count() == 0

    @patch("linkedin.actions.message.send_media_message", return_value=True)
    @patch("linkedin.actions.message.send_raw_message", return_value=True)
    @patch("linkedin.actions.conversations.get_conversation", return_value=None)
    def test_sends_when_only_a_different_operator_followed_up(
        self, mock_conversation, mock_send, mock_send_media, fake_session,
    ):
        """Dedup is operator-scoped: a `daemon-send:` from a DIFFERENT
        operator is a separate account's separate outreach and must not
        block this operator's own first follow-up."""
        from crm.models import Lead, Message
        _make_connected(fake_session)
        # icp_messages.json is keyed by operator (sender); give the daemon's
        # account a username `resolve_operator` maps to a real sender block.
        fake_session.linkedin_profile.linkedin_username = "ariant@tryfedrampgpt.com"
        lead = Lead.objects.get(linkedin_url="https://www.linkedin.com/in/alice/")
        me = fake_session.linkedin_profile.linkedin_username
        # This operator owns the thread (a non-daemon-send outbound) so the
        # owner-scoping guard lets us through...
        Message.objects.create(
            lead=lead, source=Message.Source.LINKEDIN,
            external_id="urn:li:msg:note",
            direction=Message.Direction.OUTBOUND, sender=me,
            body="(connection note)", sent_at=timezone.now() - timedelta(days=5),
        )
        # ...but the only prior *daemon follow-up* was a different operator.
        Message.objects.create(
            lead=lead, source=Message.Source.LINKEDIN,
            external_id=f"daemon-send:{lead.pk}:1778800000",
            direction=Message.Direction.OUTBOUND,
            sender="other-operator@example.com",
            body="(other operator's follow-up)",
            sent_at=timezone.now() - timedelta(days=1),
        )

        task = _make_task(
            Task.TaskType.FOLLOW_UP,
            {"campaign_id": fake_session.campaign.pk, "public_id": "alice"},
        )
        qualifiers = _build_context(fake_session)
        handle_follow_up(task, fake_session, qualifiers)

        # Not deduped: this operator's own follow-up still goes out.
        _assert_deal_state(fake_session, "alice", ProfileState.COMPLETED)
        assert mock_send.called or mock_send_media.called

    @patch("linkedin.actions.message.send_media_message", return_value=False)
    @patch("linkedin.actions.message.send_raw_message", return_value=False)
    @patch("linkedin.actions.conversations.get_conversation", return_value=None)
    def test_reenqueues_in_24h_on_send_failure(
        self, mock_conversation, mock_send, mock_send_media, fake_session,
    ):
        from crm.models import Lead, Message
        _make_connected(fake_session)
        # icp_messages.json is keyed by operator (sender); give the daemon's
        # account a username `resolve_operator` maps to a real sender block.
        fake_session.linkedin_profile.linkedin_username = "ariant@tryfedrampgpt.com"
        # Same seed as the happy-path test — we want to reach the send.
        lead = Lead.objects.get(linkedin_url="https://www.linkedin.com/in/alice/")
        Message.objects.create(
            lead=lead, source=Message.Source.LINKEDIN, external_id="urn:li:msg:note2",
            direction=Message.Direction.OUTBOUND,
            sender=fake_session.linkedin_profile.linkedin_username,
            body="(connection note)",
            sent_at=timezone.now() - timedelta(days=5),
        )

        task = _make_task(
            Task.TaskType.FOLLOW_UP,
            {"campaign_id": fake_session.campaign.pk, "public_id": "alice"},
        )
        qualifiers = _build_context(fake_session)
        handle_follow_up(task, fake_session, qualifiers)

        # Send returned False → Deal stays CONNECTED, ActionLog not recorded,
        # next task is pending with delay.
        _assert_deal_state(fake_session, "alice", ProfileState.CONNECTED)
        assert ActionLog.objects.filter(action_type=ActionLog.ActionType.FOLLOW_UP).count() == 0
        next_task = Task.objects.filter(
            task_type=Task.TaskType.FOLLOW_UP,
            status=Task.Status.PENDING,
            payload__public_id="alice",
        ).exclude(pk=task.pk).first()
        assert next_task is not None

    @patch("linkedin.actions.message.send_raw_message")
    def test_noop_when_deal_missing(self, mock_send, fake_session):
        task = _make_task(
            Task.TaskType.FOLLOW_UP,
            {"campaign_id": fake_session.campaign.pk, "public_id": "nonexistent"},
        )
        qualifiers = _build_context(fake_session)
        handle_follow_up(task, fake_session, qualifiers)
        mock_send.assert_not_called()

    def test_reschedules_on_rate_limit(self, fake_session):
        _make_connected(fake_session)
        # Force can_execute=False directly. Setting follow_up_daily_limit=0
        # on the row doesn't help: the env-var override FOLLOW_UP_DAILY_LIMIT
        # in `linkedin.models._LIMIT_OVERRIDES` wins over the per-row value
        # when truthy, so 0 just gets replaced by the env default.
        with patch.object(
            type(fake_session.linkedin_profile),
            "can_execute",
            return_value=False,
        ):
            task = _make_task(
                Task.TaskType.FOLLOW_UP,
                {"campaign_id": fake_session.campaign.pk, "public_id": "alice"},
            )
            qualifiers = _build_context(fake_session)
            handle_follow_up(task, fake_session, qualifiers)

        # Should have re-enqueued with delay
        next_task = Task.objects.filter(
            task_type=Task.TaskType.FOLLOW_UP,
            status=Task.Status.PENDING,
            payload__public_id="alice",
        ).exclude(pk=task.pk).first()
        assert next_task is not None

    @patch("linkedin.actions.message.send_raw_message")
    @patch("linkedin.actions.conversations.get_conversation", return_value=None)
    def test_skips_when_lead_belongs_to_other_operator(
        self, mock_conversation, mock_send, fake_session,
    ):
        """Travis-like incident (2026-05-12): daemon logged in as Arian
        almost sent to a lead Chuka had connected with. The Deal exists
        on the shared campaign but the outbound thread sender is Chuka,
        not Arian. Owner-scoping guard must short-circuit the send."""
        from crm.models import Lead, Message

        _make_connected(fake_session)
        lead = Lead.objects.get(linkedin_url="https://www.linkedin.com/in/alice/")
        # Seed an outbound that's NOT from this daemon's logged-in account.
        # fake_session.linkedin_profile.linkedin_username defaults to "testuser"
        # — the resolver leaves unknown handles as-is, so we set the lead's
        # outbound sender to a known-other operator.
        Message.objects.create(
            lead=lead,
            source=Message.Source.LINKEDIN,
            external_id="urn:li:msg:travis-1",
            direction=Message.Direction.OUTBOUND,
            sender="chukwuka agu",  # resolves → "Chuka"
            body="hey",
            sent_at=timezone.now() - timedelta(days=10),
        )

        task = _make_task(
            Task.TaskType.FOLLOW_UP,
            {"campaign_id": fake_session.campaign.pk, "public_id": "alice"},
        )
        qualifiers = _build_context(fake_session)
        handle_follow_up(task, fake_session, qualifiers)

        mock_send.assert_not_called()
        # State stays CONNECTED — no Completed flip, no re-enqueue. The
        # right daemon will pick it up when it runs.
        _assert_deal_state(fake_session, "alice", ProfileState.CONNECTED)

    @patch("linkedin.actions.message.send_raw_message")
    @patch("linkedin.actions.conversations.get_conversation", return_value=None)
    def test_skips_when_no_outbound_thread_exists(
        self, mock_conversation, mock_send, fake_session,
    ):
        """Imported / CSV-loaded leads that never had a connection note
        persisted have zero outbound LinkedIn messages. Sending a "follow-up"
        cold to them creates a brand-new thread, which fails silently and
        loops every 24h forever. Mark Completed and move on; the operator
        re-seeds via import_connections if they want to start the thread."""
        _make_connected(fake_session)
        # No outbound LinkedIn Message rows seeded → has_outbound=False
        task = _make_task(
            Task.TaskType.FOLLOW_UP,
            {"campaign_id": fake_session.campaign.pk, "public_id": "alice"},
        )
        qualifiers = _build_context(fake_session)
        handle_follow_up(task, fake_session, qualifiers)

        mock_send.assert_not_called()
        # Marked Completed so the Task doesn't re-enqueue
        _assert_deal_state(fake_session, "alice", ProfileState.COMPLETED)
        assert ActionLog.objects.filter(action_type=ActionLog.ActionType.FOLLOW_UP).count() == 0

    @patch("linkedin.tasks.follow_up.ENABLE_FOLLOW_UP", False)
    @patch("linkedin.actions.message.send_raw_message")
    def test_kill_switch_noop(self, mock_send, fake_session):
        """ENABLE_FOLLOW_UP=False makes the handler a no-op — defense in
        depth alongside the daemon's startup cancellation pass."""
        _make_connected(fake_session)
        task = _make_task(
            Task.TaskType.FOLLOW_UP,
            {"campaign_id": fake_session.campaign.pk, "public_id": "alice"},
        )
        qualifiers = _build_context(fake_session)
        handle_follow_up(task, fake_session, qualifiers)

        mock_send.assert_not_called()
        # Deal state untouched — still CONNECTED, not Completed.
        _assert_deal_state(fake_session, "alice", ProfileState.CONNECTED)


def test_enrich_phone_task_type_exists():
    from linkedin.models import Task

    assert Task.TaskType.ENRICH_PHONE == "enrich_phone"
    assert "enrich_phone" in {choice[0] for choice in Task.TaskType.choices}
