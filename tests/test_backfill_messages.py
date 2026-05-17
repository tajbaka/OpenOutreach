"""Tests for the backfill_messages --account / --skip-prereq-gate options."""
from __future__ import annotations

from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError


@pytest.fixture
def both_accounts(monkeypatch):
    monkeypatch.setenv("LINKEDIN_USERNAME", "primary@x.com")
    monkeypatch.setenv("LINKEDIN_PASSWORD", "p")
    monkeypatch.setenv("BACKFILL_LINKEDIN_USERNAME", "backfill@x.com")
    monkeypatch.setenv("BACKFILL_LINKEDIN_PASSWORD", "p")


def test_account_flag_restricts_to_one_slot(db, both_accounts):
    """--account primary must iterate only the primary slot."""
    seen = []

    def fake_make_session(label, env_user, env_pass):
        seen.append(label)
        raise RuntimeError("stop before login")  # we only assert the slot set

    with patch("linkedin.management.commands.backfill_messages._make_session",
               side_effect=fake_make_session), \
         patch("linkedin.management.commands.backfill_messages._run_prereq_gate_for_accounts",
               return_value=True):
        call_command("backfill_messages", account="primary")

    assert seen == ["primary"]


def test_unknown_account_raises(db, both_accounts):
    with pytest.raises(CommandError):
        call_command("backfill_messages", account="nonsense")


def test_skip_prereq_gate_bypasses_the_gate(db, both_accounts):
    with patch("linkedin.management.commands.backfill_messages._run_prereq_gate_for_accounts") as gate, \
         patch("linkedin.management.commands.backfill_messages._make_session",
               side_effect=RuntimeError("stop")):
        call_command("backfill_messages", account="primary", skip_prereq_gate=True)
    gate.assert_not_called()


def test_default_run_still_calls_the_gate(db, both_accounts):
    with patch("linkedin.management.commands.backfill_messages._run_prereq_gate_for_accounts",
               return_value=True) as gate, \
         patch("linkedin.management.commands.backfill_messages._make_session",
               side_effect=RuntimeError("stop")):
        call_command("backfill_messages")
    gate.assert_called_once()
