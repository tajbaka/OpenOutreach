from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.utils import timezone

from linkedin.feed_analysis import (
    decision_from_mapping,
    load_decisions,
    save_feed_post_analysis,
    serialize_posts_for_codex,
    should_notify_feed_post,
)
from linkedin.models import LinkedInFeedPost


def _post(**overrides):
    data = {
        "content_hash": "abc123",
        "author_name": "Pete Strouse",
        "author_headline": "FedRAMP advisor",
        "post_url": "https://www.linkedin.com/posts/petestrouse_i-have-an-interesting-opportunity-for-my-share-7478468303850639360-7JJZ/",
        "post_text": (
            "I have an interesting opportunity for my share related to "
            "FedRAMP advisory work. Looking for the right partner."
        ),
        "first_seen_at": timezone.now(),
        "last_seen_at": timezone.now(),
    }
    data.update(overrides)
    return LinkedInFeedPost.objects.create(**data)


@pytest.mark.django_db
def test_serialize_posts_for_codex_includes_grc_tool_and_advisor_criteria():
    post = _post()

    payload = serialize_posts_for_codex([post])

    assert "FedRAMP 20x tool" in payload["instructions"]
    assert "GRC automation tool" in payload["instructions"]
    assert "wants to work as" in payload["instructions"]
    assert payload["posts"][0]["id"] == post.id
    assert "interesting opportunity" in payload["posts"][0]["post_text"]


def test_decision_from_mapping_catches_fedramp_advisory_opportunity():
    result = decision_from_mapping({
        "post_id": 123,
        "is_relevant": True,
        "should_alert": True,
        "intent": "high",
        "audience": "advisor_partner",
        "topics": ["FedRAMP", "advisory", "partner"],
        "relevance_reason": "The author says they have a FedRAMP advisory opportunity.",
        "suggested_action": "Review the post and reach out with partner context.",
    })

    assert result.should_alert is True
    assert result.intent == LinkedInFeedPost.Intent.HIGH
    assert result.audience == LinkedInFeedPost.Audience.ADVISOR_PARTNER
    assert "FedRAMP" in result.topics


@pytest.mark.django_db
def test_save_feed_post_analysis_persists_codex_decision():
    post = _post(content_hash="save-test")
    result = decision_from_mapping({
        "post_id": post.id,
        "is_relevant": True,
        "should_alert": True,
        "intent": "urgent",
        "audience": "csp",
        "topics": ["GRC automation", "FedRAMP tool"],
        "relevance_reason": "They want a FedRAMP automation tool.",
        "suggested_action": "Reply with Boundera context.",
    })

    save_feed_post_analysis(post, result)

    post.refresh_from_db()
    assert post.analyzed_at is not None
    assert post.intent == LinkedInFeedPost.Intent.URGENT
    assert post.audience == LinkedInFeedPost.Audience.CSP
    assert post.topics == ["GRC automation", "FedRAMP tool"]
    assert should_notify_feed_post(post) is True


def test_load_decisions_requires_post_id(tmp_path):
    path = tmp_path / "decisions.json"
    path.write_text(json.dumps([{"intent": "high"}]))

    with pytest.raises(ValueError, match="post_id"):
        load_decisions(path)


@pytest.mark.django_db
def test_analyze_linkedin_feed_exports_codex_queue(tmp_path):
    post = _post(content_hash="export")
    out = tmp_path / "queue.json"

    call_command("analyze_linkedin_feed", output=str(out), limit=10)

    payload = json.loads(out.read_text())
    assert payload["posts"][0]["id"] == post.id
    assert "schema" in payload


@pytest.mark.django_db
def test_analyze_linkedin_feed_exports_all_unanalyzed_posts_by_default(tmp_path):
    analyzed = _post(content_hash="already-analyzed", analyzed_at=timezone.now())
    first = _post(content_hash="unanalyzed-1")
    second = _post(content_hash="unanalyzed-2")
    out = tmp_path / "queue.json"

    call_command("analyze_linkedin_feed", output=str(out))

    payload = json.loads(out.read_text())
    exported_ids = {row["id"] for row in payload["posts"]}
    assert exported_ids == {first.id, second.id}
    assert analyzed.id not in exported_ids


@pytest.mark.django_db
@patch("linkedin.notifications.slack._post_to_slack")
def test_analyze_linkedin_feed_applies_codex_decision_and_posts_slack(
    mock_post,
    monkeypatch,
    tmp_path,
):
    from linkedin.notifications import slack

    post = _post(content_hash="apply")
    decisions = tmp_path / "decisions.json"
    decisions.write_text(json.dumps({
        "decisions": [{
            "post_id": post.id,
            "is_relevant": True,
            "should_alert": True,
            "intent": "high",
            "audience": "advisor_partner",
            "topics": ["FedRAMP"],
            "relevance_reason": "FedRAMP advisory opportunity.",
            "suggested_action": "Reach out.",
        }],
    }))
    monkeypatch.setattr(slack, "SLACK_REPLIES_WEBHOOK_URL", "https://hooks.slack.test/replies")

    call_command("analyze_linkedin_feed", apply_json=str(decisions))

    post.refresh_from_db()
    assert post.intent == LinkedInFeedPost.Intent.HIGH
    assert post.slack_notified_at is not None
    mock_post.assert_called_once()
