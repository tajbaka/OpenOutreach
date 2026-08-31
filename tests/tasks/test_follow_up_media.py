from __future__ import annotations

import json
from pathlib import Path

import pytest
from django.utils import timezone

from crm.models import Deal, Lead, Message
from linkedin.actions.message import (
    DirectMessageOutcome,
    DirectMessageResult,
    MessageSubmissionAborted,
)
from linkedin.enums import ProfileState
from linkedin.exceptions import LinkedInMessageSubmissionUnclearError
from linkedin.models import Task
from linkedin.tasks.follow_up import (
    _MediaFollowUpOutcome,
    _send_media_follow_up,
)
from linkedin.tasks.follow_up_submission import (
    SUBMISSION_OPERATOR_KEY,
    persisted_submission_evidence,
    stamp_submission_attempt,
    submission_attempted,
)


pytestmark = pytest.mark.django_db


def _execution(
    fake_session,
    tmp_path: Path,
    monkeypatch,
    *,
    filename: str = "demo.gif",
    payload: bytes = b"GIF89a-follow-up-media",
):
    fake_session.linkedin_profile.linkedin_username = "ariant@tryfedrampgpt.com"
    fake_session.linkedin_profile.save(update_fields=["linkedin_username"])
    lead = Lead.objects.create(
        first_name="Chuka",
        linkedin_url="https://www.linkedin.com/in/chuka-media-qa/",
        public_identifier="chuka-media-qa",
        description=json.dumps({"urn": "urn:li:fsd_profile:CHUKA_MEDIA_QA"}),
    )
    deal = Deal.objects.create(
        lead=lead,
        campaign=fake_session.campaign,
        state=ProfileState.CONNECTED,
        connected_at=timezone.now(),
    )
    Message.objects.create(
        lead=lead,
        source=Message.Source.LINKEDIN,
        external_id="follow-up-media-seed",
        direction=Message.Direction.OUTBOUND,
        sender=fake_session.linkedin_profile.linkedin_username,
        body="Connection note",
        sent_at=timezone.now(),
    )
    asset_root = tmp_path / "assets" / "follow_up"
    asset_root.mkdir(parents=True)
    attachment = asset_root / filename
    attachment.write_bytes(payload)
    monkeypatch.setattr("linkedin.conf.ROOT_DIR", tmp_path)
    monkeypatch.setattr("linkedin.message_media.ROOT_DIR", tmp_path)
    return lead, deal, attachment


def _send(
    *,
    fake_session,
    deal,
    attachment,
):
    task = Task.objects.create(
        task_type=Task.TaskType.FOLLOW_UP,
        status=Task.Status.RUNNING,
        scheduled_at=timezone.now(),
        started_at=timezone.now(),
        payload={
            "campaign_id": fake_session.campaign.pk,
            "public_id": "chuka-media-qa",
            "operator": "Arian",
            "sequence_name": "linkedin_connect_followup",
            "channel": "linkedin_connect_followup",
            "step_index": 0,
        },
    )
    return _send_media_follow_up(
        task=task,
        session=fake_session,
        deal=deal,
        public_id="chuka-media-qa",
        body="Hey Chuka — thought you’d find this useful.",
        attachment=attachment,
        campaign_id=fake_session.campaign.pk,
        sequence_name="linkedin_connect_followup",
        step_index=0,
        operator="Arian",
    )


@pytest.mark.parametrize(
    ("filename", "payload", "expected_kind", "expected_mime_type"),
    (
        ("demo.gif", b"GIF89a-follow-up-media", "gif", "image/gif"),
        (
            "overview.mp4",
            b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2",
            "video",
            "video/mp4",
        ),
    ),
)
def test_confirmed_media_send_persists_exact_evidence(
    fake_session,
    tmp_path,
    monkeypatch,
    filename,
    payload,
    expected_kind,
    expected_mime_type,
):
    lead, deal, attachment = _execution(
        fake_session,
        tmp_path,
        monkeypatch,
        filename=filename,
        payload=payload,
    )

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
        assert member_urn == "urn:li:fsd_profile:CHUKA_MEDIA_QA"
        assert recipient_label == lead.linkedin_url
        assert body == "Hey Chuka — thought you’d find this useful."
        assert media.reference == filename
        assert media.kind.value == expected_kind
        assert media.mime_type == expected_mime_type
        on_submit_attempt()
        return DirectMessageResult(DirectMessageOutcome.SENT)

    monkeypatch.setattr("linkedin.actions.message.send_direct_message_once", send_once)

    result = _send(
        fake_session=fake_session,
        deal=deal,
        attachment=attachment,
    )

    assert result == _MediaFollowUpOutcome.SENT
    outbound = Message.objects.get(
        lead=lead,
        external_id__startswith="daemon-send:Arian:",
    )
    assert outbound.raw["media"]["type"] == expected_kind
    assert outbound.raw["media"]["reference"] == filename
    assert outbound.raw["media"]["mime_type"] == expected_mime_type
    assert outbound.raw["media"]["size_bytes"] == attachment.stat().st_size
    assert len(outbound.raw["media"]["sha256"]) == 64
    task = Task.objects.get(task_type=Task.TaskType.FOLLOW_UP)
    assert submission_attempted(task.payload)
    assert task.payload[SUBMISSION_OPERATOR_KEY] == "Arian"
    assert persisted_submission_evidence(task.payload)


def test_reply_arriving_during_upload_blocks_click_and_finishes_sequence(
    fake_session,
    tmp_path,
    monkeypatch,
):
    lead, deal, attachment = _execution(fake_session, tmp_path, monkeypatch)
    clicked = []

    def send_once(*args, on_submit_attempt, **kwargs):
        Message.objects.create(
            lead=lead,
            source=Message.Source.LINKEDIN,
            external_id="follow-up-media-inbound",
            direction=Message.Direction.INBOUND,
            sender="Chuka",
            body="Thanks — I’ll take a look.",
            sent_at=timezone.now(),
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

    monkeypatch.setattr("linkedin.actions.message.send_direct_message_once", send_once)

    result = _send(
        fake_session=fake_session,
        deal=deal,
        attachment=attachment,
    )

    deal.refresh_from_db()
    assert result == _MediaFollowUpOutcome.BLOCKED
    assert clicked == []
    assert deal.state == ProfileState.COMPLETED
    assert not Message.objects.filter(
        lead=lead,
        external_id__startswith="daemon-send:Arian:",
    ).exists()
    task = Task.objects.get(task_type=Task.TaskType.FOLLOW_UP)
    assert not submission_attempted(task.payload)


def test_pre_submit_media_failure_remains_retryable(
    fake_session,
    tmp_path,
    monkeypatch,
):
    _lead, deal, attachment = _execution(fake_session, tmp_path, monkeypatch)
    monkeypatch.setattr(
        "linkedin.actions.message.send_direct_message_once",
        lambda *args, **kwargs: DirectMessageResult(
            DirectMessageOutcome.PRE_SUBMIT_FAILED,
            "attachment did not become ready",
        ),
    )

    result = _send(
        fake_session=fake_session,
        deal=deal,
        attachment=attachment,
    )

    assert result == _MediaFollowUpOutcome.RETRYABLE_FAILURE
    task = Task.objects.get(task_type=Task.TaskType.FOLLOW_UP)
    assert not submission_attempted(task.payload)


def test_reply_during_failed_upload_is_rechecked_and_not_retried(
    fake_session,
    tmp_path,
    monkeypatch,
):
    lead, deal, attachment = _execution(fake_session, tmp_path, monkeypatch)

    def fail_upload(*args, **kwargs):
        Message.objects.create(
            lead=lead,
            source=Message.Source.LINKEDIN,
            external_id="follow-up-media-inbound-during-failed-upload",
            direction=Message.Direction.INBOUND,
            sender="Chuka",
            body="Saw this come through — thanks.",
            sent_at=timezone.now(),
        )
        return DirectMessageResult(
            DirectMessageOutcome.PRE_SUBMIT_FAILED,
            "attachment did not become ready",
        )

    monkeypatch.setattr("linkedin.actions.message.send_direct_message_once", fail_upload)

    result = _send(
        fake_session=fake_session,
        deal=deal,
        attachment=attachment,
    )

    deal.refresh_from_db()
    assert result == _MediaFollowUpOutcome.BLOCKED
    assert deal.state == ProfileState.COMPLETED
    task = Task.objects.get(task_type=Task.TaskType.FOLLOW_UP)
    assert not submission_attempted(task.payload)


def test_unclear_media_submission_raises_and_cannot_be_retried_as_failure(
    fake_session,
    tmp_path,
    monkeypatch,
):
    _lead, deal, attachment = _execution(fake_session, tmp_path, monkeypatch)

    def send_once(*args, on_submit_attempt, **kwargs):
        on_submit_attempt()
        return DirectMessageResult(
            DirectMessageOutcome.UNCLEAR,
            "Send was clicked but confirmation was absent",
        )

    monkeypatch.setattr("linkedin.actions.message.send_direct_message_once", send_once)

    with pytest.raises(
        LinkedInMessageSubmissionUnclearError,
        match="submission is unclear",
    ):
        _send(
            fake_session=fake_session,
            deal=deal,
            attachment=attachment,
        )
    task = Task.objects.get(task_type=Task.TaskType.FOLLOW_UP)
    assert submission_attempted(task.payload)
    assert not persisted_submission_evidence(task.payload)


def test_confirmed_ui_send_without_durable_message_evidence_is_unclear(
    fake_session,
    tmp_path,
    monkeypatch,
):
    _lead, deal, attachment = _execution(fake_session, tmp_path, monkeypatch)

    def send_once(*args, on_submit_attempt, **kwargs):
        on_submit_attempt()
        return DirectMessageResult(DirectMessageOutcome.SENT)

    monkeypatch.setattr("linkedin.actions.message.send_direct_message_once", send_once)
    monkeypatch.setattr("linkedin.db.chat.save_chat_message", lambda *args, **kwargs: None)

    with pytest.raises(
        LinkedInMessageSubmissionUnclearError,
        match="exact durable Message evidence is absent",
    ):
        _send(
            fake_session=fake_session,
            deal=deal,
            attachment=attachment,
        )

    task = Task.objects.get(task_type=Task.TaskType.FOLLOW_UP)
    assert submission_attempted(task.payload)
    assert not persisted_submission_evidence(task.payload)


def test_final_lead_locked_guard_blocks_new_sibling_uncertainty(
    fake_session,
    tmp_path,
    monkeypatch,
):
    lead, deal, attachment = _execution(fake_session, tmp_path, monkeypatch)
    first = Task.objects.create(
        task_type=Task.TaskType.FOLLOW_UP,
        status=Task.Status.RUNNING,
        scheduled_at=timezone.now(),
        started_at=timezone.now(),
        payload={
            "campaign_id": fake_session.campaign.pk,
            "public_id": "chuka-media-qa",
            "operator": "Arian",
            "sequence_name": "other_campaign_sequence",
            "step_index": 0,
        },
    )
    stamp_submission_attempt(
        first,
        lead_id=lead.pk,
        message_prefix=(
            f"daemon-send:Arian:{deal.pk}:other_campaign_sequence:step-0:"
        ),
        operator="Arian",
        final_guard=lambda: None,
    )
    clicks = []

    def send_once(*args, on_submit_attempt, **kwargs):
        try:
            on_submit_attempt()
        except MessageSubmissionAborted as exc:
            return DirectMessageResult(DirectMessageOutcome.PRE_SUBMIT_FAILED, str(exc))
        clicks.append(True)
        return DirectMessageResult(DirectMessageOutcome.SENT)

    monkeypatch.setattr("linkedin.actions.message.send_direct_message_once", send_once)

    result = _send(
        fake_session=fake_session,
        deal=deal,
        attachment=attachment,
    )

    assert result == _MediaFollowUpOutcome.BLOCKED
    assert clicks == []
    sibling = Task.objects.exclude(pk=first.pk).get(
        task_type=Task.TaskType.FOLLOW_UP,
    )
    assert not submission_attempted(sibling.payload)
