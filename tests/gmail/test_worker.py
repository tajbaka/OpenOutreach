from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from crm.models import Lead, Message
from gmail.submission import SUBMISSION_ATTEMPTED_AT_KEY
from gmail.worker import GmailWorker
from linkedin.models import Task


def _gmail_task(
    status=Task.Status.PENDING,
    *,
    operator="Arian",
    lead_id=1,
    started_at=None,
    scheduled_at=None,
):
    return Task.objects.create(
        task_type=Task.TaskType.GMAIL_FOLLOW_UP,
        status=status,
        started_at=started_at,
        scheduled_at=scheduled_at or timezone.now() - timedelta(seconds=1),
        payload={"lead_id": lead_id, "operator": operator, "step_index": 0},
    )


def _drip_gmail_task(
    status=Task.Status.PENDING,
    *,
    operator="Arian",
    started_at=None,
):
    return Task.objects.create(
        task_type=Task.TaskType.DRIP_GMAIL,
        status=status,
        started_at=started_at,
        scheduled_at=timezone.now() - timedelta(seconds=1),
        payload={"delivery_id": 123, "operator": operator},
    )


@pytest.mark.django_db
def test_gmail_worker_run_once_marks_completed():
    task = _gmail_task()
    seen = {}

    def record_claimed_task(claimed):
        seen["status"] = claimed.status
        seen["started_at"] = claimed.started_at

    with patch(
        "gmail.worker.handle_gmail_follow_up",
        side_effect=record_claimed_task,
    ) as mock_handler:
        assert GmailWorker(account_key="arian_boundera")._run_once() is True
    task.refresh_from_db()
    assert task.status == Task.Status.COMPLETED
    mock_handler.assert_called_once_with(task)
    assert seen["status"] == Task.Status.RUNNING
    assert seen["started_at"] is not None


@pytest.mark.django_db
def test_gmail_worker_reclaims_stale_running_tasks():
    stale = _gmail_task(
        status=Task.Status.RUNNING,
        started_at=timezone.now() - timedelta(hours=1),
    )
    fresh = _gmail_task(
        status=Task.Status.RUNNING,
        started_at=timezone.now(),
    )
    other_account = _gmail_task(
        status=Task.Status.RUNNING,
        operator="Athena",
        started_at=timezone.now() - timedelta(hours=1),
    )

    GmailWorker(account_key="arian_boundera")._reclaim_stale()

    stale.refresh_from_db()
    fresh.refresh_from_db()
    other_account.refresh_from_db()
    assert stale.status == Task.Status.PENDING
    assert stale.started_at is None
    assert fresh.status == Task.Status.RUNNING
    assert other_account.status == Task.Status.RUNNING


@pytest.mark.django_db
def test_gmail_worker_never_requeues_stale_post_submission_task():
    stale = _gmail_task(
        status=Task.Status.RUNNING,
        started_at=timezone.now() - timedelta(hours=1),
    )
    stale.payload = {
        **stale.payload,
        SUBMISSION_ATTEMPTED_AT_KEY: timezone.now().isoformat(),
    }
    stale.save(update_fields={"payload"})

    GmailWorker(account_key="arian_boundera")._reclaim_stale()

    stale.refresh_from_db()
    assert stale.status == Task.Status.FAILED
    assert "automatic retry is blocked" in stale.error


@pytest.mark.django_db
def test_gmail_worker_requeues_stale_post_submission_with_exact_message():
    lead = Lead.objects.create(
        first_name="Ada",
        email="ada@example.com",
        icp="CSPs",
    )
    stale = _gmail_task(
        status=Task.Status.RUNNING,
        lead_id=lead.pk,
        started_at=timezone.now() - timedelta(hours=1),
    )
    stale.payload = {
        **stale.payload,
        SUBMISSION_ATTEMPTED_AT_KEY: timezone.now().isoformat(),
    }
    stale.save(update_fields={"payload"})
    Message.objects.create(
        lead=lead,
        source=Message.Source.GMAIL,
        direction=Message.Direction.OUTBOUND,
        external_id="arian_boundera:sent-before-crash",
        sender="ariant@getboundera.com",
        sent_at=timezone.now(),
        raw={
            "automation_key": (
                f"gmail_follow_up:Arian:{lead.pk}:gmail_fallback:step-0"
            ),
        },
    )

    GmailWorker(account_key="arian_boundera")._reclaim_stale()

    stale.refresh_from_db()
    assert stale.status == Task.Status.PENDING
    assert stale.started_at is None


@pytest.mark.django_db
def test_gmail_worker_delays_retry_after_post_send_handler_failure():
    lead = Lead.objects.create(
        first_name="Ada",
        email="ada@example.com",
        icp="CSPs",
    )
    task = _gmail_task(lead_id=lead.pk)

    def persist_then_fail(claimed):
        payload = {
            **claimed.payload,
            SUBMISSION_ATTEMPTED_AT_KEY: timezone.now().isoformat(),
        }
        Task.objects.filter(pk=claimed.pk).update(payload=payload)
        Message.objects.create(
            lead=lead,
            source=Message.Source.GMAIL,
            direction=Message.Direction.OUTBOUND,
            external_id="arian_boundera:sent-before-successor-error",
            sender="ariant@getboundera.com",
            sent_at=timezone.now(),
            raw={
                "automation_key": (
                    f"gmail_follow_up:Arian:{lead.pk}:gmail_fallback:step-0"
                ),
            },
        )
        raise RuntimeError("successor enqueue failed")

    with (
        patch("gmail.worker.handle_gmail_follow_up", side_effect=persist_then_fail),
        patch("gmail.worker.notify_error"),
    ):
        assert GmailWorker(account_key="arian_boundera")._run_once() is True

    task.refresh_from_db()
    assert task.status == Task.Status.PENDING
    assert task.started_at is None
    assert task.scheduled_at > timezone.now() + timedelta(minutes=4)


@pytest.mark.django_db
def test_gmail_worker_run_once_no_task_returns_false():
    assert GmailWorker(account_key="arian_boundera")._run_once() is False


@pytest.mark.django_db
def test_gmail_worker_claims_all_aliases_for_its_account_only():
    arian_task = _gmail_task(operator="Arian")
    leili_task = _gmail_task(operator="Leili")
    athena_task = _gmail_task(operator="Athena")

    with patch("gmail.worker.handle_gmail_follow_up") as mock_handler:
        worker = GmailWorker(account_key="arian_boundera")
        assert worker._run_once() is True
        assert worker._run_once() is True
        assert worker._run_once() is False

    arian_task.refresh_from_db()
    leili_task.refresh_from_db()
    athena_task.refresh_from_db()
    assert arian_task.status == Task.Status.COMPLETED
    assert leili_task.status == Task.Status.COMPLETED
    assert athena_task.status == Task.Status.PENDING
    assert [call.args[0].payload["operator"] for call in mock_handler.call_args_list] == [
        "Arian",
        "Leili",
    ]


@pytest.mark.django_db
def test_gmail_worker_claim_is_guarded_and_already_running():
    task = _gmail_task()
    first_worker = GmailWorker(account_key="arian_boundera")
    second_worker = GmailWorker(account_key="arian_boundera")

    claimed = first_worker._claim_next()

    assert claimed.pk == task.pk
    assert claimed.status == Task.Status.RUNNING
    assert claimed.started_at is not None
    assert second_worker._claim_next() is None
    task.refresh_from_db()
    assert task.status == Task.Status.RUNNING


@pytest.mark.django_db
def test_gmail_worker_does_not_claim_future_task():
    _gmail_task(scheduled_at=timezone.now() + timedelta(minutes=5))

    assert GmailWorker(account_key="arian_boundera")._claim_next() is None


@pytest.mark.django_db
def test_gmail_worker_failure_marks_task_failed():
    task = _gmail_task()
    with (
        patch(
            "gmail.worker.handle_gmail_follow_up",
            side_effect=RuntimeError("send broke"),
        ),
        patch("gmail.worker.notify_error") as notify_error,
    ):
        assert GmailWorker(account_key="arian_boundera")._run_once() is True

    task.refresh_from_db()
    assert task.status == Task.Status.FAILED
    assert "send broke" in task.error
    assert notify_error.call_args.kwargs["context"]["account"] == "arian_boundera"


@pytest.mark.django_db
def test_gmail_worker_routes_drip_gmail_for_account_alias():
    task = _drip_gmail_task(operator="Leili")

    with patch("gmail.worker.handle_drip_gmail") as handler:
        assert GmailWorker(account_key="arian_boundera")._run_once() is True

    task.refresh_from_db()
    assert task.status == Task.Status.COMPLETED
    handler.assert_called_once_with(task)


@pytest.mark.django_db
def test_gmail_worker_does_not_generically_reset_stale_drip_attempt():
    task = _drip_gmail_task(
        status=Task.Status.RUNNING,
        started_at=timezone.now() - timedelta(hours=1),
    )

    with patch(
        "gmail.worker.recover_stale_drip_gmail_task",
        return_value=True,
    ) as recover:
        GmailWorker(account_key="arian_boundera")._reclaim_stale()

    recover.assert_called_once_with(task.pk)
    task.refresh_from_db()
    assert task.status == Task.Status.RUNNING
