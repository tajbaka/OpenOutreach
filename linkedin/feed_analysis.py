from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit, urlunsplit

from django.db.models import Q
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
GROUP_LOOKBACK_DAYS = 14
_WHITESPACE_RE = re.compile(r"\s+")

MARKET_TRIGGER_PHRASES = {
    "have a soc 2 and always wanted fedramp": "fedramp-class-a-soc2",
    "class a certifications allow you to get fedramp certified": "fedramp-class-a-soc2",
    "fedramp marketplace from your existing soc 2 report": "fedramp-class-a-soc2",
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
        "tool, FedRAMP/GRC help, compliance automation, evidence/SSP/POA&M "
        "support, or wants to work as / hire / find a GRC, FedRAMP, "
        "3PAO, assessor, advisor, channel, or partner resource. A Pete "
        "Strouse-style post about an interesting FedRAMP advisory opportunity "
        "should be high intent. Ignore generic ads, generic cybersecurity news, "
        "broad thought leadership with no ask/pain/opportunity, generic hiring "
        "outside GRC/FedRAMP, CMMC-only posts, and Boundera's own posts unless "
        "there is an external opportunity to act on. Use crm_matches as grounding when "
        "assigning audience: assessor/advisory firms such as A-LIGN, 3PAOs, "
        "auditors, and consultants should be assessor or advisor_partner rather "
        "than csp even if the post discusses CSPs or FedRAMP. Treat timely "
        "FedRAMP 20x, Class A, SOC 2-to-FedRAMP, and similar "
        "ecosystem trigger posts as alert-worthy high signal when they create "
        "a useful outreach/research angle, even if the top-level post is a "
        "repost, comment, or market observation rather than direct buyer intent. "
        "Do not alert on CMMC-only commentary, CMMC-only news, or CMMC-only "
        "advisor/assessor posts unless there is also a clear FedRAMP, GRC "
        "automation, compliance automation, or Boundera-relevant opportunity."
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
            "topics": ["short strings such as FedRAMP, GRC automation"],
            "relevance_reason": "short reason, quote the signal when useful",
            "suggested_action": "short suggested action for the human operator",
            "crm_matches": (
                "read-only context for profile/company matches in our CRM; use "
                "it to avoid misclassifying assessors/advisors as CSPs"
            ),
        },
        "posts": [_serialize_post(post) for post in posts],
    }


def load_decisions(path: str | Path) -> list[tuple[int, FeedPostAnalysisResult]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
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
        and not _is_cmmc_only_alert(row)
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


def mark_feed_posts_slack_notified(posts: Iterable[LinkedInFeedPost]) -> None:
    now = timezone.now()
    ids = [post.id for post in posts]
    if ids:
        LinkedInFeedPost.objects.filter(id__in=ids).update(
            slack_notified_at=now,
            updated_at=now,
        )


def should_notify_feed_post(post: LinkedInFeedPost) -> bool:
    return (
        bool(post.post_url or post.activity_urn)
        and post.slack_notified_at is None
        and post.intent in ALERT_INTENTS
        and post.audience in ALERT_AUDIENCES
        and bool(post.relevance_reason)
    )


def feed_post_group_key(post: LinkedInFeedPost) -> str:
    text = _normalize_group_text(post.post_text)
    for phrase, key in MARKET_TRIGGER_PHRASES.items():
        if phrase in text:
            return f"trigger:{key}"
    if "fedramp" in text and "class a" in text and "soc 2" in text:
        return "trigger:fedramp-class-a-soc2"
    if post.activity_urn:
        return f"urn:{post.activity_urn}"
    if post.post_url:
        return f"url:{post.post_url.rstrip('/')}"
    return f"hash:{post.content_hash}"


def group_feed_posts_for_alert(
    alert_posts: Iterable[LinkedInFeedPost],
    *,
    lookback_days: int = GROUP_LOOKBACK_DAYS,
) -> list[list[LinkedInFeedPost]]:
    alert_posts = list(alert_posts)
    if not alert_posts:
        return []

    wanted_keys = {feed_post_group_key(post) for post in alert_posts}
    cutoff = timezone.now() - timedelta(days=lookback_days)
    recent = (
        LinkedInFeedPost.objects
        .prefetch_related("observations")
        .filter(
            Q(post_url__gt="") | Q(activity_urn__gt=""),
            last_seen_at__gte=cutoff,
        )
        .order_by("-last_seen_at")
    )
    by_key: dict[str, list[LinkedInFeedPost]] = {key: [] for key in wanted_keys}
    for post in recent:
        key = feed_post_group_key(post)
        if key in by_key:
            by_key[key].append(post)

    groups: list[list[LinkedInFeedPost]] = []
    seen_post_ids: set[int] = set()
    for post in alert_posts:
        key = feed_post_group_key(post)
        group = [item for item in by_key.get(key, [post]) if item.id not in seen_post_ids]
        if not group:
            continue
        seen_post_ids.update(item.id for item in group)
        groups.append(group)
    return groups


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
        "crm_matches": crm_matches_for_feed_post(post),
    }


def _normalize_choice(value: str, allowed: set[str], default: str) -> str:
    cleaned = (value or "").strip().lower()
    return cleaned if cleaned in allowed else default


def _normalize_group_text(value: str) -> str:
    return _WHITESPACE_RE.sub(" ", (value or "").lower()).strip()


def _is_cmmc_only_alert(row: dict) -> bool:
    haystack = _normalize_group_text(
        " ".join([
            " ".join(str(topic) for topic in (row.get("topics") or [])),
            str(row.get("relevance_reason") or ""),
            str(row.get("suggested_action") or ""),
        ]),
    )
    if "cmmc" not in haystack:
        return False
    allowed_terms = (
        "fedramp",
        "grc automation",
        "compliance automation",
        "evidence automation",
        "ssp",
        "poa&m",
        "poam",
        "boundera",
    )
    return not any(term in haystack for term in allowed_terms)


def crm_matches_for_feed_post(post: LinkedInFeedPost) -> list[dict]:
    urls = _profile_urls_for_post(post)
    if not urls:
        return []

    from crm.models import Lead

    leads = Lead.objects.filter(linkedin_url__in=urls).order_by("company_name", "last_name", "first_name")
    return [
        {
            "lead_id": lead.id,
            "full_name": lead.full_name,
            "company_name": lead.company_name,
            "linkedin_url": lead.linkedin_url,
            "public_identifier": lead.public_identifier,
            "description": lead.description[:500],
            "icp": lead.icp,
            "disqualified": lead.disqualified,
        }
        for lead in leads[:12]
    ]


def _profile_urls_for_post(post: LinkedInFeedPost) -> list[str]:
    urls: list[str] = []
    if post.author_profile_url:
        urls.append(post.author_profile_url)
    raw_payload = post.raw_payload or {}
    for link in raw_payload.get("candidateLinks") or []:
        href = str(link.get("href") or "")
        if "/in/" in href:
            urls.append(href)

    normalized: list[str] = []
    seen: set[str] = set()
    for url in urls:
        clean = _normalize_profile_url(url)
        if clean and clean not in seen:
            normalized.append(clean)
            seen.add(clean)
    return normalized


def _normalize_profile_url(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    parts = urlsplit(value)
    if not parts.netloc.endswith("linkedin.com") or "/in/" not in parts.path:
        return ""
    path = parts.path.rstrip("/") + "/"
    return urlunsplit((parts.scheme or "https", parts.netloc, path, "", ""))
