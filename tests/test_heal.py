# tests/test_heal.py
import json
import pytest
from datetime import timedelta
from django.utils import timezone

from crm.models import Deal, Lead, Message
from linkedin.daemon import (
    _LOW_POOL_ALERTED,
    _ensure_connect_task_for_campaign,
    _maybe_alert_low_connect_pool,
    heal_tasks,
)
from linkedin.db.deals import set_profile_state
from linkedin.db.leads import create_enriched_lead, promote_lead_to_deal
from linkedin.models import ActionLog, Campaign, Task
from linkedin.enums import ProfileState
from linkedin.operators import resolve_operator
from linkedin.tasks.follow_up_submission import (
    SUBMISSION_ATTEMPTED_AT_KEY,
    SUBMISSION_LEAD_ID_KEY,
    SUBMISSION_MESSAGE_PREFIX_KEY,
    SUBMISSION_OPERATOR_KEY,
)


SAMPLE_PROFILE = {
    "first_name": "Alice",
    "last_name": "Smith",
    "headline": "Engineer",
    "positions": [{"company_name": "Acme"}],
}


def _make_pending(session, public_id="alice"):
    url = f"https://www.linkedin.com/in/{public_id}/"
    create_enriched_lead(session, url, SAMPLE_PROFILE)
    promote_lead_to_deal(session, public_id)
    set_profile_state(session, public_id, ProfileState.PENDING.value)


def _make_connected(session, public_id="alice"):
    url = f"https://www.linkedin.com/in/{public_id}/"
    create_enriched_lead(session, url, SAMPLE_PROFILE)
    promote_lead_to_deal(session, public_id)
    set_profile_state(session, public_id, ProfileState.CONNECTED.value)


def _make_ready(session, public_id="alice"):
    url = f"https://www.linkedin.com/in/{public_id}/"
    create_enriched_lead(session, url, SAMPLE_PROFILE)
    promote_lead_to_deal(session, public_id)
    set_profile_state(session, public_id, ProfileState.READY_TO_CONNECT.value)


@pytest.mark.django_db
class TestHealTasks:
    @pytest.fixture(autouse=True)
    def _db(self, embeddings_db):
        pass

    def test_recovers_stale_running_tasks(self, fake_session):
        Task.objects.create(
            task_type=Task.TaskType.CONNECT,
            status=Task.Status.RUNNING,
            scheduled_at=timezone.now(),
            payload={"campaign_id": fake_session.campaign.pk},
        )
        heal_tasks(fake_session)
        assert Task.objects.filter(status=Task.Status.RUNNING).count() == 0
        assert Task.objects.filter(
            task_type=Task.TaskType.CONNECT,
            status=Task.Status.PENDING,
        ).exists()

    def test_does_not_recover_recent_owned_running_task(self, fake_session):
        task = Task.objects.create(
            task_type=Task.TaskType.SWEEP_CONNECTIONS,
            status=Task.Status.RUNNING,
            scheduled_at=timezone.now(),
            started_at=timezone.now() - timedelta(minutes=5),
            payload={
                "operator": resolve_operator(
                    fake_session.linkedin_profile.linkedin_username,
                ),
            },
        )

        heal_tasks(fake_session)

        task.refresh_from_db()
        assert task.status == Task.Status.RUNNING

    def test_recovers_only_stale_task_owned_by_this_sender(self, fake_session):
        operator = resolve_operator(fake_session.linkedin_profile.linkedin_username)
        owned = Task.objects.create(
            task_type=Task.TaskType.SWEEP_CONNECTIONS,
            status=Task.Status.RUNNING,
            scheduled_at=timezone.now(),
            started_at=timezone.now() - timedelta(minutes=31),
            payload={"operator": operator},
        )
        foreign = Task.objects.create(
            task_type=Task.TaskType.SWEEP_CONNECTIONS,
            status=Task.Status.RUNNING,
            scheduled_at=timezone.now(),
            started_at=timezone.now() - timedelta(hours=4),
            payload={"operator": "Chuka"},
        )

        heal_tasks(fake_session)

        owned.refresh_from_db()
        foreign.refresh_from_db()
        assert owned.status == Task.Status.PENDING
        assert owned.started_at is None
        assert foreign.status == Task.Status.RUNNING

    def test_routes_stale_drip_through_uncertainty_aware_recovery(
        self,
        fake_session,
        monkeypatch,
    ):
        operator = resolve_operator(fake_session.linkedin_profile.linkedin_username)
        task = Task.objects.create(
            task_type=Task.TaskType.DRIP_LINKEDIN,
            status=Task.Status.RUNNING,
            scheduled_at=timezone.now(),
            started_at=timezone.now() - timedelta(minutes=31),
            payload={"delivery_id": 999, "operator": operator},
        )
        recovered = []

        def recover(task_id):
            from drip.tasks.linkedin import StaleRecoveryResult

            recovered.append(task_id)
            Task.objects.filter(pk=task_id).update(status=Task.Status.COMPLETED)
            return StaleRecoveryResult.UNCLEAR

        monkeypatch.setattr(
            "drip.tasks.linkedin.recover_stale_linkedin_task",
            recover,
        )

        heal_tasks(fake_session)

        task.refresh_from_db()
        assert recovered == [task.pk]
        assert task.status == Task.Status.COMPLETED

    def test_stale_media_follow_up_after_submit_boundary_fails_closed(
        self,
        fake_session,
        monkeypatch,
    ):
        operator = resolve_operator(fake_session.linkedin_profile.linkedin_username)
        _make_connected(fake_session, "media-crash")
        lead = Lead.objects.get(public_identifier="media-crash")
        deal = Deal.objects.get(lead=lead, campaign=fake_session.campaign)
        Message.objects.create(
            lead=lead,
            source=Message.Source.LINKEDIN,
            external_id="media-crash-connection-note",
            direction=Message.Direction.OUTBOUND,
            sender=operator,
            body="Connection note before the uncertain media follow-up.",
            sent_at=timezone.now() - timedelta(days=1),
        )
        task = Task.objects.create(
            task_type=Task.TaskType.FOLLOW_UP,
            status=Task.Status.RUNNING,
            scheduled_at=timezone.now(),
            started_at=timezone.now() - timedelta(minutes=31),
            payload={
                "campaign_id": fake_session.campaign.pk,
                "public_id": "media-crash",
                "operator": operator,
                SUBMISSION_ATTEMPTED_AT_KEY: timezone.now().isoformat(),
                SUBMISSION_LEAD_ID_KEY: lead.pk,
                SUBMISSION_MESSAGE_PREFIX_KEY: (
                    f"daemon-send:{operator}:{deal.pk}:"
                    "linkedin_connect_followup:step-0:"
                ),
                SUBMISSION_OPERATOR_KEY: operator,
            },
        )

        heal_tasks(fake_session)

        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        assert "outcome is unclear" in task.error
        assert "automatic retry is blocked" in task.error
        assert not Task.objects.exclude(pk=task.pk).filter(
            task_type=Task.TaskType.FOLLOW_UP,
            status=Task.Status.PENDING,
            payload__public_id="media-crash",
        ).exists()

        from linkedin.tasks.follow_up import handle_follow_up

        second_campaign = Campaign.objects.create(
            name="Uncertain media sibling campaign",
            user=fake_session.django_user,
        )
        Deal.objects.create(
            lead=lead,
            campaign=second_campaign,
            state=ProfileState.CONNECTED,
            connected_at=timezone.now(),
        )
        sibling = Task.objects.create(
            task_type=Task.TaskType.FOLLOW_UP,
            status=Task.Status.RUNNING,
            scheduled_at=timezone.now(),
            started_at=timezone.now(),
            payload={
                "campaign_id": second_campaign.pk,
                "public_id": "media-crash",
                "operator": operator,
                "sequence_name": "linkedin_connect_followup",
                "step_index": 0,
            },
        )
        fake_session.campaign = second_campaign
        monkeypatch.setattr(
            "linkedin.actions.message.send_raw_message",
            lambda *args, **kwargs: pytest.fail(
                "same-operator sibling Task must not send after uncertainty"
            ),
        )
        from linkedin.exceptions import LinkedInMessageSubmissionUnclearError

        with pytest.raises(
            LinkedInMessageSubmissionUnclearError,
            match="Another current LinkedIn media submission is unresolved",
        ):
            handle_follow_up(sibling, fake_session, qualifiers=None)

    def test_unclear_media_submission_does_not_block_another_operator(
        self,
        fake_session,
    ):
        lead_operator = "Arian"
        daemon_operator = "Chuka"
        _make_connected(fake_session, "media-cross-operator")
        lead = Lead.objects.get(public_identifier="media-cross-operator")
        deal = Deal.objects.get(lead=lead, campaign=fake_session.campaign)
        for sender in (lead_operator, daemon_operator):
            Message.objects.create(
                lead=lead,
                source=Message.Source.LINKEDIN,
                external_id=f"media-cross-operator-note-{sender}",
                direction=Message.Direction.OUTBOUND,
                sender=sender,
                body=f"Connection note from {sender}.",
                sent_at=timezone.now() - timedelta(days=1),
            )
        Task.objects.create(
            task_type=Task.TaskType.FOLLOW_UP,
            status=Task.Status.FAILED,
            scheduled_at=timezone.now(),
            error="Unclear Arian media submission",
            payload={
                "campaign_id": fake_session.campaign.pk,
                "public_id": "media-cross-operator",
                "operator": lead_operator,
                SUBMISSION_ATTEMPTED_AT_KEY: timezone.now().isoformat(),
                SUBMISSION_LEAD_ID_KEY: lead.pk,
                SUBMISSION_MESSAGE_PREFIX_KEY: (
                    f"daemon-send:{lead_operator}:{deal.pk}:"
                    "linkedin_connect_followup:step-0:"
                ),
                SUBMISSION_OPERATOR_KEY: lead_operator,
            },
        )
        fake_session.linkedin_profile.linkedin_username = "eddy@tryfedrampgpt.com"
        fake_session.linkedin_profile.save(update_fields=["linkedin_username"])

        heal_tasks(fake_session)

        assert Task.objects.filter(
            task_type=Task.TaskType.FOLLOW_UP,
            status=Task.Status.PENDING,
            payload__public_id="media-cross-operator",
            payload__operator=daemon_operator,
        ).exists()

    def test_stale_media_follow_up_with_exact_message_bypasses_quota_for_dedup(
        self,
        fake_session,
        tmp_path,
        monkeypatch,
    ):
        from linkedin import icp_outbound
        from linkedin.tasks.follow_up import handle_follow_up

        operator = resolve_operator(fake_session.linkedin_profile.linkedin_username)
        _make_connected(fake_session, "media-persisted")
        lead = Lead.objects.get(public_identifier="media-persisted")
        lead.icp = "CSPs"
        lead.save(update_fields=["icp"])
        deal = Deal.objects.get(lead=lead, campaign=fake_session.campaign)
        prefix = (
            f"daemon-send:{operator}:{deal.pk}:"
            "linkedin_connect_followup:step-0:"
        )
        Message.objects.create(
            lead=lead,
            source=Message.Source.LINKEDIN,
            external_id=prefix + "1234567890",
            direction=Message.Direction.OUTBOUND,
            sender=operator,
            body="Sent before the worker stopped.",
            sent_at=timezone.now(),
            raw={"media": {"type": "gif", "reference": "demo.gif"}},
        )
        task = Task.objects.create(
            task_type=Task.TaskType.FOLLOW_UP,
            status=Task.Status.RUNNING,
            scheduled_at=timezone.now() - timedelta(minutes=31),
            started_at=timezone.now() - timedelta(minutes=31),
            payload={
                "campaign_id": fake_session.campaign.pk,
                "public_id": "media-persisted",
                "operator": operator,
                "sequence_name": "linkedin_connect_followup",
                "step_index": 0,
                SUBMISSION_ATTEMPTED_AT_KEY: timezone.now().isoformat(),
                SUBMISSION_LEAD_ID_KEY: lead.pk,
                SUBMISSION_MESSAGE_PREFIX_KEY: prefix,
                SUBMISSION_OPERATOR_KEY: operator,
            },
        )

        heal_tasks(fake_session)

        task.refresh_from_db()
        assert task.status == Task.Status.PENDING
        assert task.started_at is None
        assert task.error == ""
        assert task.payload[SUBMISSION_ATTEMPTED_AT_KEY]
        assert task.payload[SUBMISSION_LEAD_ID_KEY] == lead.pk
        assert task.payload[SUBMISSION_MESSAGE_PREFIX_KEY] == prefix
        assert task.payload[SUBMISSION_OPERATOR_KEY] == operator
        assert task.payload["sequence_name"] == "linkedin_connect_followup"
        assert task.payload["step_index"] == 0

        messages_path = tmp_path / "icp_messages.json"
        messages_path.write_text(json.dumps({
            operator: {
                "CSPs": {
                    "linkedin_connect_followup": [
                        {"delay_hours": 0, "variants": ["Step zero {first_name}"]},
                        {"delay_hours": 24, "variants": ["Step one {first_name}"]},
                    ],
                },
            },
        }))
        monkeypatch.setattr(icp_outbound, "_MESSAGES_PATH", messages_path)
        monkeypatch.setattr(
            "linkedin.actions.message.send_raw_message",
            lambda *args, **kwargs: pytest.fail(
                "recovered confirmed media step must not send again"
            ),
        )
        monkeypatch.setattr(
            type(fake_session.linkedin_profile),
            "can_execute",
            lambda *args, **kwargs: False,
        )

        task.status = Task.Status.RUNNING
        task.started_at = timezone.now()
        task.save(update_fields=["status", "started_at"])
        handle_follow_up(task, fake_session, qualifiers=None)

        deal.refresh_from_db()
        assert deal.state == ProfileState.CONNECTED
        successor = Task.objects.exclude(pk=task.pk).get(
            task_type=Task.TaskType.FOLLOW_UP,
            status=Task.Status.PENDING,
            payload__public_id="media-persisted",
        )
        assert successor.payload["step_index"] == 1

    def test_failed_confirmed_media_step_recovers_exact_custom_successor(
        self,
        fake_session,
        tmp_path,
        monkeypatch,
    ):
        from linkedin import icp_outbound
        from linkedin.tasks.follow_up import handle_follow_up

        operator = resolve_operator(fake_session.linkedin_profile.linkedin_username)
        _make_connected(fake_session, "media-failed-confirmed")
        lead = Lead.objects.get(public_identifier="media-failed-confirmed")
        lead.icp = "CSPs"
        lead.save(update_fields=["icp"])
        deal = Deal.objects.get(lead=lead, campaign=fake_session.campaign)
        sequence_name = "custom_media_sequence"
        prefix = f"daemon-send:{operator}:{deal.pk}:{sequence_name}:step-1:"
        Message.objects.create(
            lead=lead,
            source=Message.Source.LINKEDIN,
            external_id=prefix + "1234567890",
            direction=Message.Direction.OUTBOUND,
            sender=operator,
            body="Confirmed custom step before bookkeeping failed.",
            sent_at=timezone.now(),
            raw={"media": {"type": "video", "reference": "overview.mp4"}},
        )
        task = Task.objects.create(
            task_type=Task.TaskType.FOLLOW_UP,
            status=Task.Status.FAILED,
            scheduled_at=timezone.now(),
            started_at=timezone.now() - timedelta(minutes=1),
            error="ActionLog write failed after provider confirmation",
            payload={
                "campaign_id": fake_session.campaign.pk,
                "public_id": "media-failed-confirmed",
                "operator": operator,
                "icp": "CSPs",
                "sequence_name": sequence_name,
                "channel": "linkedin_connect_followup",
                "step_index": 1,
                SUBMISSION_ATTEMPTED_AT_KEY: timezone.now().isoformat(),
                SUBMISSION_LEAD_ID_KEY: lead.pk,
                SUBMISSION_MESSAGE_PREFIX_KEY: prefix,
                SUBMISSION_OPERATOR_KEY: operator,
            },
        )
        messages_path = tmp_path / "icp_messages.json"
        messages_path.write_text(json.dumps({
            operator: {
                "CSPs": {
                    "linkedin_connect_followup": [
                        {"delay_hours": 0, "variants": ["Step zero {first_name}"]},
                        {"delay_hours": 24, "variants": ["Step one {first_name}"]},
                        {"delay_hours": 48, "variants": ["Step two {first_name}"]},
                    ],
                },
            },
        }))
        monkeypatch.setattr(icp_outbound, "_MESSAGES_PATH", messages_path)
        monkeypatch.setattr(
            "linkedin.actions.message.send_raw_message",
            lambda *args, **kwargs: pytest.fail(
                "confirmed failed media step must not send again"
            ),
        )

        heal_tasks(fake_session)

        task.refresh_from_db()
        assert task.status == Task.Status.PENDING
        assert task.payload["sequence_name"] == sequence_name
        assert task.payload["step_index"] == 1
        task.status = Task.Status.RUNNING
        task.started_at = timezone.now()
        task.save(update_fields=["status", "started_at"])
        handle_follow_up(task, fake_session, qualifiers=None)

        deal.refresh_from_db()
        assert deal.state == ProfileState.CONNECTED
        successor = Task.objects.exclude(pk=task.pk).get(
            task_type=Task.TaskType.FOLLOW_UP,
            status=Task.Status.PENDING,
            payload__public_id="media-failed-confirmed",
        )
        assert successor.payload["sequence_name"] == sequence_name
        assert successor.payload["step_index"] == 2

    def test_seeds_connect_per_campaign(self, fake_session):
        _make_ready(fake_session, "alice")
        heal_tasks(fake_session)
        assert Task.objects.filter(
            task_type=Task.TaskType.CONNECT,
            status=Task.Status.PENDING,
            payload__campaign_id=fake_session.campaign.pk,
        ).count() == 1

    def test_does_not_seed_connect_for_finished_campaign(self, fake_session):
        fake_session.campaign.status = Campaign.Status.FINISHED
        fake_session.campaign.save(update_fields=["status"])
        _make_ready(fake_session, "alice")

        heal_tasks(fake_session)

        assert not Task.objects.filter(
            task_type=Task.TaskType.CONNECT,
            status=Task.Status.PENDING,
            payload__campaign_id=fake_session.campaign.pk,
        ).exists()

    def test_connect_recovery_creates_missing_task_when_work_remains(self, fake_session):
        _make_ready(fake_session, "alice")

        created = _ensure_connect_task_for_campaign(fake_session.campaign, delay_seconds=0)

        assert created is True
        assert Task.objects.filter(
            task_type=Task.TaskType.CONNECT,
            status=Task.Status.PENDING,
            payload__campaign_id=fake_session.campaign.pk,
        ).count() == 1

    def test_connect_recovery_does_not_create_without_work(self, fake_session):
        created = _ensure_connect_task_for_campaign(fake_session.campaign, delay_seconds=0)

        assert created is False
        assert not Task.objects.filter(
            task_type=Task.TaskType.CONNECT,
            status=Task.Status.PENDING,
            payload__campaign_id=fake_session.campaign.pk,
        ).exists()

    def test_connect_recovery_does_not_duplicate_running_task(self, fake_session):
        _make_ready(fake_session, "alice")
        Task.objects.create(
            task_type=Task.TaskType.CONNECT,
            status=Task.Status.RUNNING,
            scheduled_at=timezone.now(),
            started_at=timezone.now(),
            payload={"campaign_id": fake_session.campaign.pk},
        )

        created = _ensure_connect_task_for_campaign(fake_session.campaign, delay_seconds=0)

        assert created is False
        assert Task.objects.filter(
            task_type=Task.TaskType.CONNECT,
            payload__campaign_id=fake_session.campaign.pk,
        ).count() == 1

    def test_low_pool_alert_ignores_empty_connect_task(
        self,
        fake_session,
        monkeypatch,
    ):
        task = Task.objects.create(
            task_type=Task.TaskType.CONNECT,
            status=Task.Status.RUNNING,
            scheduled_at=timezone.now(),
            started_at=timezone.now(),
            payload={"campaign_id": fake_session.campaign.pk},
        )
        notifications = []
        monkeypatch.setattr(
            "linkedin.daemon.notify_degraded",
            lambda **kwargs: notifications.append(kwargs),
        )
        _LOW_POOL_ALERTED.clear()

        _maybe_alert_low_connect_pool(
            "Arian",
            fake_session.campaign,
            task=task,
            profile=fake_session.linkedin_profile,
        )

        assert fake_session.campaign.pk not in _LOW_POOL_ALERTED
        assert notifications == []
        _LOW_POOL_ALERTED.clear()

    def test_low_pool_alert_tracks_campaign_that_sent_connect(
        self,
        fake_session,
        monkeypatch,
    ):
        task = Task.objects.create(
            task_type=Task.TaskType.CONNECT,
            status=Task.Status.RUNNING,
            scheduled_at=timezone.now(),
            started_at=timezone.now() - timedelta(seconds=1),
            payload={"campaign_id": fake_session.campaign.pk},
        )
        ActionLog.objects.create(
            linkedin_profile=fake_session.linkedin_profile,
            campaign=fake_session.campaign,
            action_type=ActionLog.ActionType.CONNECT,
        )
        notifications = []
        monkeypatch.setattr(
            "linkedin.daemon.notify_degraded",
            lambda **kwargs: notifications.append(kwargs),
        )
        _LOW_POOL_ALERTED.clear()

        _maybe_alert_low_connect_pool(
            "Arian",
            fake_session.campaign,
            task=task,
            profile=fake_session.linkedin_profile,
        )

        assert fake_session.campaign.pk in _LOW_POOL_ALERTED
        assert notifications[0]["title"] == "Arian's connect pool is low"
        _LOW_POOL_ALERTED.clear()

    def test_seeds_sweep_connections_when_pending_profiles_exist(self, fake_session):
        _make_pending(fake_session, "alice")
        heal_tasks(fake_session)
        assert Task.objects.filter(
            task_type=Task.TaskType.SWEEP_CONNECTIONS,
            status=Task.Status.PENDING,
            payload__operator=resolve_operator(fake_session.linkedin_profile.linkedin_username),
        ).exists()

    def test_seeds_status_summary_task(self, fake_session):
        heal_tasks(fake_session)
        assert Task.objects.filter(
            task_type=Task.TaskType.STATUS_SUMMARY,
            status=Task.Status.PENDING,
        ).exists()

    def test_retires_legacy_check_pending_tasks(self, fake_session):
        Task.objects.create(
            task_type=Task.TaskType.CHECK_PENDING,
            status=Task.Status.PENDING,
            scheduled_at=timezone.now(),
            payload={"campaign_id": fake_session.campaign.pk, "public_id": "alice", "backoff_hours": 24},
        )
        heal_tasks(fake_session)
        assert not Task.objects.filter(
            task_type=Task.TaskType.CHECK_PENDING,
            status=Task.Status.PENDING,
        ).exists()

    def test_creates_follow_up_for_connected_profiles(self, fake_session):
        _make_connected(fake_session, "alice")
        heal_tasks(fake_session)
        assert Task.objects.filter(
            task_type=Task.TaskType.FOLLOW_UP,
            status=Task.Status.PENDING,
            payload__public_id="alice",
        ).exists()

    def test_does_not_heal_follow_up_for_stopped_lead(self, fake_session):
        from crm.models import Lead, Message

        fake_session.linkedin_profile.linkedin_username = "ariant@tryfedrampgpt.com"
        _make_connected(fake_session, "alice")
        lead = Lead.objects.get(linkedin_url="https://www.linkedin.com/in/alice/")
        Message.objects.create(
            lead=lead,
            source=Message.Source.LINKEDIN,
            external_id="heal-stop-reply",
            direction=Message.Direction.INBOUND,
            sender="Alice Smith",
            body="Thanks, let's talk",
            sent_at=timezone.now(),
        )

        heal_tasks(fake_session)

        assert not Task.objects.filter(
            task_type=Task.TaskType.FOLLOW_UP,
            status=Task.Status.PENDING,
            payload__public_id="alice",
        ).exists()

    def test_does_not_seed_follow_up_for_finished_campaign(self, fake_session):
        fake_session.campaign.status = Campaign.Status.FINISHED
        fake_session.campaign.save(update_fields=["status"])
        _make_connected(fake_session, "alice")

        heal_tasks(fake_session)

        assert not Task.objects.filter(
            task_type=Task.TaskType.FOLLOW_UP,
            status=Task.Status.PENDING,
            payload__public_id="alice",
        ).exists()

    def test_no_duplicates_on_second_heal(self, fake_session):
        _make_pending(fake_session, "alice")
        _make_connected(fake_session, "bob")
        heal_tasks(fake_session)
        count_before = Task.objects.filter(status=Task.Status.PENDING).count()
        heal_tasks(fake_session)
        count_after = Task.objects.filter(status=Task.Status.PENDING).count()
        assert count_before == count_after
