"""Tests for the sync_sheets synthesis pass (D1 + D2).

Run with: `.venv/bin/pytest tests/test_synthesis.py -v`.
"""
from datetime import datetime, timezone as _tz
from unittest.mock import MagicMock, patch

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
# D.5 — synthesize_for_deal orchestrator (Sheets-flavored: returns SynthResult)
# ---------------------------------------------------------------------------


@patch("linkedin.notifications.synthesis.detect_wants_meeting")
def test_synthesize_extracts_email_and_returns_wants_meeting_result(mock_detect, fake_session):
    from linkedin.notifications import sheets
    from linkedin.notifications.synthesis import synthesize_for_deal

    lead = Lead.objects.create(
        first_name="A", linkedin_url="https://www.linkedin.com/in/a-syn-1/",
    )
    deal = Deal.objects.create(
        lead=lead, campaign=fake_session.campaign,
        state=ProfileState.CONNECTED,
        last_reply_at=datetime(2026, 4, 2, tzinfo=_tz.utc),
    )
    _msg(lead, body="reach me at jane@example.com — happy to chat",
         direction=Message.Direction.INBOUND,
         sent_at=datetime(2026, 4, 2, 10, 0, tzinfo=_tz.utc))

    mock_detect.return_value = MagicMock(
        wants_meeting=True, reason='"happy to chat"',
    )

    # Caller passes the current sheet-cell status. "Replied" is below
    # Wants Meeting in rank, so the LLM should fire.
    result = synthesize_for_deal(deal, current_outreach_status=sheets.STATUS_REPLIED)

    lead.refresh_from_db()
    deal.refresh_from_db()
    assert lead.email == "jane@example.com"  # D1 mutated Lead.email
    assert result is not None
    assert result.wants_meeting_now is True
    assert deal.wants_meeting_detected_at is not None
    assert deal.last_synthesized_at is not None


@patch("linkedin.notifications.synthesis.detect_wants_meeting")
def test_synthesize_skips_d2_when_sheet_status_already_higher(mock_detect, fake_session):
    """If the sheet says Meeting Booked, don't run the LLM."""
    from linkedin.notifications import sheets
    from linkedin.notifications.synthesis import synthesize_for_deal

    lead = Lead.objects.create(
        first_name="A", linkedin_url="https://www.linkedin.com/in/a-syn-skip-rank/",
        email="x@y.com",
    )
    deal = Deal.objects.create(
        lead=lead, campaign=fake_session.campaign,
        state=ProfileState.CONNECTED,
    )
    _msg(lead, body="anything", direction=Message.Direction.INBOUND,
         sent_at=datetime(2026, 4, 5, tzinfo=_tz.utc))

    result = synthesize_for_deal(
        deal, current_outreach_status=sheets.STATUS_MEETING_BOOKED,
    )
    mock_detect.assert_not_called()
    assert result is not None
    assert result.wants_meeting_now is False


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

    synthesize_for_deal(deal, current_outreach_status="")
    mock_detect.assert_not_called()


@patch("linkedin.notifications.synthesis.detect_wants_meeting")
def test_synthesize_returns_none_when_no_new_messages_since_last_run(mock_detect, fake_session):
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

    result = synthesize_for_deal(deal, current_outreach_status="")
    mock_detect.assert_not_called()
    assert result is None
