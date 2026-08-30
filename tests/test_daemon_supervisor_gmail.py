from argparse import Namespace
from unittest.mock import patch

import daemon_supervisor


def _supervisor_args(**overrides):
    values = {
        "no_update": False,
        "no_install": False,
        "no_migrate": False,
        "requirements": "requirements/local.txt",
    }
    values.update(overrides)
    return Namespace(**values)


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


@patch("daemon_supervisor._pull_update")
def test_maybe_pull_update_preserves_terminal_auto_update(mock_pull):
    mock_pull.return_value = True

    assert daemon_supervisor._maybe_pull_update(_supervisor_args()) is True

    mock_pull.assert_called_once_with(
        install=True,
        migrate=True,
        requirements_file="requirements/local.txt",
    )


@patch("daemon_supervisor._pull_update")
def test_maybe_pull_update_skips_git_for_immutable_deployments(mock_pull):
    assert (
        daemon_supervisor._maybe_pull_update(_supervisor_args(no_update=True))
        is False
    )

    mock_pull.assert_not_called()
