"""Read-only Slack context workflow for collected LinkedIn feed posts.

The shared Slack endpoint registers these intents and delegates here.  This
module does not enqueue sender work and stays independent from Django and the
feed-comment workflow.
"""
from __future__ import annotations

import json
import os
from urllib import request
from urllib.parse import parse_qs


POST_CONTEXT_ACTION_ID = "linkedin_feed_context_button"
POST_CONTEXT_AI_ACTION_ID = "linkedin_feed_context_ai_button"
POST_CONTEXT_MODAL_CALLBACK_ID = "linkedin_feed_context_modal"

INTENT_POST_CONTEXT = "feed_post_context"
INTENT_POST_CONTEXT_AI = "feed_post_context_ai"

INTENT_BY_ACTION_ID = {
    POST_CONTEXT_ACTION_ID: INTENT_POST_CONTEXT,
    POST_CONTEXT_AI_ACTION_ID: INTENT_POST_CONTEXT_AI,
}
HANDLER_BY_INTENT = {
    INTENT_POST_CONTEXT: "_handle_feed_post_context",
    INTENT_POST_CONTEXT_AI: "_handle_feed_post_context_ai",
}

LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_API_BASE = (os.environ.get("LLM_API_BASE") or "https://api.openai.com/v1").rstrip("/")
AI_MODEL = os.environ.get("AI_MODEL", "")

_LLM_TIMEOUT_SECONDS = 12
_SECTION_TEXT_LIMIT = 2700


def parse_post_context_action(body: str) -> dict:
    """Extract the post and modal coordinates from a context action."""
    payload = _decode_body(body)
    actions = payload.get("actions") or []
    if not actions:
        raise ValueError("no actions in payload")
    action = actions[0]
    action_id = action.get("action_id") or ""
    if action_id not in INTENT_BY_ACTION_ID:
        raise ValueError("not a feed post context action")

    value = json.loads(action.get("value") or "{}")
    view = payload.get("view") or {}
    metadata = json.loads(view.get("private_metadata") or "{}")
    return {
        "post_id": int(value.get("post_id") or metadata["post_id"]),
        "trigger_id": payload.get("trigger_id") or "",
        "view_id": view.get("id") or "",
        "view_hash": view.get("hash") or "",
        "action_id": action_id,
    }


def fetch_feed_post_context(conn, post_id: int) -> dict:
    """Read the complete saved post, analysis, and observation history."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, activity_urn, post_url, author_name, author_headline, "
            "author_profile_url, post_text, posted_at, first_seen_at, last_seen_at, "
            "analyzed_at, intent, audience, topics, relevance_reason, "
            "suggested_action, raw_analysis "
            "FROM linkedin_linkedinfeedpost WHERE id = %s",
            (int(post_id),),
        )
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"feed post {post_id} not found")

        activity_urn = row[1] or ""
        post = {
            "id": int(row[0]),
            "activity_urn": activity_urn,
            "post_url": row[2] or (
                f"https://www.linkedin.com/feed/update/{activity_urn}/"
                if activity_urn else ""
            ),
            "author_name": row[3] or "",
            "author_headline": row[4] or "",
            "author_profile_url": row[5] or "",
            "post_text": row[6] or "",
            "posted_at": row[7],
            "first_seen_at": row[8],
            "last_seen_at": row[9],
            "analyzed_at": row[10],
            "intent": row[11] or "",
            "audience": row[12] or "",
            "topics": row[13] or [],
            "relevance_reason": row[14] or "",
            "suggested_action": row[15] or "",
            "raw_analysis": row[16] or {},
        }

        cur.execute(
            "SELECT \"operator\", account_username, first_seen_at, "
            "last_seen_at, seen_count "
            "FROM linkedin_linkedinfeedobservation "
            "WHERE post_id = %s "
            "ORDER BY \"operator\", account_username",
            (int(post_id),),
        )
        observation_rows = cur.fetchall()

    observations = [
        {
            "operator": operator or "",
            "account_username": account_username or "",
            "first_seen_at": first_seen_at,
            "last_seen_at": last_seen_at,
            "seen_count": int(seen_count or 0),
        }
        for operator, account_username, first_seen_at, last_seen_at, seen_count
        in observation_rows
    ]
    return {"post": post, "observations": observations}


def render_post_context_blocks(
    context: dict,
    *,
    ai_summary: str = "",
    ai_error: str = "",
    loading: bool = False,
) -> list[dict]:
    """Render the full post first, followed by saved and generated context."""
    post = context["post"]
    full_post_chunks = _split_slack_text(post.get("post_text") or "(no text)")
    blocks: list[dict] = []
    for index, chunk in enumerate(full_post_chunks):
        prefix = "*Full post*\n" if index == 0 else ""
        blocks.append({
            "type": "section",
            "block_id": f"feed_post_context_full_post_{index}",
            "text": {"type": "mrkdwn", "text": prefix + chunk},
        })

    author = _slack_escape(post.get("author_name") or "Unknown author")
    profile_url = post.get("author_profile_url") or ""
    author_text = f"<{profile_url}|{author}>" if profile_url else author
    headline = _slack_escape(post.get("author_headline") or "")
    post_url = post.get("post_url") or ""
    identity_lines = [f"*Posted by:* {author_text}"]
    if headline:
        identity_lines.append(headline)
    if post_url:
        identity_lines.append(f"<{post_url}|Open LinkedIn post>")
    blocks.extend([
        {"type": "divider"},
        {
            "type": "section",
            "block_id": "feed_post_context_author",
            "text": {"type": "mrkdwn", "text": "\n".join(identity_lines)},
        },
        {
            "type": "section",
            "block_id": "feed_post_context_analysis_fields",
            "fields": _analysis_fields(post),
        },
    ])
    for block_id, label, value in [
        (
            "feed_post_context_reason",
            "Why it matters",
            post.get("relevance_reason") or "No reason saved.",
        ),
        (
            "feed_post_context_suggested_action",
            "Suggested action",
            post.get("suggested_action") or "Review the post.",
        ),
    ]:
        for index, chunk in enumerate(_split_slack_text(value)):
            prefix = f"*{label}*\n" if index == 0 else ""
            blocks.append({
                "type": "section",
                "block_id": f"{block_id}_{index}",
                "text": {"type": "mrkdwn", "text": prefix + chunk},
            })

    observations = context.get("observations") or []
    if observations:
        lines = []
        for observation in observations[:12]:
            operator = observation.get("operator") or observation.get("account_username") or "Unknown"
            display = "Eddy" if operator == "Chuka" else operator
            account = observation.get("account_username") or ""
            account_suffix = f" ({account})" if account and account.lower() != operator.lower() else ""
            seen_count = observation.get("seen_count") or 0
            last_seen = _format_timestamp(observation.get("last_seen_at"))
            lines.append(
                f"- *{_slack_escape(display)}*{_slack_escape(account_suffix)}: "
                f"seen {seen_count}x" + (f", last {last_seen}" if last_seen else "")
            )
        blocks.append({
            "type": "section",
            "block_id": "feed_post_context_observations",
            "text": {"type": "mrkdwn", "text": "*Feed sightings*\n" + "\n".join(lines)},
        })

    if ai_summary:
        blocks.append({"type": "divider"})
        for index, chunk in enumerate(_split_slack_text(ai_summary)):
            prefix = "*AI summary*\n" if index == 0 else ""
            blocks.append({
                "type": "section",
                "block_id": f"feed_post_context_ai_summary_{index}",
                "text": {"type": "mrkdwn", "text": prefix + chunk},
            })
    elif ai_error:
        blocks.extend([
            {"type": "divider"},
            {
                "type": "section",
                "block_id": "feed_post_context_ai_error",
                "text": {
                    "type": "mrkdwn",
                    "text": ":warning: *AI summary failed* - `"
                    + _slack_escape(_compact(ai_error, 400)) + "`",
                },
            },
        ])

    if loading:
        blocks.extend([
            {"type": "divider"},
            {
                "type": "section",
                "block_id": "feed_post_context_loading",
                "text": {
                    "type": "mrkdwn",
                    "text": ":hourglass_flowing_sand: *Generating post summary...*",
                },
            },
        ])
    else:
        blocks.append({
            "type": "actions",
            "block_id": "feed_post_context_actions",
            "elements": [{
                "type": "button",
                "action_id": POST_CONTEXT_AI_ACTION_ID,
                "text": {"type": "plain_text", "text": "Generate AI summary"},
                "value": json.dumps({"post_id": post["id"]}, separators=(",", ":")),
            }],
        })
    return blocks[:100]


def open_post_context_modal(*, slack_api, trigger_id: str, context: dict) -> None:
    slack_api("views.open", {
        "trigger_id": trigger_id,
        "view": _post_context_view(context=context),
    })


def update_post_context_modal(
    *,
    slack_api,
    view_id: str,
    context: dict,
    view_hash: str = "",
    ai_summary: str = "",
    ai_error: str = "",
    loading: bool = False,
) -> None:
    payload = {
        "view_id": view_id,
        "view": _post_context_view(
            context=context,
            ai_summary=ai_summary,
            ai_error=ai_error,
            loading=loading,
        ),
    }
    if view_hash:
        payload["hash"] = view_hash
    slack_api("views.update", payload)


def generate_ai_post_summary(context: dict) -> str:
    """Generate a decision-oriented summary grounded in the saved feed record."""
    if not LLM_API_KEY or not AI_MODEL:
        raise RuntimeError("LLM_API_KEY or AI_MODEL is not configured on Vercel")
    post = context["post"]
    prompt = (
        "Analyze this LinkedIn feed post for a Boundera operator. Be concise but specific. "
        "Use these headings: What the post says; Signal and opportunity; Recommended engagement; "
        "Cautions. Separate explicit claims from inference, identify any ask or pain point, and do "
        "not invent facts. Explain FedRAMP, GRC, CSP, assessor, advisor, or channel relevance only "
        "when supported by the post. The recommendation should help a human decide whether and how "
        "to engage; do not draft the actual comment.\n\n"
        "Boundera context: Boundera helps software vendors reduce manual FedRAMP evidence work, "
        "KSI/package readiness friction, gap tracking, remediation ownership, and ongoing monitoring.\n\n"
        + json.dumps({
            "author": post.get("author_name", ""),
            "author_headline": post.get("author_headline", ""),
            "full_post": post.get("post_text", ""),
            "saved_analysis": {
                "intent": post.get("intent", ""),
                "audience": post.get("audience", ""),
                "topics": post.get("topics", []),
                "why_it_matters": post.get("relevance_reason", ""),
                "suggested_action": post.get("suggested_action", ""),
                "raw": post.get("raw_analysis", {}),
            },
        }, ensure_ascii=False)
    )
    body = json.dumps({
        "model": AI_MODEL,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": "You are a precise B2B post analyst for Boundera."},
            {"role": "user", "content": prompt},
        ],
    }).encode("utf-8")
    req = request.Request(
        f"{LLM_API_BASE}/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {LLM_API_KEY}",
            "Content-Type": "application/json; charset=utf-8",
        },
        method="POST",
    )
    with request.urlopen(req, timeout=_LLM_TIMEOUT_SECONDS) as response:
        data = json.loads(response.read().decode("utf-8"))
    summary = (
        ((data.get("choices") or [{}])[0].get("message") or {}).get("content")
        or ""
    ).strip()
    if not summary:
        raise RuntimeError("No post summary returned")
    return summary


def handle_post_context(responder, body: str, *, connect_factory, slack_api, **_kwargs) -> None:
    try:
        data = parse_post_context_action(body)
        with connect_factory() as conn:
            context = fetch_feed_post_context(conn, data["post_id"])
        open_post_context_modal(
            slack_api=slack_api,
            trigger_id=data["trigger_id"],
            context=context,
        )
    except (ValueError, KeyError, json.JSONDecodeError):
        responder._respond_text(400, "malformed feed post context action")
        return
    except Exception:
        responder._respond_text(500, "feed post context modal error")
        return
    responder._respond_text(200, "")


def handle_post_context_ai(
    responder,
    body: str,
    *,
    connect_factory,
    slack_api,
    **_kwargs,
) -> None:
    try:
        data = parse_post_context_action(body)
        with connect_factory() as conn:
            context = fetch_feed_post_context(conn, data["post_id"])
    except (ValueError, KeyError, json.JSONDecodeError):
        responder._respond_text(400, "malformed feed post summary action")
        return
    except Exception:
        responder._respond_text(500, "feed post context error")
        return

    try:
        update_post_context_modal(
            slack_api=slack_api,
            view_id=data["view_id"],
            view_hash=data["view_hash"],
            context=context,
            loading=True,
        )
    except Exception:
        pass

    try:
        summary = generate_ai_post_summary(context)
        update_post_context_modal(
            slack_api=slack_api,
            view_id=data["view_id"],
            context=context,
            ai_summary=summary,
        )
    except Exception as exc:
        try:
            update_post_context_modal(
                slack_api=slack_api,
                view_id=data["view_id"],
                context=context,
                ai_error=str(exc),
            )
        except Exception:
            responder._respond_text(500, "feed post context modal error")
            return
    responder._respond_text(200, "")


def _post_context_view(
    *,
    context: dict,
    ai_summary: str = "",
    ai_error: str = "",
    loading: bool = False,
) -> dict:
    return {
        "type": "modal",
        "callback_id": POST_CONTEXT_MODAL_CALLBACK_ID,
        "private_metadata": json.dumps(
            {"post_id": context["post"]["id"]}, separators=(",", ":")
        ),
        "title": {"type": "plain_text", "text": "Post context"},
        "close": {"type": "plain_text", "text": "Close"},
        "blocks": render_post_context_blocks(
            context,
            ai_summary=ai_summary,
            ai_error=ai_error,
            loading=loading,
        ),
    }


def _analysis_fields(post: dict) -> list[dict]:
    topics = ", ".join(str(topic) for topic in post.get("topics") or []) or "unknown"
    values = [
        ("Intent", post.get("intent") or "unknown"),
        ("Audience", post.get("audience") or "unknown"),
        ("Topics", topics),
        ("Posted", _format_timestamp(post.get("posted_at")) or "unknown"),
        ("First seen", _format_timestamp(post.get("first_seen_at")) or "unknown"),
        ("Last seen", _format_timestamp(post.get("last_seen_at")) or "unknown"),
        ("Analyzed", _format_timestamp(post.get("analyzed_at")) or "not analyzed"),
    ]
    return [
        {
            "type": "mrkdwn",
            "text": f"*{label}:* {_slack_escape(_compact(str(value), 500))}",
        }
        for label, value in values
    ]


def _format_timestamp(value) -> str:
    if not value:
        return ""
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d %H:%M %Z").strip()
    return str(value)


def _split_slack_text(value: str, *, limit: int = _SECTION_TEXT_LIMIT) -> list[str]:
    """Escape and split text without dropping characters or breaking entities."""
    raw = (value or "").strip()
    if not raw:
        raw = "(no text)"
    chunks: list[str] = []
    current: list[str] = []
    current_length = 0
    for character in raw:
        escaped = _slack_escape(character)
        if current and current_length + len(escaped) > limit:
            chunks.append("".join(current))
            current = []
            current_length = 0
        current.append(escaped)
        current_length += len(escaped)
    if current:
        chunks.append("".join(current))
    return chunks


def _decode_body(body: str) -> dict:
    fields = parse_qs(body)
    raw = (fields.get("payload") or [None])[0]
    if not raw:
        raise ValueError("no payload field")
    return json.loads(raw)


def _compact(value: str, limit: int) -> str:
    text = " ".join((value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _slack_escape(value: str) -> str:
    return (value or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
