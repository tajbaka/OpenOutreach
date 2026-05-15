"""Tests for manage.py import_connections.

Per the user's instructions during the autonomous run on 2026-04-27, these
are written but NOT executed in the orchestration session — the user wants
to verify the CSV-to-DB script manually before running these. Run with:
`.venv/bin/pytest tests/test_import_connections.py -v`.
"""
import io
import textwrap
from datetime import date
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command

from crm.models import Deal, Lead, Message
from linkedin.enums import ProfileState
from linkedin.management.commands.import_connections import (
    CsvFormatError,
    CsvRow,
    DedupeDecision,
    apply_match,
    decide_dedupe,
    parse_csv,
)


def _csv(text: str) -> io.StringIO:
    return io.StringIO(textwrap.dedent(text).lstrip())


# ---------------------------------------------------------------------------
# C.1 — CSV parser
# ---------------------------------------------------------------------------


def test_parse_csv_basic():
    rows = list(parse_csv(_csv("""\
        LinkedIn URL,First Name,Message
        https://www.linkedin.com/in/waylonkrush/,Waylon,"Hey Waylon"
        https://www.linkedin.com/in/jane-d/,Jane,"Hi Jane"
    """)))
    assert len(rows) == 2
    assert rows[0].public_id == "waylonkrush"
    assert rows[0].linkedin_url == "https://www.linkedin.com/in/waylonkrush/"
    assert rows[0].first_name == "Waylon"
    assert rows[0].outbound_message == "Hey Waylon"


def test_parse_csv_canonicalizes_url():
    """A URL missing the trailing slash must be stored canonically — the
    daemon's follow_up Deal lookup builds public_id_to_url() (always
    slashed), so a raw non-canonical URL silently breaks follow-ups."""
    rows = list(parse_csv(_csv("""\
        LinkedIn URL,First Name
        https://www.linkedin.com/in/waylonkrush,Waylon
    """)))
    assert rows[0].linkedin_url == "https://www.linkedin.com/in/waylonkrush/"


def test_parse_csv_message_column_optional():
    rows = list(parse_csv(_csv("""\
        LinkedIn URL,First Name
        https://www.linkedin.com/in/waylonkrush/,Waylon
    """)))
    assert rows[0].outbound_message == ""


def test_parse_csv_skips_blank_url():
    rows = list(parse_csv(_csv("""\
        LinkedIn URL,First Name,Message
        ,Nobody,"orphan row"
        https://www.linkedin.com/in/waylonkrush/,Waylon,"hi"
    """)))
    assert len(rows) == 1
    assert rows[0].public_id == "waylonkrush"


def test_parse_csv_raises_when_url_column_missing():
    with pytest.raises(CsvFormatError, match="LinkedIn URL"):
        list(parse_csv(_csv("""\
            First Name,Message
            Waylon,"hi"
        """)))


# ---------------------------------------------------------------------------
# C.2 — Three-way dedupe
# ---------------------------------------------------------------------------


@pytest.fixture
def target_campaign(fake_session):
    """Use the existing FedRampGPT campaign — both Arian's daemon and Chuka's
    CSV imports share the same campaign."""
    return fake_session.campaign


def test_decide_dedupe_url_not_in_db_creates(target_campaign):
    decision, existing = decide_dedupe(
        linkedin_url="https://www.linkedin.com/in/new-person/",
        target_campaign=target_campaign,
    )
    assert decision == DedupeDecision.CREATE
    assert existing is None


def test_decide_dedupe_existing_at_connected_skips(target_campaign):
    lead = Lead.objects.create(
        first_name="W", linkedin_url="https://www.linkedin.com/in/dup-1/",
    )
    Deal.objects.create(
        lead=lead, campaign=target_campaign, state=ProfileState.CONNECTED,
    )
    decision, existing = decide_dedupe(
        linkedin_url=lead.linkedin_url, target_campaign=target_campaign,
    )
    assert decision == DedupeDecision.SKIP
    assert existing is not None


def test_decide_dedupe_existing_at_pending_replaces(target_campaign):
    lead = Lead.objects.create(
        first_name="W", linkedin_url="https://www.linkedin.com/in/dup-2/",
    )
    Deal.objects.create(
        lead=lead, campaign=target_campaign, state=ProfileState.PENDING,
    )
    decision, existing = decide_dedupe(
        linkedin_url=lead.linkedin_url, target_campaign=target_campaign,
    )
    assert decision == DedupeDecision.REPLACE
    assert existing is not None


def test_decide_dedupe_existing_at_qualified_replaces(target_campaign):
    lead = Lead.objects.create(
        first_name="W", linkedin_url="https://www.linkedin.com/in/dup-3/",
    )
    Deal.objects.create(
        lead=lead, campaign=target_campaign, state=ProfileState.QUALIFIED,
    )
    decision, _ = decide_dedupe(
        linkedin_url=lead.linkedin_url, target_campaign=target_campaign,
    )
    assert decision == DedupeDecision.REPLACE


# ---------------------------------------------------------------------------
# C.3 — apply_match
# ---------------------------------------------------------------------------


def test_apply_match_creates_lead_and_deal_at_connected(target_campaign):
    row = CsvRow(
        public_id="waylonkrush",
        linkedin_url="https://www.linkedin.com/in/waylonkrush/",
        first_name="Waylon",
        outbound_message="Hey Waylon, we built FedrampGPT",
    )
    decision = apply_match(row=row, target_campaign=target_campaign)
    assert decision == DedupeDecision.CREATE

    lead = Lead.objects.get(linkedin_url=row.linkedin_url)
    assert lead.first_name == "Waylon"
    assert lead.public_identifier == "waylonkrush"
    deal = Deal.objects.get(lead=lead, campaign=target_campaign)
    assert deal.state == ProfileState.CONNECTED
    assert "FedrampGPT" in deal.sent_note

    # apply_match no longer creates Messages — get_conversation does that during
    # the live run with real Voyager data.
    assert Message.objects.filter(lead=lead).count() == 0


def test_apply_match_promotes_pending_to_connected(target_campaign):
    """Replace-if-ahead: existing PENDING Deal gets promoted with Chuka's invite."""
    lead = Lead.objects.create(
        first_name="W", linkedin_url="https://www.linkedin.com/in/dup-promote/",
    )
    Deal.objects.create(
        lead=lead, campaign=target_campaign, state=ProfileState.PENDING,
        sent_note="Arian's old invite",
        connect_attempts=2, backoff_hours=24,
    )
    row = CsvRow(
        public_id="dup-promote",
        linkedin_url=lead.linkedin_url,
        first_name="W",
        outbound_message="Chuka's invite",
    )
    decision = apply_match(row=row, target_campaign=target_campaign)
    assert decision == DedupeDecision.REPLACE

    deal = Deal.objects.get(lead=lead, campaign=target_campaign)
    assert deal.state == ProfileState.CONNECTED
    assert deal.sent_note == "Chuka's invite"
    assert deal.connect_attempts == 0
    assert deal.backoff_hours == 0


def test_apply_match_skips_when_already_connected(target_campaign):
    lead = Lead.objects.create(
        first_name="W", linkedin_url="https://www.linkedin.com/in/dup-skip/",
    )
    Deal.objects.create(
        lead=lead, campaign=target_campaign, state=ProfileState.CONNECTED,
        sent_note="Arian's original",
    )
    row = CsvRow(
        public_id="dup-skip",
        linkedin_url=lead.linkedin_url,
        first_name="W",
        outbound_message="Chuka's later import",
    )
    decision = apply_match(row=row, target_campaign=target_campaign)
    assert decision == DedupeDecision.SKIP

    # Original sent_note preserved.
    deal = Deal.objects.get(lead=lead, campaign=target_campaign)
    assert deal.sent_note == "Arian's original"


def test_apply_match_is_idempotent(target_campaign):
    row = CsvRow(
        public_id="waylonkrush",
        linkedin_url="https://www.linkedin.com/in/waylonkrush/",
        first_name="Waylon",
        outbound_message="Hey Waylon",
    )
    apply_match(row=row, target_campaign=target_campaign)
    # Second call hits the SKIP path because the first created a CONNECTED Deal.
    decision = apply_match(row=row, target_campaign=target_campaign)
    assert decision == DedupeDecision.SKIP
    assert Lead.objects.filter(linkedin_url=row.linkedin_url).count() == 1
    assert Deal.objects.filter(lead__linkedin_url=row.linkedin_url).count() == 1


# ---------------------------------------------------------------------------
# C.4 — handle() integration
# ---------------------------------------------------------------------------


@patch("linkedin.management.commands.import_connections.get_conversation")
@patch("linkedin.management.commands.import_connections.scrape_connections")
@patch("linkedin.management.commands.import_connections.make_backfill_session")
def test_handle_end_to_end_creates_connected_deal_for_matches(
    mock_make_session, mock_scrape, mock_get_conv, fake_session, tmp_path, monkeypatch,
):
    from linkedin.actions.connections import ConnectionEntry

    monkeypatch.setenv("BACKFILL_LINKEDIN_USERNAME", "backfill@example.com")
    monkeypatch.setenv("BACKFILL_LINKEDIN_PASSWORD", "x")

    csv_path = tmp_path / "batch.csv"
    csv_path.write_text(
        "LinkedIn URL,First Name,Message\n"
        "https://www.linkedin.com/in/waylonkrush/,Waylon,\"Hey Waylon\"\n"
        "https://www.linkedin.com/in/not-yet-connected/,Foo,\"Hey Foo\"\n"
    )

    bf_session = MagicMock()
    bf_session.start.return_value = None
    bf_session.username = "backfill@example.com"
    mock_make_session.return_value = bf_session
    mock_scrape.return_value = [
        ConnectionEntry(
            public_id="waylonkrush",
            name="Waylon Krush",
            connected_on=date(2026, 4, 1),
        ),
    ]
    mock_get_conv.return_value = []

    out = StringIO()
    call_command(
        "import_connections",
        "--csv", str(csv_path),
        "--campaign", str(fake_session.campaign.pk),
        stdout=out,
    )

    deal = Deal.objects.get(lead__linkedin_url="https://www.linkedin.com/in/waylonkrush/")
    assert deal.state == ProfileState.CONNECTED
    assert deal.sent_note == "Hey Waylon"
    assert not Lead.objects.filter(
        linkedin_url="https://www.linkedin.com/in/not-yet-connected/",
    ).exists()
