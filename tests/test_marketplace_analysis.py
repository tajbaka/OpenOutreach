from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.utils import timezone

from linkedin.marketplace_analysis import (
    decision_from_mapping,
    group_marketplace_signals_for_alert,
    serialize_signals_for_codex,
    should_notify_marketplace_signal,
)
from linkedin.models import FedRAMPMarketplaceSignal


def _signal(**overrides):
    data = {
        "event_key": "20x_initial:FR1:2026-07-22",
        "source_kind": FedRAMPMarketplaceSignal.SourceKind.CHANGELOG,
        "source_event_id": "event-1",
        "signal_type": FedRAMPMarketplaceSignal.SignalType.TWENTYX_INITIAL,
        "icp_bucket": "20x Pipeline",
        "product_id": "FR1",
        "provider_name": "Acme, Inc.",
        "offering_name": "Acme Cloud",
        "certification_path": "Program",
        "from_status": "",
        "to_status": "Initial Implementation",
        "transition_at": datetime(2026, 7, 22, 9, 0, tzinfo=UTC),
        "recorded_at": datetime(2026, 7, 22, 9, 1, tzinfo=UTC),
        "source_url": "https://example.test/changelog.json",
        "marketplace_url": "https://marketplace.fedramp.gov/products/FR1/",
        "product_context": {},
        "raw_payload": {},
        "first_seen_at": timezone.now(),
        "last_seen_at": timezone.now(),
    }
    data.update(overrides)
    return FedRAMPMarketplaceSignal.objects.create(**data)


@pytest.mark.django_db
def test_codex_queue_explains_20x_and_ready_routing():
    signal = _signal()

    payload = serialize_signals_for_codex([signal])

    assert "Program-path" in payload["instructions"]
    assert "Rev5 Ready" in payload["instructions"]
    assert payload["signals"][0]["id"] == signal.id
    assert payload["signals"][0]["expected_icp_bucket"] == "20x Pipeline"


@pytest.mark.django_db
def test_codex_queue_loads_crm_leads_once(django_assert_num_queries):
    first = _signal()
    second = _signal(
        event_key="20x_initial:FR2:2026-07-22",
        source_event_id="event-2",
        product_id="FR2",
        provider_name="Other Cloud",
    )

    with django_assert_num_queries(1):
        payload = serialize_signals_for_codex([first, second])

    assert len(payload["signals"]) == 2


def test_decision_requires_relevance_and_high_priority_for_alert():
    result = decision_from_mapping({
        "signal_id": 1,
        "is_relevant": True,
        "should_alert": True,
        "priority": "urgent",
        "relevance_reason": "Fresh 20x entrant.",
        "suggested_action": "Research the owner.",
    })
    assert result.should_alert is True

    medium = decision_from_mapping({
        "signal_id": 1,
        "is_relevant": True,
        "should_alert": True,
        "priority": "medium",
    })
    assert medium.should_alert is False


def test_decision_rejects_string_booleans():
    with pytest.raises(ValueError, match="is_relevant must be a boolean"):
        decision_from_mapping({
            "signal_id": 1,
            "is_relevant": "false",
            "should_alert": True,
            "priority": "high",
        })


@pytest.mark.django_db
def test_grouping_keeps_multiple_offerings_for_one_company_together():
    first = _signal()
    second = _signal(
        event_key="20x_initial:FR2:2026-07-22",
        source_event_id="event-2",
        product_id="FR2",
        provider_name="Acme Inc",
        offering_name="Acme Data",
    )

    assert group_marketplace_signals_for_alert([first, second]) == [[first, second]]


@pytest.mark.django_db
def test_analyze_command_exports_unanalyzed_queue(tmp_path):
    signal = _signal()
    output = tmp_path / "review.json"

    call_command("analyze_fedramp_marketplace", output=str(output))

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert [row["id"] for row in payload["signals"]] == [signal.id]


@pytest.mark.django_db
@patch("linkedin.notifications.slack._post_to_slack")
def test_apply_posts_once_to_high_signal_slack(mock_post, monkeypatch, tmp_path):
    from linkedin.notifications import slack

    signal = _signal()
    decisions = tmp_path / "decisions.json"
    decisions.write_text(json.dumps({
        "decisions": [{
            "signal_id": signal.id,
            "is_relevant": True,
            "should_alert": True,
            "priority": "urgent",
            "relevance_reason": "Acme just entered Initial Implementation on the Program path.",
            "suggested_action": "Add Acme to 20x Pipeline and identify the technical owner.",
        }]
    }), encoding="utf-8")
    monkeypatch.setattr(slack, "SLACK_HIGH_SIGNAL_URL", "https://hooks.slack.test/high-signal")

    call_command("analyze_fedramp_marketplace", apply_json=str(decisions))

    signal.refresh_from_db()
    assert signal.slack_notified_at is not None
    assert should_notify_marketplace_signal(signal) is False
    mock_post.assert_called_once()
    args = mock_post.call_args.args
    assert args[0] == "https://hooks.slack.test/high-signal"
    assert "Acme" in args[1]["text"]

    call_command("analyze_fedramp_marketplace", apply_json=str(decisions))
    mock_post.assert_called_once()


@pytest.mark.django_db
@patch("linkedin.notifications.slack._post_to_slack")
def test_no_slack_saves_decision_without_marking_notification(mock_post, tmp_path):
    signal = _signal()
    decisions = tmp_path / "decisions.json"
    decisions.write_text(json.dumps({
        "decisions": [{
            "signal_id": signal.id,
            "is_relevant": True,
            "should_alert": True,
            "priority": "high",
            "relevance_reason": "New external marketplace entrant.",
            "suggested_action": "Research the account.",
        }]
    }), encoding="utf-8")

    call_command(
        "analyze_fedramp_marketplace",
        apply_json=str(decisions),
        no_slack=True,
    )

    signal.refresh_from_db()
    assert signal.analyzed_at is not None
    assert signal.slack_notified_at is None
    mock_post.assert_not_called()
