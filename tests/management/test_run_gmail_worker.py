from unittest.mock import patch

from django.core.management import call_command


def test_run_gmail_worker_is_account_scoped_and_guarded():
    with (
        patch(
            "linkedin.management.commands.run_gmail_worker.GmailWorker"
        ) as worker_class,
        patch(
            "linkedin.management.commands.run_gmail_worker.SingleInstanceGuard"
        ) as guard_class,
    ):
        call_command("run_gmail_worker", account="arian_boundera")

    worker_class.assert_called_once_with(account_key="arian_boundera")
    worker_class.return_value.run_forever.assert_called_once_with()
    worker_class.return_value.stop.assert_called_once_with()
    guard_class.assert_called_once()
    guard_kwargs = guard_class.call_args.kwargs
    assert guard_kwargs["pidfile"].name == (
        "run-gmail-worker-arian_boundera.pid"
    )
    assert guard_kwargs["marker"] == (
        "manage.py run_gmail_worker --account arian_boundera"
    )
    guard_class.return_value.acquire.assert_called_once_with()
    guard_class.return_value.release.assert_called_once_with()
