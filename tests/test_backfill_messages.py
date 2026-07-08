"""Tests for the backfill_messages --account / --skip-prereq-gate options."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from crm.models import Deal, Lead, Message
from linkedin.enums import ProfileState
from linkedin.management.commands.backfill_messages import Command
from linkedin.models import Campaign


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


def _campaign(name: str = "Backfill test") -> Campaign:
    user = User.objects.create_user(username=f"{name.lower().replace(' ', '-')}-user")
    return Campaign.objects.create(name=name, user=user)


@pytest.mark.django_db
def test_backfill_notifies_newly_persisted_inbound_message(monkeypatch):
    campaign = _campaign()
    lead = Lead.objects.create(
        first_name="Waylon",
        last_name="Krush",
        public_identifier="waylonkrush",
        linkedin_url="https://www.linkedin.com/in/waylonkrush/",
    )
    Deal.objects.create(lead=lead, campaign=campaign, state=ProfileState.CONNECTED)
    Message.objects.create(
        lead=lead,
        source=Message.Source.LINKEDIN,
        direction=Message.Direction.OUTBOUND,
        external_id="outbound-1",
        sender="Athena Aghdami",
        body="Sent you a note.",
        sent_at=timezone.now(),
    )

    def fake_get_conversation(_session, public_identifier):
        assert public_identifier == "waylonkrush"
        Message.objects.create(
            lead=lead,
            source=Message.Source.LINKEDIN,
            direction=Message.Direction.INBOUND,
            external_id="inbound-1",
            sender="waylon krush",
            body="Missed you while the daemon was down.",
            sent_at=timezone.now(),
            thread_external_id="thread-1",
        )
        return [{"entity_urn": "inbound-1"}]

    with patch("linkedin.management.commands.backfill_messages._open_messaging_inbox"), \
         patch(
             "linkedin.management.commands.backfill_messages.get_conversation",
             side_effect=fake_get_conversation,
         ), \
         patch(
             "linkedin.management.commands.backfill_messages._run_prereq_gate_for_accounts",
             return_value=True,
         ), \
         patch("linkedin.notifications.slack.notify_message_received") as notify:
        Command()._run_pass(
            session=SimpleNamespace(campaign=None),
            sender="Athena Aghdami",
            campaign_id=None,
            limit=0,
            dry_run=False,
        )

    notify.assert_called_once()
    assert notify.call_args.kwargs["lead"] == lead
    assert notify.call_args.kwargs["text"] == "Missed you while the daemon was down."
    assert notify.call_args.kwargs["operator"] == "Athena"
    assert notify.call_args.kwargs["thread_external_id"] == "thread-1"


@pytest.mark.django_db
def test_backfill_does_not_renotify_existing_inbound_message():
    campaign = _campaign("Existing inbound test")
    lead = Lead.objects.create(
        first_name="Waylon",
        last_name="Krush",
        public_identifier="waylonkrush",
        linkedin_url="https://www.linkedin.com/in/waylonkrush-existing/",
    )
    Deal.objects.create(lead=lead, campaign=campaign, state=ProfileState.CONNECTED)
    Message.objects.create(
        lead=lead,
        source=Message.Source.LINKEDIN,
        direction=Message.Direction.OUTBOUND,
        external_id="outbound-existing",
        sender="Athena Aghdami",
        body="Sent you a note.",
        sent_at=timezone.now(),
    )
    Message.objects.create(
        lead=lead,
        source=Message.Source.LINKEDIN,
        direction=Message.Direction.INBOUND,
        external_id="inbound-existing",
        sender="waylon krush",
        body="Already notified.",
        sent_at=timezone.now(),
        thread_external_id="thread-existing",
    )

    with patch("linkedin.management.commands.backfill_messages._open_messaging_inbox"), \
         patch(
             "linkedin.management.commands.backfill_messages.get_conversation",
             return_value=[{"entity_urn": "inbound-existing"}],
         ), \
         patch("linkedin.notifications.slack.notify_message_received") as notify:
        Command()._run_pass(
            session=SimpleNamespace(campaign=None),
            sender="Athena Aghdami",
            campaign_id=None,
            limit=0,
            dry_run=False,
        )

    notify.assert_not_called()
