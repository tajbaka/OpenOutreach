"""Supervisor control alerts are channel-specific and never imply send health."""
from __future__ import annotations

import json
from unittest.mock import Mock

import pytest

from linkedin.notifications import slack


OPS = "https://hooks.slack.test/ops"
REPLIES = "https://hooks.slack.test/replies"


@pytest.fixture
def post(monkeypatch):
    monkeypatch.setattr(slack, "SLACK_WEBHOOK_URL", OPS)
    monkeypatch.setattr(slack, "SLACK_REPLIES_WEBHOOK_URL", REPLIES)
    mock = Mock(return_value=True)
    monkeypatch.setattr(slack, "_post_to_slack", mock)
    return mock


@pytest.mark.parametrize("delivered", [True, False])
def test_restart_uses_ops_only_and_returns_actual_status(post, delivered):
    post.return_value = delivered
    assert slack.notify_supervisor_restart(sender="Arian", detail="Workers relaunched.") is delivered
    post.assert_called_once()
    url, payload, label = post.call_args.args
    assert url == OPS
    assert label == "sender-restart-trigger"
    body = json.dumps(payload)
    assert "Sender restart trigger completed" in body
    assert "Arian" in body
    assert "Workers relaunched." in body
    assert "Worker process launch only" in body
    assert "login and sending health are not confirmed" in body
    assert "commit" not in body.lower()


def test_restart_without_ops_does_not_route_to_replies(post, monkeypatch):
    monkeypatch.setattr(slack, "SLACK_WEBHOOK_URL", "")
    assert slack.notify_supervisor_restart(sender="Arian", detail="Workers relaunched.") is False
    post.assert_not_called()


def test_stop_prefers_replies_and_does_not_duplicate_success(post):
    assert slack.notify_supervisor_stop(sender="Chuka", detail="Owned workers stopped.") is True
    post.assert_called_once()
    url, payload, label = post.call_args.args
    assert url == REPLIES
    assert label == "sender-emergency-stop"
    body = json.dumps(payload)
    assert "Emergency stop activated" in body
    assert "Chuka" in body
    assert "Owned workers stopped." in body
    assert "Supervisor safety shutdown" in body
    assert "Scope: this sender's managed workers" in body
    assert "crash" not in body.lower()
    assert "all senders" not in body.lower()
    assert "commit" not in body.lower()


@pytest.mark.parametrize("delivered", [True, False])
def test_stop_missing_replies_uses_ops_status(post, monkeypatch, delivered):
    monkeypatch.setattr(slack, "SLACK_REPLIES_WEBHOOK_URL", "")
    post.return_value = delivered
    assert slack.notify_supervisor_stop(sender="Arian", detail="Workers stopped.") is delivered
    post.assert_called_once()
    assert post.call_args.args[0] == OPS
    assert post.call_args.args[2] == "sender-emergency-stop-ops-fallback"


@pytest.mark.parametrize("delivered", [True, False])
def test_stop_failed_replies_falls_back_once_and_preserves_payload(post, delivered):
    post.side_effect = [False, delivered]
    assert slack.notify_supervisor_stop(sender="Arian", detail="Workers stopped.") is delivered
    assert post.call_count == 2
    replies_call, ops_call = post.call_args_list
    assert replies_call.args[0] == REPLIES
    assert ops_call.args[0] == OPS
    assert replies_call.args[1] == ops_call.args[1]


def test_stop_no_channels_does_not_post(post, monkeypatch):
    monkeypatch.setattr(slack, "SLACK_REPLIES_WEBHOOK_URL", "")
    monkeypatch.setattr(slack, "SLACK_WEBHOOK_URL", "")
    assert slack.notify_supervisor_stop(sender="Arian", detail="Workers stopped.") is False
    post.assert_not_called()


def test_stop_failed_replies_without_ops_returns_false(post, monkeypatch):
    monkeypatch.setattr(slack, "SLACK_WEBHOOK_URL", "")
    post.return_value = False
    assert slack.notify_supervisor_stop(sender="Arian", detail="Workers stopped.") is False
    post.assert_called_once()
    assert post.call_args.args[0] == REPLIES


def test_stop_identical_failed_urls_not_retried(post, monkeypatch):
    monkeypatch.setattr(slack, "SLACK_REPLIES_WEBHOOK_URL", OPS)
    post.return_value = False
    assert slack.notify_supervisor_stop(sender="Arian", detail="Workers stopped.") is False
    post.assert_called_once()


@pytest.mark.parametrize("notify", [slack.notify_supervisor_restart, slack.notify_supervisor_stop])
def test_control_alert_escapes_sender_and_detail(post, notify):
    notify(sender="Arian <@U123> & team", detail="Trigger <!channel> <https://example.test|link>")
    payload = post.call_args.args[1]
    body = json.dumps(payload)
    assert "<@U123>" not in body
    assert "<!channel>" not in body
    assert "<https://example.test|link>" not in body
    assert "&lt;@U123&gt; &amp; team" in body
    assert "&lt;!channel&gt;" in body


@pytest.mark.parametrize("notify", [slack.notify_supervisor_restart, slack.notify_supervisor_stop])
def test_control_alert_bounds_large_details(post, notify):
    notify(sender="A" * 10000, detail="&" * 10000)
    payload = post.call_args.args[1]
    assert len(payload["text"]) < 300
    assert len(payload["blocks"][1]["text"]["text"]) <= 2900


@pytest.mark.parametrize("notify", [slack.notify_supervisor_restart, slack.notify_supervisor_stop])
def test_control_alert_empty_detail_has_valid_nonblank_section(post, notify):
    notify(sender="Arian", detail="")
    payload = post.call_args.args[1]
    assert payload["blocks"][1]["text"]["text"]
