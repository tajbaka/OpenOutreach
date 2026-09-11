"""The command converts termination signals into listener cleanup requests."""
import signal
from unittest.mock import MagicMock, patch

import pytest

from linkedin.management.commands.listen_realtime import Command


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_signal_requests_cooperative_stop_and_restores_handlers(signum):
    original = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    profile = MagicMock(linkedin_username="a@x.com")

    def run_listener(*, operator, username, stop_event):
        assert not stop_event.is_set()
        signal.getsignal(signum)(signum, None)
        assert stop_event.is_set()
        return 0

    with patch("linkedin.conf.get_daemon_handle", return_value="arian"), \
         patch("linkedin.models.LinkedInProfile.objects") as profiles, \
         patch("linkedin.operators.resolve_operator", return_value="Arian"), \
         patch("linkedin.single_instance.SingleInstanceGuard") as guard, \
         patch("linkedin.realtime.listener.run_listener", side_effect=run_listener):
        profiles.select_related.return_value.filter.return_value.first.return_value = profile
        with pytest.raises(SystemExit) as result:
            Command().handle()
    assert result.value.code == 0
    guard.return_value.release.assert_called_once()
    assert {sig: signal.getsignal(sig) for sig in original} == original
