from unittest.mock import patch

import pytest
from django.core.management import call_command


@pytest.mark.parametrize("failure", [None, KeyboardInterrupt(), RuntimeError("worker loop failed")])
def test_email_enrichment_command_is_browserless_sender_scoped_and_guarded(failure):
    with (
        patch("linkedin.management.commands.run_email_enrichment.EnrichmentWorker") as worker_class,
        patch("linkedin.management.commands.run_email_enrichment.SingleInstanceGuard") as guard_class,
        patch("linkedin.browser.login.launch_browser", side_effect=AssertionError("No browser")) as browser,
    ):
        worker_class.return_value.run_forever.side_effect = failure
        if isinstance(failure, RuntimeError):
            with pytest.raises(RuntimeError, match="worker loop failed"):
                call_command("run_email_enrichment", operator="Chuka")
        else:
            call_command("run_email_enrichment", operator="Chuka")

    worker_class.assert_called_once_with(operator="Chuka", include_phone=False)
    worker_class.return_value.run_forever.assert_called_once_with()
    worker_class.return_value.stop.assert_called_once_with()
    assert guard_class.call_args.kwargs["pidfile"].name == "run-email-enrichment-chuka.pid"
    assert guard_class.call_args.kwargs["marker"] == "manage.py run_email_enrichment --operator Chuka"
    guard_class.return_value.acquire.assert_called_once_with()
    guard_class.return_value.release.assert_called_once_with()
    browser.assert_not_called()
