"""Tests for the sync_attio synthesis pass (D1 + D2).

Per the user's instructions during the autonomous run on 2026-04-27, these
are written but NOT executed in the orchestration session — the user wants
to verify the Attio cron synthesis flow manually before running these. Run
with: `.venv/bin/pytest tests/test_synthesis.py -v`.
"""
from datetime import datetime, timezone as _tz
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command

from crm.models import Deal, Lead, Message
from linkedin.enums import ProfileState


# ---------------------------------------------------------------------------
# D.1 — model fields
# ---------------------------------------------------------------------------


def test_lead_has_email_field(db):
    lead = Lead.objects.create(
        first_name="A", linkedin_url="https://www.linkedin.com/in/a-1/",
        email="a@example.com",
    )
    assert lead.email == "a@example.com"


def test_lead_email_defaults_to_blank(db):
    lead = Lead.objects.create(
        first_name="A", linkedin_url="https://www.linkedin.com/in/a-2/",
    )
    assert lead.email == ""


def test_deal_has_synthesis_tracking_fields(fake_session):
    lead = Lead.objects.create(
        first_name="A", linkedin_url="https://www.linkedin.com/in/a-3/",
    )
    deal = Deal.objects.create(lead=lead, campaign=fake_session.campaign)
    assert deal.last_synthesized_at is None
    assert deal.wants_meeting_detected_at is None
    deal.last_synthesized_at = datetime(2026, 4, 27, tzinfo=_tz.utc)
    deal.wants_meeting_detected_at = datetime(2026, 4, 27, tzinfo=_tz.utc)
    deal.save()
    deal.refresh_from_db()
    assert deal.last_synthesized_at is not None
    assert deal.wants_meeting_detected_at is not None


# ---------------------------------------------------------------------------
# D.2 — extract_email_from_messages
# ---------------------------------------------------------------------------


def _msg(lead, *, body, direction, sent_at):
    return Message.objects.create(
        lead=lead,
        source=Message.Source.LINKEDIN,
        external_id=f"urn:li:msg:{body[:10]}_{direction}_{sent_at.isoformat()}",
        direction=direction,
        body=body,
        sent_at=sent_at,
    )


def test_extract_email_returns_first_inbound_email(db):
    from linkedin.notifications.synthesis import extract_email_from_messages

    lead = Lead.objects.create(
        first_name="A", linkedin_url="https://www.linkedin.com/in/a-em-1/",
    )
    _msg(lead, body="Hey, you can reach me at jane@example.com",
         direction=Message.Direction.INBOUND,
         sent_at=datetime(2026, 4, 1, tzinfo=_tz.utc))
    _msg(lead, body="Or also janedoe@gmail.com",
         direction=Message.Direction.INBOUND,
         sent_at=datetime(2026, 4, 2, tzinfo=_tz.utc))
    assert extract_email_from_messages(lead.messages.all()) == "jane@example.com"


def test_extract_email_ignores_outbound(db):
    """We sent 'reach me at us@ours.com' — shouldn't be extracted as the lead's email."""
    from linkedin.notifications.synthesis import extract_email_from_messages

    lead = Lead.objects.create(
        first_name="A", linkedin_url="https://www.linkedin.com/in/a-em-2/",
    )
    _msg(lead, body="Reach me at us@ours.com",
         direction=Message.Direction.OUTBOUND,
         sent_at=datetime(2026, 4, 1, tzinfo=_tz.utc))
    _msg(lead, body="OK noted",
         direction=Message.Direction.INBOUND,
         sent_at=datetime(2026, 4, 2, tzinfo=_tz.utc))
    assert extract_email_from_messages(lead.messages.all()) == ""


def test_extract_email_returns_empty_when_none_found(db):
    from linkedin.notifications.synthesis import extract_email_from_messages

    lead = Lead.objects.create(
        first_name="A", linkedin_url="https://www.linkedin.com/in/a-em-3/",
    )
    _msg(lead, body="just text no email",
         direction=Message.Direction.INBOUND,
         sent_at=datetime(2026, 4, 1, tzinfo=_tz.utc))
    assert extract_email_from_messages(lead.messages.all()) == ""


# ---------------------------------------------------------------------------
# D.4 — detect_wants_meeting
# ---------------------------------------------------------------------------


@patch("linkedin.notifications.synthesis._build_llm")
def test_detect_wants_meeting_true_with_quote(mock_build, db):
    from linkedin.notifications.synthesis import detect_wants_meeting

    fake_llm = MagicMock()
    fake_llm.invoke.return_value = MagicMock(
        wants_meeting=True, reason='"send me a calendar link"',
    )
    mock_build.return_value = fake_llm

    lead = Lead.objects.create(
        first_name="A", linkedin_url="https://www.linkedin.com/in/a-w-1/",
    )
    msgs = [
        _msg(lead, body="Hey", direction=Message.Direction.OUTBOUND,
             sent_at=datetime(2026, 4, 1, tzinfo=_tz.utc)),
        _msg(lead, body="send me a calendar link", direction=Message.Direction.INBOUND,
             sent_at=datetime(2026, 4, 2, tzinfo=_tz.utc)),
    ]
    result = detect_wants_meeting(msgs)
    assert result.wants_meeting is True
    assert "calendar link" in result.reason


@patch("linkedin.notifications.synthesis._build_llm")
def test_detect_wants_meeting_false_when_no_signal(mock_build, db):
    from linkedin.notifications.synthesis import detect_wants_meeting

    fake_llm = MagicMock()
    fake_llm.invoke.return_value = MagicMock(
        wants_meeting=False, reason="no clear signal",
    )
    mock_build.return_value = fake_llm

    lead = Lead.objects.create(
        first_name="A", linkedin_url="https://www.linkedin.com/in/a-w-2/",
    )
    msgs = [
        _msg(lead, body="not interested", direction=Message.Direction.INBOUND,
             sent_at=datetime(2026, 4, 1, tzinfo=_tz.utc)),
    ]
    result = detect_wants_meeting(msgs)
    assert result.wants_meeting is False


# ---------------------------------------------------------------------------
# D.5 — synthesize_for_deal orchestrator
# ---------------------------------------------------------------------------


@patch("linkedin.notifications.synthesis.detect_wants_meeting")
@patch("linkedin.notifications.synthesis.add_person_email")
@patch("linkedin.notifications.synthesis.create_person_note")
@patch("linkedin.notifications.synthesis.set_person_outreach_status")
@patch("linkedin.notifications.synthesis.get_person_outreach_status")
def test_synthesize_extracts_email_and_flags_wants_meeting(
    mock_get_status, mock_set_status, mock_create_note,
    mock_add_email, mock_detect, fake_session,
):
    from linkedin.notifications import attio
    from linkedin.notifications.synthesis import synthesize_for_deal

    lead = Lead.objects.create(
        first_name="A", linkedin_url="https://www.linkedin.com/in/a-syn-1/",
        attio_person_id="rec_attio_1",
    )
    deal = Deal.objects.create(
        lead=lead, campaign=fake_session.campaign,
        state=ProfileState.CONNECTED,
        last_reply_at=datetime(2026, 4, 2, tzinfo=_tz.utc),
    )
    _msg(lead, body="reach me at jane@example.com — happy to chat",
         direction=Message.Direction.INBOUND,
         sent_at=datetime(2026, 4, 2, 10, 0, tzinfo=_tz.utc))

    mock_get_status.return_value = attio.STATUS_REPLIED
    mock_detect.return_value = MagicMock(
        wants_meeting=True, reason='"happy to chat"',
    )

    synthesize_for_deal(deal)

    lead.refresh_from_db()
    deal.refresh_from_db()
    assert lead.email == "jane@example.com"
    mock_add_email.assert_called_once_with("rec_attio_1", "jane@example.com")
    mock_set_status.assert_called_once_with("rec_attio_1", attio.STATUS_WANTS_MEETING)
    mock_create_note.assert_called_once()
    assert deal.wants_meeting_detected_at is not None
    assert deal.last_synthesized_at is not None


@patch("linkedin.notifications.synthesis.detect_wants_meeting")
def test_synthesize_skips_llm_when_already_detected(mock_detect, fake_session):
    from linkedin.notifications.synthesis import synthesize_for_deal

    lead = Lead.objects.create(
        first_name="A", linkedin_url="https://www.linkedin.com/in/a-syn-2/",
        email="x@y.com",
    )
    deal = Deal.objects.create(
        lead=lead, campaign=fake_session.campaign,
        state=ProfileState.CONNECTED,
        wants_meeting_detected_at=datetime(2026, 4, 1, tzinfo=_tz.utc),
    )
    _msg(lead, body="I want to meet", direction=Message.Direction.INBOUND,
         sent_at=datetime(2026, 4, 5, tzinfo=_tz.utc))

    synthesize_for_deal(deal)
    mock_detect.assert_not_called()


@patch("linkedin.notifications.synthesis.detect_wants_meeting")
def test_synthesize_skips_llm_when_no_new_messages_since_last_run(mock_detect, fake_session):
    from linkedin.notifications.synthesis import synthesize_for_deal

    lead = Lead.objects.create(
        first_name="A", linkedin_url="https://www.linkedin.com/in/a-syn-3/",
    )
    deal = Deal.objects.create(
        lead=lead, campaign=fake_session.campaign,
        state=ProfileState.CONNECTED,
        last_synthesized_at=datetime(2026, 4, 10, tzinfo=_tz.utc),
    )
    _msg(lead, body="stale", direction=Message.Direction.INBOUND,
         sent_at=datetime(2026, 4, 1, tzinfo=_tz.utc))

    synthesize_for_deal(deal)
    mock_detect.assert_not_called()


# ---------------------------------------------------------------------------
# D.6 — sync_attio integration
# ---------------------------------------------------------------------------


@patch("linkedin.notifications.synthesis.synthesize_for_deal")
def test_sync_attio_calls_synthesize_for_deal_per_deal(mock_synth, fake_session, monkeypatch):
    from linkedin.notifications import attio as attio_mod
    monkeypatch.setattr(attio_mod, "create_company", lambda *a, **kw: "rec_co")
    monkeypatch.setattr(attio_mod, "create_person", lambda *a, **kw: "rec_p")
    monkeypatch.setattr(attio_mod, "set_person_outreach_status", lambda *a, **kw: None)
    monkeypatch.setattr(attio_mod, "get_person_outreach_status", lambda *a, **kw: "")
    monkeypatch.setattr(attio_mod, "create_sales_entry", lambda *a, **kw: "rec_e")
    monkeypatch.setattr(attio_mod, "get_sales_entry_state", lambda *a, **kw: {"stage": "", "mpoc_id": ""})
    monkeypatch.setattr(attio_mod, "patch_sales_entry_stage", lambda *a, **kw: None)
    monkeypatch.setattr(attio_mod, "patch_sales_entry_mpoc", lambda *a, **kw: None)

    lead = Lead.objects.create(
        first_name="A", company_name="Acme",
        linkedin_url="https://www.linkedin.com/in/a-syn-int-1/",
    )
    Deal.objects.create(
        lead=lead, campaign=fake_session.campaign,
        state=ProfileState.CONNECTED,
    )

    call_command("sync_attio", "--campaign", str(fake_session.campaign.pk), stdout=StringIO())

    assert mock_synth.call_count >= 1
