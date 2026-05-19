"""Daemon startup integrity checks."""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from linkedin import env_check, env_spec, version_check


def _completed(stdout: str = "", returncode: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess(
        args=["git"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


class TestGitHelpers:
    def test_is_git_checkout_true(self, monkeypatch):
        monkeypatch.setattr(
            version_check,
            "_git",
            lambda args, check=True: _completed(stdout="true\n"),
        )
        assert version_check._is_git_checkout() is True

    def test_is_git_checkout_false_when_not_a_repo(self, monkeypatch):
        monkeypatch.setattr(
            version_check,
            "_git",
            lambda args, check=True: _completed(stdout="", returncode=128),
        )
        assert version_check._is_git_checkout() is False

    def test_is_git_checkout_false_when_git_missing(self, monkeypatch):
        def _raise(args, check=True):
            raise FileNotFoundError("git")

        monkeypatch.setattr(version_check, "_git", _raise)
        assert version_check._is_git_checkout() is False

    def test_upstream_ref_returns_tracking_branch(self, monkeypatch):
        monkeypatch.setattr(
            version_check,
            "_git",
            lambda args, check=True: _completed(stdout="origin/main\n"),
        )
        assert version_check._upstream_ref() == "origin/main"

    def test_upstream_ref_none_when_no_upstream(self, monkeypatch):
        monkeypatch.setattr(
            version_check,
            "_git",
            lambda args, check=True: _completed(stdout="", returncode=128),
        )
        assert version_check._upstream_ref() is None

    def test_commits_behind_parses_count(self, monkeypatch):
        monkeypatch.setattr(
            version_check,
            "_git",
            lambda args, check=True: _completed(stdout="3\n"),
        )
        assert version_check._commits_behind("origin/main") == 3

    def test_commits_behind_zero_on_git_error(self, monkeypatch):
        monkeypatch.setattr(
            version_check,
            "_git",
            lambda args, check=True: _completed(stdout="", returncode=1),
        )
        assert version_check._commits_behind("origin/main") == 0


class TestCheckForUpdates:
    def test_skips_when_not_a_git_checkout(self, monkeypatch):
        monkeypatch.setattr(version_check, "_is_git_checkout", lambda: False)
        version_check.check_for_updates()

    def test_skips_when_no_upstream(self, monkeypatch):
        monkeypatch.setattr(version_check, "_is_git_checkout", lambda: True)
        monkeypatch.setattr(version_check, "_upstream_ref", lambda: None)
        version_check.check_for_updates()

    def test_continues_when_fetch_fails(self, monkeypatch):
        monkeypatch.setattr(version_check, "_is_git_checkout", lambda: True)
        monkeypatch.setattr(version_check, "_upstream_ref", lambda: "origin/main")
        monkeypatch.setattr(
            version_check,
            "_git",
            lambda args, check=True: _completed(returncode=1, stderr="no network"),
        )
        version_check.check_for_updates()

    def test_continues_when_up_to_date(self, monkeypatch):
        monkeypatch.setattr(version_check, "_is_git_checkout", lambda: True)
        monkeypatch.setattr(version_check, "_upstream_ref", lambda: "origin/main")
        monkeypatch.setattr(
            version_check,
            "_git",
            lambda args, check=True: _completed(returncode=0),
        )
        monkeypatch.setattr(version_check, "_commits_behind", lambda upstream: 0)
        version_check.check_for_updates()

    def test_interactive_decline_continues(self, monkeypatch):
        monkeypatch.setattr(version_check, "_is_git_checkout", lambda: True)
        monkeypatch.setattr(version_check, "_upstream_ref", lambda: "origin/main")
        monkeypatch.setattr(
            version_check,
            "_git",
            lambda args, check=True: _completed(returncode=0),
        )
        monkeypatch.setattr(version_check, "_commits_behind", lambda upstream: 2)
        monkeypatch.setattr(version_check, "_stdio_is_tty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda prompt="": "n")
        pull = MagicMock()
        monkeypatch.setattr(version_check, "_pull_and_exit", pull)
        version_check.check_for_updates()
        pull.assert_not_called()

    def test_interactive_accept_pulls(self, monkeypatch):
        monkeypatch.setattr(version_check, "_is_git_checkout", lambda: True)
        monkeypatch.setattr(version_check, "_upstream_ref", lambda: "origin/main")
        monkeypatch.setattr(
            version_check,
            "_git",
            lambda args, check=True: _completed(returncode=0),
        )
        monkeypatch.setattr(version_check, "_commits_behind", lambda upstream: 2)
        monkeypatch.setattr(version_check, "_stdio_is_tty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda prompt="": "y")
        pull = MagicMock()
        monkeypatch.setattr(version_check, "_pull_and_exit", pull)
        version_check.check_for_updates()
        pull.assert_called_once()

    def test_headless_auto_pulls(self, monkeypatch):
        monkeypatch.setattr(version_check, "_is_git_checkout", lambda: True)
        monkeypatch.setattr(version_check, "_upstream_ref", lambda: "origin/main")
        monkeypatch.setattr(
            version_check,
            "_git",
            lambda args, check=True: _completed(returncode=0),
        )
        monkeypatch.setattr(version_check, "_commits_behind", lambda upstream: 5)
        monkeypatch.setattr(version_check, "_stdio_is_tty", lambda: False)
        pull = MagicMock()
        monkeypatch.setattr(version_check, "_pull_and_exit", pull)
        version_check.check_for_updates()
        pull.assert_called_once()

    def test_pull_success_exits_zero(self, monkeypatch):
        monkeypatch.setattr(
            version_check,
            "_git",
            lambda args, check=True: _completed(returncode=0),
        )
        with pytest.raises(SystemExit) as exc:
            version_check._pull_and_exit()
        assert exc.value.code == 0

    def test_pull_failure_notifies_and_exits_one(self, monkeypatch):
        def _raise(args, check=True):
            raise subprocess.CalledProcessError(
                returncode=1,
                cmd=["git", "pull"],
                stderr="local changes",
            )

        monkeypatch.setattr(version_check, "_git", _raise)
        notify = MagicMock()
        monkeypatch.setattr("linkedin.notifications.slack.notify_error", notify)
        with pytest.raises(SystemExit) as exc:
            version_check._pull_and_exit()
        assert exc.value.code == 1
        notify.assert_called_once()


class TestEnvSpec:
    def test_registry_is_non_empty(self):
        assert len(env_spec.ENV_VARS) > 0

    def test_var_names_are_unique(self):
        names = [var.name for var in env_spec.ENV_VARS]
        assert len(names) == len(set(names))

    def test_known_required_vars_present(self):
        required = {var.name for var in env_spec.ENV_VARS if var.required}
        assert {"LLM_API_KEY", "LINKEDIN_USERNAME", "LINKEDIN_PASSWORD"} <= required

    def test_django_internal_vars_excluded(self):
        names = {var.name for var in env_spec.ENV_VARS}
        assert "DJANGO_SETTINGS_MODULE" not in names
        assert "DJANGO_ALLOW_ASYNC_UNSAFE" not in names

    def test_every_var_has_a_group_and_description(self):
        for var in env_spec.ENV_VARS:
            assert var.group, f"{var.name} missing group"
            assert var.description, f"{var.name} missing description"

    def test_dotenv_example_matches_registry(self):
        example_path = Path(".env.example")
        assert example_path.exists(), ".env.example is missing"

        example_names = []
        for line in example_path.read_text().splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            name = stripped.split("=", 1)[0].strip()
            example_names.append(name)

        registry_names = [var.name for var in env_spec.ENV_VARS]
        assert example_names == registry_names


class TestCheckEnvVars:
    def _isolate_env(self, monkeypatch):
        for var in env_spec.ENV_VARS:
            monkeypatch.delenv(var.name, raising=False)

    def test_warns_when_required_var_missing(self, monkeypatch, caplog):
        self._isolate_env(monkeypatch)
        with caplog.at_level("WARNING", logger="linkedin.env_check"):
            env_check.check_env_vars()
        assert "LLM_API_KEY" in caplog.text
        assert any(record.levelname == "WARNING" for record in caplog.records)

    def test_optional_without_default_reported_as_optional(self, monkeypatch, caplog):
        self._isolate_env(monkeypatch)
        for var in env_spec.ENV_VARS:
            if var.required:
                monkeypatch.setenv(var.name, "x")
        with caplog.at_level("INFO", logger="linkedin.env_check"):
            env_check.check_env_vars()
        assert "GOOGLE_SHEETS_ID" in caplog.text

    def test_optional_with_default_not_reported(self, monkeypatch, caplog):
        self._isolate_env(monkeypatch)
        for var in env_spec.ENV_VARS:
            if var.required:
                monkeypatch.setenv(var.name, "x")
        with caplog.at_level("INFO", logger="linkedin.env_check"):
            env_check.check_env_vars()
        assert "GOOGLE_SHEETS_TAB_NAME" not in caplog.text

    def test_silent_when_all_present(self, monkeypatch, caplog):
        for var in env_spec.ENV_VARS:
            monkeypatch.setenv(var.name, "value")
        with caplog.at_level("DEBUG", logger="linkedin.env_check"):
            env_check.check_env_vars()
        assert not any(record.levelname == "WARNING" for record in caplog.records)

    def test_never_raises(self, monkeypatch):
        self._isolate_env(monkeypatch)
        assert env_check.check_env_vars() is None
