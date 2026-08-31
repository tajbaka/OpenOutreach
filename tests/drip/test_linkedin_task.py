from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import timedelta

import pytest
from django.utils import timezone

from crm.models import Deal, Lead, Message
from drip.manifest import validate_manifest
from drip.models import (
    DripDelivery,
    DripDeliveryAttempt,
    DripEnrollment,
    DripLane,
)
from drip.services.publication import publish_manifest
from drip.tasks.linkedin import (
    StaleRecoveryResult,
    handle_drip_linkedin,
    recover_stale_linkedin_delivery,
)
from linkedin.actions.message import (
    DirectMessageOutcome,
    DirectMessageResult,
    MessageSubmissionAborted,
)
from linkedin.enums import ProfileState
from linkedin.exceptions import LinkedInMediaMismatchError
from linkedin.models import ActionLog, Task
from tests.drip.helpers import linkedin_profile_description


pytestmark = pytest.mark.django_db


@dataclass
class _Execution:
    lead: Lead
    enrollment: DripEnrollment
    lane: DripLane
    delivery: DripDelivery
    task: Task
    deal: Deal


def _execution(
    valid_drip_payload,
    fake_session,
    *,
    theme_index: int = 0,
    step_index: int = 0,
    deal_state: str = ProfileState.CONNECTED,
    sequence_status: str = DripLane.CurrentSequenceStatus.NOT_APPLICABLE,
) -> _Execution:
    published = publish_manifest(validate_manifest(valid_drip_payload))
    now = timezone.now()
    fake_session.linkedin_profile.linkedin_username = "ariantajbakh@gmail.com"
    fake_session.linkedin_profile.save(update_fields=["linkedin_username"])
    lead = Lead.objects.create(
        first_name="Ada",
        last_name="Lovelace",
        company_name="Analytical Engines",
        linkedin_url="https://www.linkedin.com/in/ada-lovelace/",
        public_identifier="ada-lovelace",
        description=linkedin_profile_description("ada-lovelace"),
        email="ada@example.com",
        icp="CSPs",
    )
    deal = Deal.objects.create(
        lead=lead,
        campaign=fake_session.campaign,
        state=deal_state,
        invitation_sender="Arian",
        invitation_sent_at=now - timedelta(days=30),
        connected_at=now - timedelta(days=20),
    )
    enrollment = DripEnrollment.objects.create(
        campaign=published.campaign,
        campaign_version=published.version,
        lead=lead,
        frozen_icp="CSPs",
        status=DripEnrollment.Status.ACTIVE,
        activated_at=now - timedelta(days=10),
        enrolled_by="reviewer",
        plan_hash="a" * 64,
    )
    theme = published.version.manifest["audiences"]["CSPs"]["themes"][theme_index]
    lane = DripLane.objects.create(
        enrollment=enrollment,
        channel=DripLane.Channel.LINKEDIN,
        operator="Arian",
        provider_account="arian",
        sender_identity="arian",
        recipient_identity="https://www.linkedin.com/in/ada-lovelace/",
        linkedin_member_urn="urn:li:fsd_profile:ada-lovelace",
        status=DripLane.Status.ACTIVE,
        current_sequence_status=sequence_status,
        current_sequence_reviewed_at=now - timedelta(days=10),
        current_sequence_reviewed_by="reviewer",
        handed_off_at=now - timedelta(days=10),
        current_theme_index=theme_index,
        current_theme_key=theme["key"],
        theme_started_at=now - timedelta(days=5),
    )
    body = (
        theme["senders"]["Arian"]["linkedin"][step_index]["body"]
        .replace("{first_name}", lead.first_name)
        .replace("{company_name}", lead.company_name)
    )
    media = theme["senders"]["Arian"]["linkedin"][step_index].get("media") or {}
    delivery = DripDelivery.objects.create(
        lane=lane,
        theme_key=theme["key"],
        theme_index=theme_index,
        step_index=step_index,
        frozen_body=body,
        frozen_media_kind=media.get("type", ""),
        frozen_media_reference=media.get("file", ""),
        frozen_media_mime_type=media.get("mime_type", ""),
        frozen_media_size_bytes=media.get("size_bytes"),
        frozen_media_sha256=media.get("sha256", ""),
        scheduled_at=now - timedelta(minutes=5),
        status=DripDelivery.Status.QUEUED,
        provider_account="arian",
    )
    task = Task.objects.create(
        task_type=Task.TaskType.DRIP_LINKEDIN,
        status=Task.Status.RUNNING,
        scheduled_at=delivery.scheduled_at,
        started_at=now,
        payload={"delivery_id": delivery.pk, "operator": "Arian"},
    )
    delivery.current_task = task
    delivery.save(update_fields=["current_task", "updated_at"])
    return _Execution(lead, enrollment, lane, delivery, task, deal)


def test_success_commits_submit_boundary_then_persists_ledgers_without_altering_deal(
    valid_drip_payload,
    fake_session,
    monkeypatch,
):
    execution = _execution(valid_drip_payload, fake_session)

    def send_once(session, member_urn, body, *, recipient_label, on_submit_attempt):
        assert session is fake_session
        assert member_urn == "urn:li:fsd_profile:ada-lovelace"
        assert recipient_label == "https://www.linkedin.com/in/ada-lovelace/"
        assert body == execution.delivery.frozen_body
        on_submit_attempt()
        attempt = DripDeliveryAttempt.objects.get(delivery=execution.delivery)
        assert attempt.submission_attempted_at is not None
        assert attempt.outcome == DripDeliveryAttempt.Outcome.RESERVED
        return DirectMessageResult(DirectMessageOutcome.SENT)

    monkeypatch.setattr("drip.tasks.linkedin.send_direct_message_once", send_once)

    handle_drip_linkedin(execution.task, fake_session, qualifiers=None)

    execution.delivery.refresh_from_db()
    execution.lane.refresh_from_db()
    execution.deal.refresh_from_db()
    attempt = DripDeliveryAttempt.objects.get(delivery=execution.delivery)
    outbound = Message.objects.get(
        source=Message.Source.LINKEDIN,
        external_id=f"drip-linkedin:{execution.delivery.pk}",
    )
    assert execution.delivery.status == DripDelivery.Status.SENT
    assert execution.delivery.outbound_message == outbound
    assert attempt.outcome == DripDeliveryAttempt.Outcome.SENT
    assert attempt.finished_at is not None
    assert outbound.lead == execution.lead
    assert outbound.direction == Message.Direction.OUTBOUND
    assert outbound.sender == "Arian"
    assert outbound.raw["delivery_id"] == execution.delivery.pk
    assert execution.lane.status == DripLane.Status.ACTIVE
    assert execution.lane.current_theme_key == "visibility_gap"
    assert execution.deal.state == ProfileState.CONNECTED
    assert ActionLog.objects.filter(
        linkedin_profile=fake_session.linkedin_profile,
        campaign=fake_session.campaign,
        action_type=ActionLog.ActionType.FOLLOW_UP,
    ).count() == 1


@pytest.mark.parametrize(
    ("media_type", "filename", "media_bytes"),
    (
        ("gif", "demo.gif", b"GIF89a-drip-media"),
        (
            "video",
            "overview.mp4",
            b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2",
        ),
    ),
)
def test_media_success_uses_frozen_asset_and_persists_exact_evidence(
    valid_drip_payload,
    fake_session,
    monkeypatch,
    tmp_path,
    media_type,
    filename,
    media_bytes,
):
    asset_root = tmp_path / "assets" / "follow_up"
    asset_root.mkdir(parents=True)
    (asset_root / filename).write_bytes(media_bytes)
    monkeypatch.setattr("linkedin.message_media.ROOT_DIR", tmp_path)
    payload = deepcopy(valid_drip_payload)
    payload["audiences"]["CSPs"]["themes"][0]["senders"]["Arian"][
        "linkedin"
    ][0]["media"] = {"type": media_type, "file": filename}
    execution = _execution(payload, fake_session)

    def send_once(
        session,
        member_urn,
        body,
        *,
        recipient_label,
        on_submit_attempt,
        media,
    ):
        assert session is fake_session
        assert member_urn == execution.lane.linkedin_member_urn
        assert recipient_label == execution.lane.recipient_identity
        assert body == execution.delivery.frozen_body
        assert media.reference == filename
        assert media.kind.value == media_type
        assert media.sha256 == execution.delivery.frozen_media_sha256
        on_submit_attempt()
        return DirectMessageResult(DirectMessageOutcome.SENT)

    monkeypatch.setattr("drip.tasks.linkedin.send_direct_message_once", send_once)

    handle_drip_linkedin(execution.task, fake_session)

    execution.delivery.refresh_from_db()
    outbound = Message.objects.get(
        external_id=f"drip-linkedin:{execution.delivery.pk}",
    )
    assert execution.delivery.status == DripDelivery.Status.SENT
    assert outbound.raw["media"] == {
        "type": execution.delivery.frozen_media_kind,
        "reference": execution.delivery.frozen_media_reference,
        "mime_type": execution.delivery.frozen_media_mime_type,
        "size_bytes": execution.delivery.frozen_media_size_bytes,
        "sha256": execution.delivery.frozen_media_sha256,
    }


def test_missing_or_changed_frozen_media_pauses_without_browser_send(
    valid_drip_payload,
    fake_session,
    monkeypatch,
):
    payload = deepcopy(valid_drip_payload)
    payload["audiences"]["CSPs"]["themes"][0]["senders"]["Arian"][
        "linkedin"
    ][0]["media"] = {"type": "gif", "file": "demo.gif"}
    execution = _execution(payload, fake_session)
    monkeypatch.setattr(
        "linkedin.message_media.resolve_linkedin_media",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            LinkedInMediaMismatchError("SHA-256 mismatch"),
        ),
    )
    monkeypatch.setattr(
        "drip.tasks.linkedin.send_direct_message_once",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("invalid frozen media must not reach the browser"),
        ),
    )

    handle_drip_linkedin(execution.task, fake_session)

    execution.delivery.refresh_from_db()
    execution.lane.refresh_from_db()
    attempt = execution.delivery.attempts.get()
    assert execution.delivery.status == DripDelivery.Status.PLANNED
    assert execution.delivery.current_task_id is None
    assert execution.lane.status == DripLane.Status.PAUSED
    assert attempt.outcome == DripDeliveryAttempt.Outcome.NOT_SUBMITTED
    assert attempt.submission_attempted_at is None
    assert "SHA-256 mismatch" in attempt.diagnostic_detail
    assert not Message.objects.filter(external_id__startswith="drip-linkedin:").exists()


def test_frozen_media_db_drift_during_upload_aborts_before_click(
    valid_drip_payload,
    fake_session,
    monkeypatch,
):
    payload = deepcopy(valid_drip_payload)
    payload["audiences"]["CSPs"]["themes"][0]["senders"]["Arian"][
        "linkedin"
    ][0]["media"] = {"type": "gif", "file": "demo.gif"}
    execution = _execution(payload, fake_session)
    clicked = []

    def send_once(*args, on_submit_attempt, media, **kwargs):
        DripDelivery.objects.filter(pk=execution.delivery.pk).update(
            frozen_media_sha256="f" * 64,
        )
        try:
            on_submit_attempt()
        except MessageSubmissionAborted as exc:
            return DirectMessageResult(
                DirectMessageOutcome.PRE_SUBMIT_FAILED,
                str(exc),
            )
        clicked.append(True)
        return DirectMessageResult(DirectMessageOutcome.SENT)

    monkeypatch.setattr("drip.tasks.linkedin.send_direct_message_once", send_once)

    handle_drip_linkedin(execution.task, fake_session)

    execution.delivery.refresh_from_db()
    execution.lane.refresh_from_db()
    attempt = execution.delivery.attempts.get()
    assert clicked == []
    assert execution.delivery.status == DripDelivery.Status.PLANNED
    assert execution.lane.status == DripLane.Status.PAUSED
    assert attempt.outcome == DripDeliveryAttempt.Outcome.NOT_SUBMITTED
    assert "frozen LinkedIn media changed" in attempt.diagnostic_detail


def test_inbound_stop_during_upload_survives_pre_submit_failure_without_callback(
    valid_drip_payload,
    fake_session,
    monkeypatch,
):
    payload = deepcopy(valid_drip_payload)
    payload["audiences"]["CSPs"]["themes"][0]["senders"]["Arian"][
        "linkedin"
    ][0]["media"] = {"type": "gif", "file": "demo.gif"}
    execution = _execution(payload, fake_session)
    inbound_id = None

    def send_once(*args, on_submit_attempt, media, **kwargs):
        nonlocal inbound_id
        assert callable(on_submit_attempt)
        assert media.reference == execution.delivery.frozen_media_reference
        inbound = Message.objects.create(
            lead=execution.lead,
            source=Message.Source.LINKEDIN,
            external_id="linkedin-inbound-during-upload-failure",
            direction=Message.Direction.INBOUND,
            sender="Ada Lovelace",
            body="Thanks, I will take it from here",
            sent_at=timezone.now(),
        )
        inbound_id = inbound.pk
        from drip.services.stops import stop_for_inbound_message

        assert stop_for_inbound_message(inbound.pk) == 1
        execution.enrollment.refresh_from_db()
        execution.lane.refresh_from_db()
        execution.delivery.refresh_from_db()
        assert execution.enrollment.status == DripEnrollment.Status.STOPPED
        assert execution.lane.status == DripLane.Status.STOPPED
        # The inbound hook deliberately leaves an in-flight delivery alone.
        # Cleanup of a definitely pre-submit failure must preserve the global
        # stop instead of making this delivery retryable again.
        assert execution.delivery.status == DripDelivery.Status.SENDING
        return DirectMessageResult(
            DirectMessageOutcome.PRE_SUBMIT_FAILED,
            "attachment upload failed before callback",
        )

    monkeypatch.setattr("drip.tasks.linkedin.send_direct_message_once", send_once)

    handle_drip_linkedin(execution.task, fake_session)

    execution.enrollment.refresh_from_db()
    execution.lane.refresh_from_db()
    execution.delivery.refresh_from_db()
    attempt = execution.delivery.attempts.get()
    assert inbound_id is not None
    assert execution.enrollment.status == DripEnrollment.Status.STOPPED
    assert execution.enrollment.stop_trigger_message_id == inbound_id
    assert execution.lane.status == DripLane.Status.STOPPED
    assert execution.delivery.status == DripDelivery.Status.STOPPED
    assert execution.delivery.current_task_id is None
    assert attempt.outcome == DripDeliveryAttempt.Outcome.NOT_SUBMITTED
    assert attempt.submission_attempted_at is None
    assert "upload failed" in attempt.diagnostic_detail
    assert not Message.objects.filter(external_id__startswith="drip-linkedin:").exists()


def test_pre_submit_failure_is_retryable_without_enqueuing_an_alternate_route(
    valid_drip_payload,
    fake_session,
    monkeypatch,
):
    execution = _execution(valid_drip_payload, fake_session)
    monkeypatch.setattr(
        "drip.tasks.linkedin.send_direct_message_once",
        lambda *args, **kwargs: DirectMessageResult(
            DirectMessageOutcome.PRE_SUBMIT_FAILED,
            "composer did not open",
        ),
    )

    handle_drip_linkedin(execution.task, fake_session, qualifiers=None)

    execution.delivery.refresh_from_db()
    execution.lane.refresh_from_db()
    attempt = DripDeliveryAttempt.objects.get(delivery=execution.delivery)
    assert execution.delivery.status == DripDelivery.Status.PLANNED
    assert execution.delivery.current_task_id is None
    assert execution.lane.status == DripLane.Status.ACTIVE
    assert attempt.outcome == DripDeliveryAttempt.Outcome.NOT_SUBMITTED
    assert attempt.submission_attempted_at is None
    assert Task.objects.filter(task_type=Task.TaskType.DRIP_LINKEDIN).count() == 1
    assert not Message.objects.filter(external_id__startswith="drip-linkedin:").exists()
    assert not ActionLog.objects.filter(action_type=ActionLog.ActionType.FOLLOW_UP).exists()


def test_temporary_campaign_pause_holds_without_permanently_pausing_lane(
    valid_drip_payload,
    fake_session,
    monkeypatch,
):
    execution = _execution(valid_drip_payload, fake_session)
    execution.enrollment.campaign.status = "paused"
    execution.enrollment.campaign.save(update_fields=["status", "updated_at"])
    monkeypatch.setattr(
        "drip.tasks.linkedin.send_direct_message_once",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("paused campaign must hold before browser mutation"),
        ),
    )

    handle_drip_linkedin(execution.task, fake_session)

    execution.delivery.refresh_from_db()
    execution.lane.refresh_from_db()
    assert execution.delivery.status == DripDelivery.Status.PLANNED
    assert execution.delivery.current_task is None
    assert execution.lane.status == DripLane.Status.ACTIVE


def test_missing_active_traditional_campaign_holds_lane_for_later(
    valid_drip_payload,
    fake_session,
    monkeypatch,
):
    execution = _execution(valid_drip_payload, fake_session)
    fake_session.campaign.status = "finished"
    fake_session.campaign.save(update_fields=["status"])
    monkeypatch.setattr(
        "drip.tasks.linkedin.send_direct_message_once",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("missing active Campaign must hold before browser mutation"),
        ),
    )

    handle_drip_linkedin(execution.task, fake_session)

    execution.delivery.refresh_from_db()
    execution.lane.refresh_from_db()
    assert execution.delivery.status == DripDelivery.Status.PLANNED
    assert execution.delivery.current_task is None
    assert execution.lane.status == DripLane.Status.ACTIVE


def test_unexpected_linkedin_exception_before_click_releases_safely(
    valid_drip_payload,
    fake_session,
    monkeypatch,
):
    execution = _execution(valid_drip_payload, fake_session)
    monkeypatch.setattr(
        "drip.tasks.linkedin.send_direct_message_once",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("navigation drift")),
    )

    with pytest.raises(RuntimeError, match="navigation drift"):
        handle_drip_linkedin(execution.task, fake_session)

    execution.delivery.refresh_from_db()
    attempt = execution.delivery.attempts.get()
    assert execution.delivery.status == DripDelivery.Status.PLANNED
    assert execution.delivery.current_task is None
    assert attempt.outcome == DripDeliveryAttempt.Outcome.NOT_SUBMITTED


def test_unexpected_linkedin_exception_after_click_boundary_is_unclear(
    valid_drip_payload,
    fake_session,
    monkeypatch,
):
    execution = _execution(valid_drip_payload, fake_session)

    def crash_after_boundary(*args, on_submit_attempt, **kwargs):
        on_submit_attempt()
        raise RuntimeError("renderer crashed after click boundary")

    monkeypatch.setattr(
        "drip.tasks.linkedin.send_direct_message_once",
        crash_after_boundary,
    )

    with pytest.raises(RuntimeError, match="renderer crashed"):
        handle_drip_linkedin(execution.task, fake_session)

    execution.delivery.refresh_from_db()
    execution.lane.refresh_from_db()
    attempt = execution.delivery.attempts.get()
    assert execution.delivery.status == DripDelivery.Status.UNCLEAR
    assert execution.lane.status == DripLane.Status.PAUSED
    assert attempt.outcome == DripDeliveryAttempt.Outcome.UNCLEAR


def test_inbound_reply_arriving_while_typing_aborts_click_and_globally_stops_drip(
    valid_drip_payload,
    fake_session,
    monkeypatch,
):
    execution = _execution(valid_drip_payload, fake_session)
    now = timezone.now()
    gmail_lane = DripLane.objects.create(
        enrollment=execution.enrollment,
        channel=DripLane.Channel.GMAIL,
        operator="Arian",
        provider_account="arian_boundera",
        sender_identity="ariant@getboundera.com",
        recipient_identity=execution.lead.email,
        status=DripLane.Status.ACTIVE,
        current_sequence_status=DripLane.CurrentSequenceStatus.COMPLETED,
        handed_off_at=now - timedelta(days=2),
        current_theme_key="visibility_gap",
        theme_started_at=now - timedelta(days=2),
    )
    sibling = DripDelivery.objects.create(
        lane=gmail_lane,
        theme_key="visibility_gap",
        theme_index=0,
        step_index=0,
        frozen_subject="Hello",
        frozen_body="Email body",
        scheduled_at=now,
        status=DripDelivery.Status.PLANNED,
        provider_account="arian_boundera",
    )

    clicked = False

    def send_once(session, member_urn, body, *, recipient_label, on_submit_attempt):
        nonlocal clicked
        assert member_urn == execution.lane.linkedin_member_urn
        assert recipient_label == execution.lane.recipient_identity
        inbound = Message.objects.create(
            lead=execution.lead,
            source=Message.Source.LINKEDIN,
            external_id="linkedin-inbound-during-type",
            direction=Message.Direction.INBOUND,
            sender="Ada Lovelace",
            body="Thanks, tell me more",
            sent_at=timezone.now(),
        )
        # Model the listener's on-commit hook winning the race while this
        # delivery is SENDING.  The hook stops lane state but deliberately
        # leaves the in-flight delivery for the pre-click callback to resolve.
        from drip.services.stops import stop_for_inbound_message

        stop_for_inbound_message(inbound.pk)
        try:
            on_submit_attempt()
        except MessageSubmissionAborted as exc:
            return DirectMessageResult(DirectMessageOutcome.PRE_SUBMIT_FAILED, str(exc))
        clicked = True
        return DirectMessageResult(DirectMessageOutcome.SENT)

    monkeypatch.setattr("drip.tasks.linkedin.send_direct_message_once", send_once)

    handle_drip_linkedin(execution.task, fake_session, qualifiers=None)

    execution.enrollment.refresh_from_db()
    execution.lane.refresh_from_db()
    execution.delivery.refresh_from_db()
    gmail_lane.refresh_from_db()
    sibling.refresh_from_db()
    attempt = DripDeliveryAttempt.objects.get(delivery=execution.delivery)
    assert clicked is False
    assert execution.enrollment.status == DripEnrollment.Status.STOPPED
    assert execution.lane.status == DripLane.Status.STOPPED
    assert gmail_lane.status == DripLane.Status.STOPPED
    assert execution.delivery.status == DripDelivery.Status.STOPPED
    assert sibling.status == DripDelivery.Status.STOPPED
    assert attempt.outcome == DripDeliveryAttempt.Outcome.NOT_SUBMITTED
    assert attempt.submission_attempted_at is None
    assert not Message.objects.filter(external_id__startswith="drip-linkedin:").exists()


def test_post_click_unclear_pauses_lane_and_never_persists_success(
    valid_drip_payload,
    fake_session,
    monkeypatch,
):
    execution = _execution(valid_drip_payload, fake_session)

    def send_once(session, member_urn, body, *, recipient_label, on_submit_attempt):
        assert member_urn == execution.lane.linkedin_member_urn
        assert recipient_label == execution.lane.recipient_identity
        on_submit_attempt()
        return DirectMessageResult(
            DirectMessageOutcome.UNCLEAR,
            "click occurred but confirmation was absent",
        )

    monkeypatch.setattr("drip.tasks.linkedin.send_direct_message_once", send_once)

    handle_drip_linkedin(execution.task, fake_session, qualifiers=None)

    execution.delivery.refresh_from_db()
    execution.lane.refresh_from_db()
    attempt = DripDeliveryAttempt.objects.get(delivery=execution.delivery)
    assert execution.delivery.status == DripDelivery.Status.UNCLEAR
    assert execution.lane.status == DripLane.Status.PAUSED
    assert attempt.outcome == DripDeliveryAttempt.Outcome.UNCLEAR
    assert attempt.submission_attempted_at is not None
    assert not Message.objects.filter(external_id__startswith="drip-linkedin:").exists()
    assert not ActionLog.objects.filter(action_type=ActionLog.ActionType.FOLLOW_UP).exists()


def test_next_step_uses_previous_successful_sent_at_not_stale_scheduled_date(
    valid_drip_payload,
    fake_session,
    monkeypatch,
):
    execution = _execution(
        valid_drip_payload,
        fake_session,
        step_index=1,
    )
    predecessor = DripDelivery.objects.create(
        lane=execution.lane,
        theme_key="visibility_gap",
        theme_index=0,
        step_index=0,
        frozen_body="Prior message",
        scheduled_at=timezone.now() - timedelta(days=10),
        sent_at=timezone.now() - timedelta(hours=1),
        status=DripDelivery.Status.SENT,
        provider_account="arian",
    )
    called = False

    def should_not_send(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("delivery was not due from predecessor.sent_at")

    monkeypatch.setattr("drip.tasks.linkedin.send_direct_message_once", should_not_send)

    handle_drip_linkedin(execution.task, fake_session, qualifiers=None)

    execution.delivery.refresh_from_db()
    predecessor.refresh_from_db()
    assert called is False
    assert execution.delivery.status == DripDelivery.Status.PLANNED
    assert execution.delivery.current_task_id is None
    assert predecessor.status == DripDelivery.Status.SENT
    assert not DripDeliveryAttempt.objects.filter(delivery=execution.delivery).exists()


def test_final_linkedin_theme_normalizes_terminal_lane_progress(
    valid_drip_payload,
    fake_session,
    monkeypatch,
):
    execution = _execution(
        valid_drip_payload,
        fake_session,
        theme_index=1,
        step_index=0,
    )
    for step_index in (0, 1):
        DripDelivery.objects.create(
            lane=execution.lane,
            theme_key="visibility_gap",
            theme_index=0,
            step_index=step_index,
            frozen_body=f"Prior {step_index}",
            scheduled_at=timezone.now() - timedelta(days=5 - step_index),
            sent_at=timezone.now() - timedelta(days=4 - step_index),
            status=DripDelivery.Status.SENT,
            provider_account="arian",
        )

    def send_once(*args, on_submit_attempt, **kwargs):
        on_submit_attempt()
        return DirectMessageResult(DirectMessageOutcome.SENT)

    monkeypatch.setattr("drip.tasks.linkedin.send_direct_message_once", send_once)

    handle_drip_linkedin(execution.task, fake_session)

    execution.lane.refresh_from_db()
    assert execution.lane.status == DripLane.Status.COMPLETED
    assert execution.lane.current_theme_index == 2
    assert execution.lane.current_theme_key == ""
    assert execution.lane.theme_started_at is not None


def test_completed_deal_is_allowed_only_for_reviewed_completed_current_sequence(
    valid_drip_payload,
    fake_session,
    monkeypatch,
):
    execution = _execution(
        valid_drip_payload,
        fake_session,
        deal_state=ProfileState.COMPLETED,
        sequence_status=DripLane.CurrentSequenceStatus.COMPLETED,
    )

    def send_once(session, member_urn, body, *, recipient_label, on_submit_attempt):
        assert member_urn == execution.lane.linkedin_member_urn
        assert recipient_label == execution.lane.recipient_identity
        on_submit_attempt()
        return DirectMessageResult(DirectMessageOutcome.SENT)

    monkeypatch.setattr("drip.tasks.linkedin.send_direct_message_once", send_once)
    handle_drip_linkedin(execution.task, fake_session, qualifiers=None)
    execution.delivery.refresh_from_db()
    execution.deal.refresh_from_db()
    assert execution.delivery.status == DripDelivery.Status.SENT
    assert execution.deal.state == ProfileState.COMPLETED


def test_non_connected_deal_returns_lane_to_waiting_connection(
    valid_drip_payload,
    fake_session,
    monkeypatch,
):
    execution = _execution(
        valid_drip_payload,
        fake_session,
        deal_state=ProfileState.PENDING,
    )
    monkeypatch.setattr(
        "drip.tasks.linkedin.send_direct_message_once",
        lambda *args, **kwargs: pytest.fail("an unconnected lead must not be messaged"),
    )

    handle_drip_linkedin(execution.task, fake_session, qualifiers=None)

    execution.delivery.refresh_from_db()
    execution.lane.refresh_from_db()
    execution.deal.refresh_from_db()
    assert execution.delivery.status == DripDelivery.Status.PLANNED
    assert execution.delivery.current_task_id is None
    assert execution.lane.status == DripLane.Status.WAITING_CONNECTION
    assert execution.deal.state == ProfileState.PENDING
    assert not DripDeliveryAttempt.objects.filter(delivery=execution.delivery).exists()


def test_connected_campaign_row_without_exact_sender_proof_cannot_send(
    valid_drip_payload,
    fake_session,
    monkeypatch,
):
    execution = _execution(valid_drip_payload, fake_session)
    execution.deal.invitation_sent_at = None
    execution.deal.invitation_sender = ""
    execution.deal.save(
        update_fields={"invitation_sent_at", "invitation_sender", "update_date"},
    )
    monkeypatch.setattr(
        "drip.tasks.linkedin.send_direct_message_once",
        lambda *args, **kwargs: pytest.fail("unattributed connection must not send"),
    )

    handle_drip_linkedin(execution.task, fake_session)

    execution.delivery.refresh_from_db()
    execution.lane.refresh_from_db()
    assert execution.delivery.status == DripDelivery.Status.PLANNED
    assert execution.lane.status == DripLane.Status.WAITING_CONNECTION
    assert not execution.delivery.attempts.exists()


def test_reservation_blocks_when_lead_profile_urn_drifted(
    valid_drip_payload,
    fake_session,
    monkeypatch,
):
    execution = _execution(valid_drip_payload, fake_session)
    execution.lead.description = linkedin_profile_description(
        "someone-else",
        member_urn="urn:li:fsd_profile:SOMEONE_ELSE",
    )
    execution.lead.save(update_fields={"description", "update_date"})
    monkeypatch.setattr(
        "drip.tasks.linkedin.send_direct_message_once",
        lambda *args, **kwargs: pytest.fail("identity drift must stop before navigation"),
    )

    handle_drip_linkedin(execution.task, fake_session)

    execution.delivery.refresh_from_db()
    execution.lane.refresh_from_db()
    assert execution.delivery.status == DripDelivery.Status.PLANNED
    assert execution.lane.status == DripLane.Status.PAUSED
    assert not execution.delivery.attempts.exists()


def test_pre_click_guard_binds_send_to_identity_reserved_before_navigation(
    valid_drip_payload,
    fake_session,
    monkeypatch,
):
    execution = _execution(valid_drip_payload, fake_session)
    clicked = False

    def send_once(
        session,
        member_urn,
        body,
        *,
        recipient_label,
        on_submit_attempt,
    ):
        nonlocal clicked
        assert member_urn == "urn:li:fsd_profile:ada-lovelace"
        assert recipient_label == "https://www.linkedin.com/in/ada-lovelace/"
        execution.lead.public_identifier = "new-vanity"
        execution.lead.linkedin_url = "https://www.linkedin.com/in/new-vanity/"
        execution.lead.description = linkedin_profile_description(
            "new-vanity",
            member_urn="urn:li:fsd_profile:NEW_MEMBER",
        )
        execution.lead.save(
            update_fields={
                "public_identifier",
                "linkedin_url",
                "description",
                "update_date",
            },
        )
        execution.lane.recipient_identity = execution.lead.linkedin_url
        execution.lane.linkedin_member_urn = "urn:li:fsd_profile:NEW_MEMBER"
        execution.lane.save(
            update_fields={"recipient_identity", "linkedin_member_urn", "updated_at"},
        )
        try:
            on_submit_attempt()
        except MessageSubmissionAborted as exc:
            return DirectMessageResult(DirectMessageOutcome.PRE_SUBMIT_FAILED, str(exc))
        clicked = True
        return DirectMessageResult(DirectMessageOutcome.SENT)

    monkeypatch.setattr("drip.tasks.linkedin.send_direct_message_once", send_once)

    handle_drip_linkedin(execution.task, fake_session)

    execution.delivery.refresh_from_db()
    execution.lane.refresh_from_db()
    attempt = execution.delivery.attempts.get()
    assert clicked is False
    assert attempt.outcome == DripDeliveryAttempt.Outcome.NOT_SUBMITTED
    assert attempt.submission_attempted_at is None
    assert execution.delivery.status == DripDelivery.Status.PLANNED
    assert execution.lane.status == DripLane.Status.PAUSED
    assert not Message.objects.filter(external_id__startswith="drip-linkedin:").exists()


def test_claimed_work_after_lane_pause_releases_completed_task_link(
    valid_drip_payload,
    fake_session,
    monkeypatch,
):
    execution = _execution(valid_drip_payload, fake_session)
    execution.lane.status = DripLane.Status.PAUSED
    execution.lane.save(update_fields=["status", "updated_at"])
    monkeypatch.setattr(
        "drip.tasks.linkedin.send_direct_message_once",
        lambda *args, **kwargs: pytest.fail("paused work must not send"),
    )

    handle_drip_linkedin(execution.task, fake_session, qualifiers=None)

    execution.delivery.refresh_from_db()
    execution.lane.refresh_from_db()
    assert execution.delivery.status == DripDelivery.Status.PLANNED
    assert execution.delivery.current_task_id is None
    assert execution.lane.status == DripLane.Status.PAUSED


def test_stale_recovery_requeues_only_attempts_that_never_crossed_submit_boundary(
    valid_drip_payload,
    fake_session,
):
    execution = _execution(valid_drip_payload, fake_session)
    execution.delivery.status = DripDelivery.Status.SENDING
    execution.delivery.save(update_fields=["status", "updated_at"])
    attempt = DripDeliveryAttempt.objects.create(
        delivery=execution.delivery,
        attempt_number=1,
    )

    result = recover_stale_linkedin_delivery(execution.delivery.pk)

    execution.delivery.refresh_from_db()
    execution.task.refresh_from_db()
    attempt.refresh_from_db()
    assert result == StaleRecoveryResult.REQUEUED
    assert execution.delivery.status == DripDelivery.Status.QUEUED
    assert execution.task.status == Task.Status.PENDING
    assert execution.task.started_at is None
    assert attempt.outcome == DripDeliveryAttempt.Outcome.NOT_SUBMITTED


def test_stale_recovery_never_retries_after_committed_submit_boundary(
    valid_drip_payload,
    fake_session,
):
    execution = _execution(valid_drip_payload, fake_session)
    execution.delivery.status = DripDelivery.Status.SENDING
    execution.delivery.save(update_fields=["status", "updated_at"])
    attempt = DripDeliveryAttempt.objects.create(
        delivery=execution.delivery,
        attempt_number=1,
        submission_attempted_at=timezone.now() - timedelta(minutes=30),
    )

    result = recover_stale_linkedin_delivery(execution.delivery.pk)

    execution.delivery.refresh_from_db()
    execution.lane.refresh_from_db()
    execution.task.refresh_from_db()
    attempt.refresh_from_db()
    assert result == StaleRecoveryResult.UNCLEAR
    assert execution.delivery.status == DripDelivery.Status.UNCLEAR
    assert execution.lane.status == DripLane.Status.PAUSED
    assert execution.task.status == Task.Status.COMPLETED
    assert attempt.outcome == DripDeliveryAttempt.Outcome.UNCLEAR


def test_stale_linkedin_recovery_does_not_requeue_while_lane_paused(
    valid_drip_payload,
    fake_session,
):
    execution = _execution(valid_drip_payload, fake_session)
    execution.lane.status = DripLane.Status.PAUSED
    execution.lane.save(update_fields=["status", "updated_at"])
    execution.delivery.status = DripDelivery.Status.SENDING
    execution.delivery.save(update_fields=["status", "updated_at"])
    attempt = DripDeliveryAttempt.objects.create(
        delivery=execution.delivery,
        attempt_number=1,
    )

    result = recover_stale_linkedin_delivery(execution.delivery.pk)

    execution.delivery.refresh_from_db()
    execution.task.refresh_from_db()
    attempt.refresh_from_db()
    assert result == StaleRecoveryResult.PLANNED
    assert execution.delivery.status == DripDelivery.Status.PLANNED
    assert execution.delivery.current_task_id is None
    assert execution.task.status == Task.Status.COMPLETED
    assert attempt.outcome == DripDeliveryAttempt.Outcome.NOT_SUBMITTED
