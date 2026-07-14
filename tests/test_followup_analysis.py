from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from crm.models import Deal, Lead, Message
from linkedin.enums import ProfileState
from linkedin.followup_analysis import (
    apply_followup_decisions,
    followup_decision_from_mapping,
    serialize_followup_queue,
)
from linkedin.models import Campaign, WorkflowRun
from linkedin.notifications import sheets


@pytest.fixture
def lead(db):
    return Lead.objects.create(
        first_name="Jane",
        last_name="Doe",
        company_name="Acme",
        linkedin_url="https://www.linkedin.com/in/jane-doe/",
        email="jane@example.com",
        icp="CSPs",
    )


def _campaign():
    user = User.objects.create_user(username="Arian", email="ariant@boundera.io")
    return Campaign.objects.create(name="Arian Test", user=user)


def _message(lead, *, direction, sender, days_ago, source=Message.Source.LINKEDIN, body=""):
    return Message.objects.create(
        lead=lead,
        source=source,
        direction=direction,
        sender=sender,
        body=body,
        external_id=f"{source}:{direction}:{days_ago}",
        sent_at=timezone.now() - timedelta(days=days_ago),
        thread_external_id="urn:li:conv:abc",
    )


def test_serialize_followup_queue_exports_ball_on_us_candidate(lead):
    campaign = _campaign()
    Deal.objects.create(
        lead=lead,
        campaign=campaign,
        state=ProfileState.CONNECTED,
        connected_at=timezone.now() - timedelta(days=14),
    )
    _message(
        lead,
        direction=Message.Direction.OUTBOUND,
        sender="Arian Taj",
        days_ago=3,
        body="Want me to send a Loom?",
    )
    _message(
        lead,
        direction=Message.Direction.INBOUND,
        sender="Jane Doe",
        days_ago=1,
        body="Yes, send it over.",
    )

    payload = serialize_followup_queue(
        operators=["Arian"],
        read_sheet=False,
    )

    assert len(payload["candidates"]) == 1
    candidate = payload["candidates"][0]
    assert candidate["operator"] == "Arian"
    assert candidate["classification"] == "ball_on_us"
    assert candidate["sheet_row"][sheets.FU_COL_STATE] == sheets.STATE_BALL_ON_US
    assert candidate["sheet_row"][sheets.FU_COL_ROLE] == "CSP"
    assert candidate["messages"][-1]["body"] == "Yes, send it over."


def test_followup_decision_validates_state_and_priority():
    with pytest.raises(ValueError):
        followup_decision_from_mapping({
            "lead_id": 1,
            "operator": "Arian",
            "state": "Met",
            "priority": "HIGH",
        })
    with pytest.raises(ValueError):
        followup_decision_from_mapping({
            "lead_id": 1,
            "operator": "Arian",
            "state": sheets.STATE_BALL_ON_US,
            "priority": "URGENT",
        })


def test_apply_followup_decisions_writes_rows_and_records_workflow(lead, monkeypatch):
    campaign = _campaign()
    Deal.objects.create(
        lead=lead,
        campaign=campaign,
        state=ProfileState.CONNECTED,
        connected_at=timezone.now() - timedelta(days=10),
    )
    _message(
        lead,
        direction=Message.Direction.OUTBOUND,
        sender="Arian Taj",
        days_ago=9,
        body="Initial note",
    )
    _message(
        lead,
        direction=Message.Direction.INBOUND,
        sender="Jane Doe",
        days_ago=8,
        body="Can you send context?",
    )

    captured = {}

    def fake_write(rows_by_operator):
        captured.update(rows_by_operator)
        return {op: len(rows) for op, rows in rows_by_operator.items()}

    monkeypatch.setattr("linkedin.followup_analysis.write_followups", fake_write)
    decision = followup_decision_from_mapping({
        "lead_id": lead.pk,
        "operator": "Arian",
        "status": "Replied",
        "state": sheets.STATE_BALL_ON_US,
        "role": "CSP",
        "priority": "HIGH",
        "convo": "Jane asked for more context after the intro.",
        "draft_linkedin": "Jane, sending the context here.",
        "draft_email": "",
    })

    counts = apply_followup_decisions([decision])

    assert counts == {"Arian": 1}
    row = captured["Arian"][0]
    assert row[sheets.FU_COL_NAME] == "Jane Doe"
    assert row[sheets.FU_COL_CONVO] == "Jane asked for more context after the intro."
    assert row[sheets.FU_COL_DRAFT_LINKEDIN] == "Jane, sending the context here."
    assert WorkflowRun.objects.filter(name="followup", operator="").exists()
