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
from datetime import datetime, timezone
from types import SimpleNamespace
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
    constants so callers see the URL even though imports happened earlier.

    Sets both the ops webhook (SLACK_WEBHOOK_URL — errors, connection
    accepts) and the replies webhook (SLACK_REPLIES_WEBHOOK_URL — inbound
    messages, phone enrichment) to the same test URL."""
    url = "https://hooks.slack.com/services/T000/B000/test"
    monkeypatch.setenv("SLACK_WEBHOOK_URL", url)
    monkeypatch.setenv("SLACK_REPLIES_WEBHOOK_URL", url)
    monkeypatch.setattr("linkedin.conf.SLACK_WEBHOOK_URL", url)
    monkeypatch.setattr("linkedin.conf.SLACK_REPLIES_WEBHOOK_URL", url)
    monkeypatch.setattr(
        "linkedin.notifications.slack.SLACK_WEBHOOK_URL", url,
    )
    monkeypatch.setattr(
        "linkedin.notifications.slack.SLACK_REPLIES_WEBHOOK_URL", url,
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


def test_marketplace_alert_includes_official_listing_details(monkeypatch):
    monkeypatch.setattr(
        slack_mod,
        "SLACK_HIGH_SIGNAL_URL",
        "https://hooks.slack.test/marketplace",
    )
    signal = SimpleNamespace(
        priority="urgent",
        recorded_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
        first_seen_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
        transition_at=None,
        provider_name="Acme Cloud",
        offering_name="Acme Secure Cloud",
        product_id="FR1234",
        marketplace_url="https://marketplace.fedramp.gov/products/FR1234/",
        signal_type="20x_initial",
        icp_bucket="20x Pipeline",
        certification_path="Program",
        from_status="In Process",
        to_status="Initial Implementation",
        relevance_reason="New Program-path entrant.",
        suggested_action="Research the compliance owner.",
        source_url="https://example.test/fedramp.json",
        product_context={
            "website": "https://acme.example/",
            "partnering_agency": "GSA",
            "impact_level": "Moderate",
            "auth_type": "Agency",
            "small_business": True,
            "sales_email": "fedramp@acme.example",
        },
    )

    with patch("linkedin.notifications.slack.request.urlopen") as mock_open:
        mock_open.return_value.__enter__.return_value.status = 200
        assert slack_mod.notify_marketplace_signal_group(signals=[signal]) is True

    sent = json.loads(mock_open.call_args[0][0].data.decode("utf-8"))
    body = json.dumps(sent)
    assert "Official listing details" in body
    assert "https://acme.example/" in body
    assert "GSA" in body
    assert "Moderate" in body
    assert "Agency" in body
    assert "Small business" in body
    assert "fedramp@acme.example" in body


def test_notify_manual_reply_sent_updates_original_slack_message(monkeypatch):
    monkeypatch.setattr(slack_mod, "SLACK_BOT_TOKEN", "xoxb-test")
    payload = {
        "slack_channel_id": "C123",
        "slack_message_ts": "171234.567",
        "slack_blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": "inbound"}},
            {"type": "actions", "elements": []},
        ],
        "slack_response_url": "https://hooks.slack.com/actions/T/B/R",
    }
    with patch("linkedin.notifications.slack.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value.__enter__.return_value.read.return_value = b'{"ok": true}'
        slack_mod.notify_manual_reply_sent(payload, lead_name="Alice Manual")

    req = mock_urlopen.call_args[0][0]
    assert req.full_url == "https://slack.com/api/chat.update"
    assert req.headers["Authorization"] == "Bearer xoxb-test"
    sent = json.loads(req.data.decode("utf-8"))
    assert sent["channel"] == "C123"
    assert sent["ts"] == "171234.567"
    statuses = [
        block for block in sent["blocks"]
        if block.get("block_id", "").startswith("reply_status:")
    ]
    assert len(statuses) == 1
    assert "LinkedIn reply sent" in statuses[0]["text"]["text"]


def test_notify_manual_reply_failed_falls_back_to_response_url(monkeypatch):
    monkeypatch.setattr(slack_mod, "SLACK_BOT_TOKEN", "")
    payload = {
        "slack_response_url": "https://hooks.slack.com/actions/T/B/R",
    }
    with patch("linkedin.notifications.slack.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value.__enter__.return_value.read.return_value = b"ok"
        slack_mod.notify_manual_reply_failed(payload, "LinkedIn send failed for lead 42")

    req = mock_urlopen.call_args[0][0]
    assert req.full_url == "https://hooks.slack.com/actions/T/B/R"
    sent = json.loads(req.data.decode("utf-8"))
    assert sent["replace_original"] is False
    assert "LinkedIn reply failed" in sent["text"]


def _feed_source_blocks():
    return [
        {
            "type": "section",
            "block_id": "feed_post_body",
            "text": {"type": "mrkdwn", "text": "Evidence quality matters."},
        },
        {
            "type": "actions",
            "block_id": "feed_post_actions",
            "elements": [],
        },
    ]


def test_notify_feed_comment_sent_updates_original_alert_via_response_url():
    payload = {
        "slack_channel_id": "C123",
        "slack_message_ts": "171234.567",
        "slack_response_url": "https://hooks.slack.com/actions/T/B/R",
        "slack_blocks": _feed_source_blocks(),
    }
    with patch("linkedin.notifications.slack.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value.__enter__.return_value.read.return_value = b"ok"
        slack_mod.notify_feed_comment_sent(
            payload,
            post_label="Ada Lovelace",
            like_result="liked",
        )

    req = mock_urlopen.call_args[0][0]
    assert req.full_url == "https://hooks.slack.com/actions/T/B/R"
    sent = json.loads(req.data.decode("utf-8"))
    assert sent["replace_original"] is True
    assert sent["blocks"][0] == _feed_source_blocks()[0]
    assert sent["blocks"][-1] == _feed_source_blocks()[-1]
    assert sent["blocks"][-2]["block_id"] == "feed_comment_status:sent"
    assert "LinkedIn feed comment posted" in sent["text"]
    assert "Post liked" in sent["blocks"][-2]["text"]["text"]


def test_notify_feed_comment_sent_falls_back_to_original_chat_update():
    payload = {
        "slack_channel_id": "C123",
        "slack_message_ts": "171234.567",
        "slack_blocks": _feed_source_blocks(),
    }
    with (
        patch(
            "linkedin.notifications.slack._post_slack_response_url",
            return_value=False,
        ),
        patch("linkedin.notifications.slack._slack_api", return_value=True) as slack_api,
    ):
        slack_mod.notify_feed_comment_sent(payload, post_label="Ada Lovelace")

    method, sent, _label = slack_api.call_args.args
    assert method == "chat.update"
    assert sent["channel"] == "C123"
    assert sent["ts"] == "171234.567"
    assert sent["blocks"][-2]["block_id"] == "feed_comment_status:sent"


def test_notify_feed_comment_uncertain_falls_back_to_response_url(monkeypatch):
    monkeypatch.setattr(slack_mod, "SLACK_BOT_TOKEN", "")
    payload = {
        "slack_response_url": "https://hooks.slack.com/actions/T/B/R",
        "slack_blocks": _feed_source_blocks(),
    }
    with patch("linkedin.notifications.slack.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value.__enter__.return_value.read.return_value = b"ok"
        slack_mod.notify_feed_comment_uncertain(payload, "Could not verify comment")

    req = mock_urlopen.call_args[0][0]
    assert req.full_url == "https://hooks.slack.com/actions/T/B/R"
    sent = json.loads(req.data.decode("utf-8"))
    assert sent["replace_original"] is True
    assert "manual verification" in sent["text"]
    assert sent["blocks"][-2]["block_id"] == "feed_comment_status:uncertain"


def test_notify_feed_like_complete_updates_original_alert():
    payload = {
        "slack_response_url": "https://hooks.slack.com/actions/T/B/R",
        "slack_blocks": _feed_source_blocks(),
    }
    with patch("linkedin.notifications.slack.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value.__enter__.return_value.read.return_value = b"ok"
        slack_mod.notify_feed_like_complete(
            payload,
            result="already_liked",
            post_label="Ada Lovelace",
        )

    req = mock_urlopen.call_args[0][0]
    assert req.full_url == "https://hooks.slack.com/actions/T/B/R"
    sent = json.loads(req.data.decode("utf-8"))
    assert sent["replace_original"] is True
    assert sent["blocks"][-2]["block_id"] == "feed_like_status:already_liked"
    assert "already liked" in sent["blocks"][-2]["text"]["text"]


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


def test_connection_accepted_without_reply_does_not_post(slack_url):
    with patch("linkedin.notifications.slack.request.urlopen") as mock_urlopen:
        slack_mod.notify_connection_accepted(
            full_name="Plain Accept",
            title="CISO",
            company="Acme",
            profile_url="https://www.linkedin.com/in/plain/",
            campaign_name="FedRampGPT",
            reply_text=None,
            operator="Leili",
        )

    mock_urlopen.assert_not_called()


def test_connection_accepted_with_reply_still_posts(slack_url):
    with patch("linkedin.notifications.slack.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value.__enter__.return_value.status = 200
        slack_mod.notify_connection_accepted(
            full_name="Reply Accept",
            title="CISO",
            company="Acme",
            profile_url="https://www.linkedin.com/in/reply/",
            campaign_name="FedRampGPT",
            reply_text="Happy to talk.",
            operator="Leili",
        )

    req = mock_urlopen.call_args[0][0]
    sent = json.loads(req.data.decode("utf-8"))
    assert "accepted and replied" in sent["text"]
    assert "Happy to talk." in sent["blocks"][1]["text"]["text"]


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


class TestNotifyMessageReceived:
    def test_noop_when_webhook_unset(self, db):
        """conftest._silence_slack clears the webhook — must not POST."""
        from crm.models import Lead
        lead = Lead.objects.create(
            first_name="Waylon", last_name="Krush",
            linkedin_url="https://www.linkedin.com/in/waylonkrush/",
        )
        with patch("linkedin.notifications.slack.request.urlopen") as mock_open:
            slack_mod.notify_message_received(
                lead=lead,
                text="hello there",
                operator="Arian",
                thread_external_id="thread-arian",
            )
        mock_open.assert_not_called()

    def test_posts_block_kit_when_webhook_set(self, db, slack_url):
        from crm.models import Lead
        lead = Lead.objects.create(
            first_name="Waylon", last_name="Krush",
            linkedin_url="https://www.linkedin.com/in/waylonkrush/",
        )
        with patch("linkedin.notifications.slack.request.urlopen") as mock_open:
            mock_open.return_value.__enter__.return_value.status = 200
            slack_mod.notify_message_received(
                lead=lead,
                text="hello there",
                operator="Arian",
                thread_external_id="thread-arian",
            )
        mock_open.assert_called_once()
        sent = json.loads(mock_open.call_args[0][0].data.decode("utf-8"))
        assert "blocks" in sent
        assert "Waylon Krush" in sent["text"]
        body = json.dumps(sent)
        assert "hello there" in body
        assert "waylonkrush" in body  # profile link

        blocks = sent["blocks"]
        # Block 0: action line with profile link and lead name.
        assert blocks[0]["type"] == "section"
        block0_text = blocks[0]["text"]["text"]
        assert "waylonkrush" in block0_text
        assert "Waylon Krush" in block0_text
        # Block 1: quoted full message.
        assert blocks[1]["type"] == "section"
        assert "> hello there" in blocks[1]["text"]["text"]
        # Block 2: context block with operator name.
        assert blocks[2]["type"] == "context"
        elements_text = " ".join(e["text"] for e in blocks[2]["elements"])
        assert "Arian" in elements_text
        actions = next(b for b in blocks if b.get("type") == "actions")
        reply_button = next(
            el for el in actions["elements"]
            if el.get("action_id") == "linkedin_reply_button"
        )
        reply_value = json.loads(reply_button["value"])
        assert reply_value == {
            "lead_id": lead.id,
            "operator": "Arian",
            "thread_external_id": "thread-arian",
        }
        context_button = next(
            el for el in actions["elements"]
            if el.get("action_id") == "linkedin_lead_context_button"
        )
        assert json.loads(context_button["value"]) == {
            "lead_id": lead.id,
            "operator": "Arian",
            "thread_external_id": "thread-arian",
        }

    def test_long_text_is_not_truncated_at_preview_length(self, db, slack_url):
        from crm.models import Lead
        lead = Lead.objects.create(
            first_name="A", linkedin_url="https://www.linkedin.com/in/a-long/",
        )
        text = "x" * 600
        with patch("linkedin.notifications.slack.request.urlopen") as mock_open:
            mock_open.return_value.__enter__.return_value.status = 200
            slack_mod.notify_message_received(
                lead=lead, text=text, operator="",
            )
        sent = json.loads(mock_open.call_args[0][0].data.decode("utf-8"))
        message_block = sent["blocks"][1]["text"]["text"]
        assert text in message_block
        assert "...(truncated)" not in message_block

    def test_message_received_preserves_line_breaks(self, db, slack_url):
        from crm.models import Lead
        lead = Lead.objects.create(
            first_name="A", linkedin_url="https://www.linkedin.com/in/a-lines/",
        )
        with patch("linkedin.notifications.slack.request.urlopen") as mock_open:
            mock_open.return_value.__enter__.return_value.status = 200
            slack_mod.notify_message_received(
                lead=lead, text="first line\n\nsecond line", operator="",
            )
        sent = json.loads(mock_open.call_args[0][0].data.decode("utf-8"))
        assert sent["blocks"][1]["text"]["text"] == "> first line\n>\n> second line"

    def test_very_long_text_is_slack_safely_truncated(self, db, slack_url):
        from crm.models import Lead
        lead = Lead.objects.create(
            first_name="A", linkedin_url="https://www.linkedin.com/in/a-huge/",
        )
        with patch("linkedin.notifications.slack.request.urlopen") as mock_open:
            mock_open.return_value.__enter__.return_value.status = 200
            slack_mod.notify_message_received(
                lead=lead, text="x" * 4000, operator="",
            )
        sent = json.loads(mock_open.call_args[0][0].data.decode("utf-8"))
        message_block = sent["blocks"][1]["text"]["text"]
        assert len(message_block) <= slack_mod._SLACK_SECTION_TEXT_LIMIT
        assert "...(truncated)" in message_block

    def test_disqualified_lead_name_has_no_prefix(self, db, slack_url):
        """Disqualified leads must not bleed the '(Disqualified)' prefix into Slack."""
        from crm.models import Lead
        lead = Lead.objects.create(
            first_name="Bad", last_name="Actor",
            linkedin_url="https://www.linkedin.com/in/badactor/",
            disqualified=True,
        )
        with patch("linkedin.notifications.slack.request.urlopen") as mock_open:
            mock_open.return_value.__enter__.return_value.status = 200
            slack_mod.notify_message_received(
                lead=lead, text="hey", operator="Arian",
            )
        mock_open.assert_called_once()
        sent = json.loads(mock_open.call_args[0][0].data.decode("utf-8"))
        body = json.dumps(sent)
        assert "(Disqualified)" not in body
        assert "Bad Actor" in body

    def test_unknown_company_sentinel_is_hidden(self, db, slack_url):
        from crm.models import Lead
        lead = Lead.objects.create(
            first_name="Jamil", last_name="Mahmood",
            company_name="Unknown Company",
            linkedin_url="https://www.linkedin.com/in/jamil-j-mahmood/",
        )
        with patch("linkedin.notifications.slack.request.urlopen") as mock_open:
            mock_open.return_value.__enter__.return_value.status = 200
            slack_mod.notify_message_received(
                lead=lead, text="hi", operator="Leili",
            )
        sent = json.loads(mock_open.call_args[0][0].data.decode("utf-8"))
        body = json.dumps(sent)
        assert "Unknown Company" not in body
        assert "Jamil Mahmood" in body


    def test_includes_provider_select_block(self, db, slack_url):
        from crm.models import Lead
        lead = Lead.objects.create(
            first_name="Ada", last_name="Lovelace",
            linkedin_url="https://www.linkedin.com/in/ada/",
        )
        with patch("linkedin.notifications.slack.request.urlopen") as mock_open:
            mock_open.return_value.__enter__.return_value.status = 200
            slack_mod.notify_message_received(
                lead=lead, text="hi", operator="Arian",
            )
        sent = json.loads(mock_open.call_args[0][0].data.decode("utf-8"))
        actions = [b for b in sent["blocks"] if b.get("type") == "actions"]
        assert len(actions) == 1
        select = next(
            el for el in actions[0]["elements"]
            if el.get("action_id") == "enrich_phone_select"
        )
        assert select["type"] == "static_select"
        assert select["action_id"] == "enrich_phone_select"
        values = [opt["value"] for opt in select["options"]]
        assert values == [
            f"{lead.id}:waterfall", f"{lead.id}:bettercontact",
            f"{lead.id}:leadmagic", f"{lead.id}:prospeo",
        ]


class TestNotifyPhoneEnriched:
    def _lead(self):
        from types import SimpleNamespace
        return SimpleNamespace(
            first_name="Ada", last_name="Lovelace", company_name="Analytical Engines",
            linkedin_url="https://www.linkedin.com/in/ada/", public_identifier="ada",
        )

    def test_noop_when_webhook_unset(self, monkeypatch):
        from linkedin.notifications.slack import notify_phone_enriched
        from linkedin.enrichment.base import EnrichmentResult, EnrichmentStatus

        monkeypatch.setattr(
            "linkedin.notifications.slack.SLACK_REPLIES_WEBHOOK_URL", "",
        )
        # urlopen must never be called when the webhook is unset.
        with patch("linkedin.notifications.slack.request.urlopen") as mock_open:
            notify_phone_enriched(
                lead=self._lead(),
                result=EnrichmentResult(
                    status=EnrichmentStatus.FOUND, provider="leadmagic", phone="+1",
                ),
            )
        mock_open.assert_not_called()

    def test_found_posts_phone_and_provider(self, monkeypatch):
        from linkedin.notifications.slack import notify_phone_enriched
        from linkedin.enrichment.base import EnrichmentResult, EnrichmentStatus

        monkeypatch.setattr(
            "linkedin.notifications.slack.SLACK_REPLIES_WEBHOOK_URL",
            "https://hooks.test/x",
        )
        with patch("linkedin.notifications.slack.request.urlopen") as mock_open:
            mock_open.return_value.__enter__.return_value.status = 200
            notify_phone_enriched(
                lead=self._lead(),
                result=EnrichmentResult(
                    status=EnrichmentStatus.FOUND, provider="leadmagic",
                    phone="+14155550199",
                ),
            )
        body = mock_open.call_args[0][0].data.decode("utf-8")
        # Rendered in NANP display format, not the raw E.164 string.
        assert "+1 (415) 555-0199" in body
        assert "leadmagic" in body

    def test_not_found_posts_no_number(self, monkeypatch):
        from linkedin.notifications.slack import notify_phone_enriched
        from linkedin.enrichment.base import EnrichmentResult, EnrichmentStatus

        monkeypatch.setattr(
            "linkedin.notifications.slack.SLACK_REPLIES_WEBHOOK_URL",
            "https://hooks.test/x",
        )
        with patch("linkedin.notifications.slack.request.urlopen") as mock_open:
            mock_open.return_value.__enter__.return_value.status = 200
            notify_phone_enriched(
                lead=self._lead(),
                result=EnrichmentResult(
                    status=EnrichmentStatus.NOT_FOUND, provider="prospeo",
                ),
            )
        body = mock_open.call_args[0][0].data.decode("utf-8")
        assert "No phone number found" in body


def test_format_phone_display():
    from linkedin.notifications.slack import format_phone_display

    # NANP — with and without the leading +1 / formatting.
    assert format_phone_display("+12566558960") == "+1 (256) 655-8960"
    assert format_phone_display("2566558960") == "+1 (256) 655-8960"
    assert format_phone_display("+1 (256) 655-8960") == "+1 (256) 655-8960"
    # Non-NANP and empty are returned unchanged.
    assert format_phone_display("+447911123456") == "+447911123456"
    assert format_phone_display("") == ""


class TestNotifyDegraded:
    """notify_degraded posts monitoring alerts to the ops webhook."""

    def test_noop_when_webhook_unset(self):
        """conftest._silence_slack clears the ops webhook — must not POST."""
        with patch("linkedin.notifications.slack.request.urlopen") as mock_open:
            slack_mod.notify_degraded(
                sender="Arian", title="X looks down", detail="no heartbeat",
            )
        mock_open.assert_not_called()

    def test_posts_to_ops_webhook(self, slack_url):
        with patch("linkedin.notifications.slack.request.urlopen") as mock_open:
            mock_open.return_value.__enter__.return_value.status = 200
            slack_mod.notify_degraded(
                sender="Chuka",
                title="Chuka's daemon looks down",
                detail="No heartbeat for 120 min.",
            )
        mock_open.assert_called_once()
        sent = json.loads(mock_open.call_args[0][0].data.decode("utf-8"))
        body = json.dumps(sent)
        assert "Chuka's daemon looks down" in body
        assert "120 min" in body
        # Sender rendered in the context block.
        ctx = [b for b in sent["blocks"] if b.get("type") == "context"]
        assert ctx and "Chuka" in " ".join(
            e["text"] for e in ctx[0]["elements"]
        )


class TestNotifySweepSummary:
    """notify_sweep_summary posts the per-sweep analytics to the ops webhook."""

    def test_noop_when_webhook_unset(self):
        """conftest._silence_slack clears the ops webhook — must not POST."""
        with patch("linkedin.notifications.slack.request.urlopen") as mock_open:
            slack_mod.notify_sweep_summary(
                sender="Leili", connects_today=18, followups_today=5,
            )
        mock_open.assert_not_called()

    def test_posts_to_ops_webhook(self, slack_url):
        with patch("linkedin.notifications.slack.request.urlopen") as mock_open:
            mock_open.return_value.__enter__.return_value.status = 200
            slack_mod.notify_sweep_summary(
                sender="Leili", connects_today=18, followups_today=5,
                email_followups_today=3,
            )
        mock_open.assert_called_once()
        body = mock_open.call_args[0][0].data.decode("utf-8")
        assert "Leili" in body
        assert "18 invites" in body
        assert "5 LinkedIn follow-ups" in body
        assert "3 email follow-ups" in body
