from unittest.mock import patch

import daemon_supervisor


def test_gmail_account_for_supervisor_uses_existing_alias_mapping(monkeypatch):
    monkeypatch.setenv("LINKEDIN_USERNAME", "ariantajbakh@gmail.com")

    assert daemon_supervisor._gmail_account_for_supervisor() == "arian_boundera"


def test_gmail_account_for_supervisor_returns_none_for_unmapped_identity(monkeypatch):
    monkeypatch.setenv("LINKEDIN_USERNAME", "not-configured@example.com")

    assert daemon_supervisor._gmail_account_for_supervisor() is None


@patch("daemon_supervisor.subprocess.Popen")
def test_start_gmail_worker_uses_independent_management_command(mock_popen):
    daemon_supervisor._start_gmail_worker("eddy_boundera")

    args, kwargs = mock_popen.call_args
    assert args[0] == [
        daemon_supervisor.sys.executable,
        "manage.py",
        "run_gmail_worker",
        "--account",
        "eddy_boundera",
    ]
    assert kwargs["cwd"] == daemon_supervisor.ROOT_DIR
    assert kwargs["env"]["OPENOUTREACH_SUPERVISED"] == "1"
