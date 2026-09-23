from datetime import datetime, timedelta, timezone
from itertools import count

import pytest

from crm.models import Message, SalesOwner
from linkedin.linkedin_pending import (
    _message_excerpt,
    build_linkedin_pending,
    needs_linkedin_reply,
)
from tests.factories import LeadFactory


pytestmark = pytest.mark.django_db
NOW = datetime(2026, 9, 22, 16, tzinfo=timezone.utc)
IDS = count()


def message(
    lead,
    *,
    direction,
    body,
    offset_days=0,
    offset_minutes=0,
    thread="thread-1",
    operator=None,
    sender="",
):
    owner = SalesOwner.objects.get_or_create(handle=operator)[0] if operator else None
    return Message.objects.create(
        lead=lead,
        operator=owner,
        source=Message.Source.LINKEDIN,
        external_id=f"linkedin-pending-{next(IDS)}",
        thread_external_id=thread,
        direction=direction,
        sender=sender,
        body=body,
        sent_at=NOW + timedelta(days=offset_days, minutes=offset_minutes),
    )


@pytest.mark.parametrize("body", [
    "Thanks",
    "Thank you Arian, have a great day!",
    "Bye",
    "Sounds good",
    "Perfect, talk soon",
    "Thanks Eddy happy to connect",
    "I'm happy to connect",
    "I finished reviewing the document.",
])
def test_acknowledgements_and_statements_do_not_require_reply(body):
    assert needs_linkedin_reply(body) is False


@pytest.mark.parametrize("body", [
    "Could you send the pricing proposal?",
    "Thank you — could you share the deck?",
    "I'm interested in a demo",
    "Let's schedule a call next week",
    "What are the next steps?",
])
def test_questions_requests_and_interest_require_reply(body):
    assert needs_linkedin_reply(body) is True


def test_long_message_excerpt_keeps_actionable_ending():
    body = "Context " * 200 + "Could you introduce me to the right person?"
    excerpt = _message_excerpt(body, limit=120)
    assert len(excerpt) == 120
    assert " … " in excerpt
    assert excerpt.endswith("Could you introduce me to the right person?")


def test_exact_thread_reconciliation_filters_ack_and_clears_after_outbound():
    pending_lead = LeadFactory(
        first_name="Pending",
        last_name="Person",
        company_name="Example",
        linkedin_url="https://www.linkedin.com/in/pending-person/",
    )
    message(
        pending_lead,
        direction=Message.Direction.OUTBOUND,
        body="Initial note",
        offset_days=-5,
        operator="Arian",
    )
    first = message(
        pending_lead,
        direction=Message.Direction.INBOUND,
        body="Could you send the pricing proposal?",
        offset_days=-3,
    )

    ack_lead = LeadFactory()
    message(
        ack_lead,
        direction=Message.Direction.OUTBOUND,
        body="Great speaking with you",
        offset_days=-3,
        operator="Arian",
        thread="ack-thread",
    )
    message(
        ack_lead,
        direction=Message.Direction.INBOUND,
        body="Thank you Arian, have a great day!",
        offset_days=-2,
        thread="ack-thread",
    )

    replied_lead = LeadFactory()
    message(
        replied_lead,
        direction=Message.Direction.OUTBOUND,
        body="Hello",
        offset_days=-5,
        operator="Arian",
        thread="replied-thread",
    )
    message(
        replied_lead,
        direction=Message.Direction.INBOUND,
        body="Can you share a demo?",
        offset_days=-4,
        thread="replied-thread",
    )
    message(
        replied_lead,
        direction=Message.Direction.OUTBOUND,
        body="Here is the demo link",
        offset_days=-3,
        operator="Arian",
        thread="replied-thread",
    )

    rows, counts = build_linkedin_pending(now=NOW)

    assert len(rows) == 1
    assert rows[0].lead_id == pending_lead.id
    assert rows[0].operator == "Arian"
    assert rows[0].message_external_id == first.external_id
    assert rows[0].sheet_values(now=NOW)[6] == 3
    assert counts["acknowledgements_filtered"] == 1
    assert counts["replied_threads"] == 1


def test_later_actionable_inbound_reopens_same_stable_thread_row():
    lead = LeadFactory()
    message(
        lead,
        direction=Message.Direction.OUTBOUND,
        body="Opening note",
        offset_days=-8,
        operator="Arian",
    )
    message(
        lead,
        direction=Message.Direction.INBOUND,
        body="Can you send the overview?",
        offset_days=-7,
    )
    message(
        lead,
        direction=Message.Direction.OUTBOUND,
        body="Sent",
        offset_days=-6,
        operator="Arian",
    )
    latest = message(
        lead,
        direction=Message.Direction.INBOUND,
        body="Could you also share pricing?",
        offset_days=-1,
    )

    rows, _counts = build_linkedin_pending(now=NOW)

    assert len(rows) == 1
    assert rows[0].message_external_id == latest.external_id
    stable_id = rows[0].pending_id
    assert stable_id == build_linkedin_pending(now=NOW)[0][0].pending_id


def test_decline_blank_thread_ambiguous_owner_and_old_messages_fail_closed():
    declined = LeadFactory()
    message(
        declined,
        direction=Message.Direction.OUTBOUND,
        body="Hello",
        offset_days=-4,
        operator="Arian",
        thread="declined-thread",
    )
    message(
        declined,
        direction=Message.Direction.INBOUND,
        body="Can we meet?",
        offset_days=-3,
        thread="declined-thread",
    )
    message(
        declined,
        direction=Message.Direction.INBOUND,
        body="No thanks, this is not a fit",
        offset_days=-2,
        thread="declined-thread",
    )

    blank = LeadFactory()
    message(
        blank,
        direction=Message.Direction.INBOUND,
        body="Can you send pricing?",
        offset_days=-1,
        thread="",
        operator="Arian",
    )

    ambiguous = LeadFactory()
    message(
        ambiguous,
        direction=Message.Direction.OUTBOUND,
        body="Arian note",
        offset_days=-3,
        operator="Arian",
        thread="ambiguous-thread",
    )
    message(
        ambiguous,
        direction=Message.Direction.OUTBOUND,
        body="Chuka note",
        offset_days=-2,
        operator="Chuka",
        thread="ambiguous-thread",
    )
    message(
        ambiguous,
        direction=Message.Direction.INBOUND,
        body="Could you send the deck?",
        offset_days=-1,
        thread="ambiguous-thread",
    )

    old = LeadFactory()
    message(
        old,
        direction=Message.Direction.INBOUND,
        body="Can you send pricing?",
        offset_days=-15,
        thread="old-thread",
        operator="Arian",
    )

    rows, counts = build_linkedin_pending(now=NOW)

    assert rows == []
    assert counts["blank_thread_held"] == 1
    assert counts["ambiguous_threads"] == 1
    assert counts["declines_filtered"] == 1
