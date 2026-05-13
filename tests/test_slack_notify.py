"""Tests for the Slack error-notification surface.

Covers `notify_error` (raw helper) and `notify_on_error` (context manager
used by the daemon + management commands). Verifies:

- No-op when SLACK_WEBHOOK_URL is empty (the `_silence_slack` autouse
  fixture in conftest sets that for free).
- POSTs the expected Block Kit shape when the webhook IS set.
- Within-window dedupe collapses repeats of the same crash.
- Different errors / workflows fire separately even within the window.
- `notify_on_error` re-raises Exception while leaving
  KeyboardInterrupt / SystemExit untouched (so Ctrl-C doesn't ping the
  channel).
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from linkedin.notifications import slack as slack_mod


@pytest.fixture(autouse=True)
def _reset_dedupe_state():
    """Each test starts with an empty in-process dedupe cache."""
    slack_mod._RECENT_ERRORS.clear()
    yield
    slack_mod._RECENT_ERRORS.clear()


@pytest.fixture
def slack_url(monkeypatch):
    """Override the `_silence_slack` autouse fixture for tests that need a
    webhook value. Patches both the env var and the imported module-level
    constant so callers see the URL even though imports happened earlier."""
    url = "https://hooks.slack.com/services/T000/B000/test"
    monkeypatch.setenv("SLACK_WEBHOOK_URL", url)
    monkeypatch.setattr("linkedin.conf.SLACK_WEBHOOK_URL", url)
    monkeypatch.setattr(
        "linkedin.notifications.slack.SLACK_WEBHOOK_URL", url,
    )
    return url


def _make_exc():
    """Synthesize an exception with a real traceback so notify_error has
    a `last_frame` to key on (raising and catching is the only way to get
    `exc.__traceback__` populated)."""
    try:
        raise ValueError("boom")
    except ValueError as e:
        return e


def test_notify_error_noop_when_webhook_unset():
    """The autouse fixture clears SLACK_WEBHOOK_URL — calls should be silent."""
    with patch("linkedin.notifications.slack.request.urlopen") as mock_urlopen:
        slack_mod.notify_error("test_workflow", _make_exc())
    mock_urlopen.assert_not_called()


def test_notify_error_posts_block_kit_when_webhook_set(slack_url):
    """Exercises the POST body shape — header, traceback section, context block."""
    with patch("linkedin.notifications.slack.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value.__enter__.return_value.status = 200
        slack_mod.notify_error(
            "test_workflow",
            _make_exc(),
            context={"operator": "Arian", "lead_id": 42},
        )
    assert mock_urlopen.call_count == 1
    req = mock_urlopen.call_args[0][0]
    body = json.loads(req.data.decode("utf-8"))
    assert "test_workflow crashed" in body["text"]
    assert "ValueError" in body["text"]
    # Block layout: summary section, traceback section, context block.
    blocks = body["blocks"]
    assert len(blocks) == 3
    assert blocks[0]["type"] == "section"
    assert "test_workflow crashed" in blocks[0]["text"]["text"]
    assert blocks[1]["type"] == "section"
    assert "ValueError" in blocks[1]["text"]["text"]
    assert blocks[2]["type"] == "context"
    elements = blocks[2]["elements"]
    assert any("Arian" in e["text"] for e in elements)
    assert any("lead_id" in e["text"] for e in elements)


def test_notify_error_dedupes_repeats_within_window(slack_url):
    """Same (workflow, exc_type, last_frame) → first POSTs, second is suppressed."""
    with patch("linkedin.notifications.slack.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value.__enter__.return_value.status = 200
        # Use the same call site so the last_frame is identical across calls.
        for _ in range(5):
            slack_mod.notify_error("dup_workflow", _make_exc())
    assert mock_urlopen.call_count == 1


def test_notify_error_different_workflows_fire_separately(slack_url):
    """Two distinct workflows hitting the same error key should both POST."""
    with patch("linkedin.notifications.slack.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value.__enter__.return_value.status = 200
        slack_mod.notify_error("workflow_a", _make_exc())
        slack_mod.notify_error("workflow_b", _make_exc())
    assert mock_urlopen.call_count == 2


def test_notify_on_error_reraises_exception(slack_url):
    """The context manager must let the underlying exception propagate so
    the process still crashes per CLAUDE.md's "crash on unexpected" rule."""
    with patch("linkedin.notifications.slack.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value.__enter__.return_value.status = 200
        with pytest.raises(RuntimeError, match="planned"):
            with slack_mod.notify_on_error("ctx_workflow"):
                raise RuntimeError("planned")
    assert mock_urlopen.call_count == 1


def test_notify_on_error_passes_through_keyboard_interrupt(slack_url):
    """Ctrl-C must not be wrapped — operator Ctrl-Cs daemon, we don't ping Slack."""
    with patch("linkedin.notifications.slack.request.urlopen") as mock_urlopen:
        with pytest.raises(KeyboardInterrupt):
            with slack_mod.notify_on_error("ctx_workflow"):
                raise KeyboardInterrupt()
    mock_urlopen.assert_not_called()


def test_notify_on_error_passes_context_through(slack_url):
    """Operator + payload context should reach the Slack message body."""
    with patch("linkedin.notifications.slack.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value.__enter__.return_value.status = 200
        with pytest.raises(ValueError):
            with slack_mod.notify_on_error(
                "ctx_workflow",
                context={"operator": "Chuka", "campaign_id": 7},
            ):
                raise ValueError("nope")
    req = mock_urlopen.call_args[0][0]
    body = json.loads(req.data.decode("utf-8"))
    context_block = next(b for b in body["blocks"] if b["type"] == "context")
    elements_text = " ".join(e["text"] for e in context_block["elements"])
    assert "Chuka" in elements_text
    assert "campaign_id" in elements_text
