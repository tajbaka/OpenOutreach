from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from django.utils import timezone

from linkedin.models import LinkedInFeedPost

ALERT_INTENTS = {
    LinkedInFeedPost.Intent.HIGH,
    LinkedInFeedPost.Intent.URGENT,
}
ALERT_AUDIENCES = {
    LinkedInFeedPost.Audience.CSP,
    LinkedInFeedPost.Audience.ADVISOR_PARTNER,
    LinkedInFeedPost.Audience.ASSESSOR,
    LinkedInFeedPost.Audience.CHANNEL,
}


@dataclass(frozen=True)
class FeedPostAnalysisResult:
    is_relevant: bool
    should_alert: bool
    intent: str
    audience: str
    topics: list[str]
    relevance_reason: str
    suggested_action: str
    raw: dict


def codex_review_instructions() -> str:
    return (
        "Review these LinkedIn feed posts for Boundera. Flag high/urgent intent "
        "when someone wants a GRC automation tool, FedRAMP tool, FedRAMP 20x "
        "tool, CMMC/FedRAMP/GRC help, compliance automation, evidence/SSP/POA&M "
        "support, or wants to work as / hire / find a GRC, FedRAMP, CMMC, "
        "3PAO, assessor, advisor, channel, or partner resource. A Pete "
        "Strouse-style post about an interesting FedRAMP advisory opportunity "
        "should be high intent. Ignore generic ads, generic cybersecurity news, "
        "broad thought leadership with no ask/pain/opportunity, generic hiring "
        "outside GRC/FedRAMP/CMMC, and Boundera's own posts unless there is an "
        "external opportunity to act on."
    )


def serialize_posts_for_codex(posts: Iterable[LinkedInFeedPost]) -> dict:
    return {
        "instructions": codex_review_instructions(),
        "schema": {
            "post_id": "integer from the input post.id",
            "is_relevant": "boolean",
            "should_alert": "boolean",
            "intent": ["none", "low", "medium", "high", "urgent"],
            "audience": [
                "csp", "advisor_partner", "assessor", "channel",
                "other", "not_relevant",
            ],
            "topics": ["short strings such as FedRAMP, CMMC, GRC automation"],
            "relevance_reason": "short reason, quote the signal when useful",
            "suggested_action": "short suggested action for the human operator",
        },
        "posts": [_serialize_post(post) for post in posts],
    }


def load_decisions(path: str | Path) -> list[tuple[int, FeedPostAnalysisResult]]:
    payload = json.loads(Path(path).read_text())
    rows = payload if isinstance(payload, list) else payload.get("decisions", [])
    if not isinstance(rows, list):
        raise ValueError("Decision JSON must be a list or an object with a decisions list.")
    decisions: list[tuple[int, FeedPostAnalysisResult]] = []
    for row in rows:
        if "post_id" not in row:
            raise ValueError("Every decision row must include post_id.")
        decisions.append((int(row["post_id"]), decision_from_mapping(row)))
    return decisions


def decision_from_mapping(row: dict) -> FeedPostAnalysisResult:
    intent = _normalize_choice(
        str(row.get("intent", "")),
        {choice for choice, _label in LinkedInFeedPost.Intent.choices},
        LinkedInFeedPost.Intent.NONE,
    )
    audience = _normalize_choice(
        str(row.get("audience", "")),
        {choice for choice, _label in LinkedInFeedPost.Audience.choices},
        LinkedInFeedPost.Audience.NOT_RELEVANT,
    )
    is_relevant = bool(row.get("is_relevant"))
    requested_alert = bool(row.get("should_alert"))
    should_alert = (
        requested_alert
        and is_relevant
        and intent in ALERT_INTENTS
        and audience in ALERT_AUDIENCES
    )
    topics = [str(topic).strip() for topic in (row.get("topics") or []) if str(topic).strip()]
    return FeedPostAnalysisResult(
        is_relevant=is_relevant,
        should_alert=should_alert,
        intent=intent,
        audience=audience,
        topics=topics[:8],
        relevance_reason=str(row.get("relevance_reason") or "").strip(),
        suggested_action=str(row.get("suggested_action") or "").strip(),
        raw=dict(row),
    )


def save_feed_post_analysis(
    post: LinkedInFeedPost,
    result: FeedPostAnalysisResult,
) -> LinkedInFeedPost:
    post.analyzed_at = timezone.now()
    post.intent = result.intent
    post.audience = result.audience
    post.topics = result.topics
    post.relevance_reason = result.relevance_reason
    post.suggested_action = result.suggested_action
    post.raw_analysis = result.raw
    post.save(
        update_fields=[
            "analyzed_at", "intent", "audience", "topics",
            "relevance_reason", "suggested_action", "raw_analysis", "updated_at",
        ],
    )
    return post


def mark_feed_post_slack_notified(post: LinkedInFeedPost) -> None:
    post.slack_notified_at = timezone.now()
    post.save(update_fields=["slack_notified_at", "updated_at"])


def should_notify_feed_post(post: LinkedInFeedPost) -> bool:
    return (
        post.slack_notified_at is None
        and post.intent in ALERT_INTENTS
        and post.audience in ALERT_AUDIENCES
        and bool(post.relevance_reason)
    )


def _serialize_post(post: LinkedInFeedPost) -> dict:
    return {
        "id": post.id,
        "author_name": post.author_name,
        "author_headline": post.author_headline,
        "author_profile_url": post.author_profile_url,
        "post_url": post.post_url,
        "posted_at": post.posted_at.isoformat() if post.posted_at else "",
        "last_seen_at": post.last_seen_at.isoformat() if post.last_seen_at else "",
        "post_text": post.post_text,
        "seen_by": [
            {
                "operator": obs.operator,
                "account_username": obs.account_username,
                "first_seen_at": obs.first_seen_at.isoformat(),
                "last_seen_at": obs.last_seen_at.isoformat(),
                "seen_count": obs.seen_count,
            }
            for obs in post.observations.order_by("operator", "account_username")[:10]
        ],
    }


def _normalize_choice(value: str, allowed: set[str], default: str) -> str:
    cleaned = (value or "").strip().lower()
    return cleaned if cleaned in allowed else default
