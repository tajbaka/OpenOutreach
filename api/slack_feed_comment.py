"""Slack workflow for human-approved public LinkedIn feed comments.

This module is deliberately independent from Django and from the existing
manual-reply implementation in ``api.slack_enrich``.  The shared endpoint only
registers these intents and delegates to the handlers below.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from urllib import request
from urllib.parse import parse_qs

from psycopg.types.json import Jsonb

logger = logging.getLogger(__name__)

COMMENT_ACTION_ID = "linkedin_feed_comment_button"
OPEN_POST_ACTION_ID = "linkedin_feed_open_post_button"
COMMENT_DRAFT_ACTION_ID = "linkedin_feed_comment_draft_button"
COMMENT_CANCEL_ACTION_ID = "linkedin_feed_comment_cancel_button"
COMMENT_MODAL_CALLBACK_ID = "linkedin_feed_comment_modal"
COMMENT_BODY_ACTION_ID = "linkedin_feed_comment_body"
COMMENT_SENDER_ACTION_ID = "linkedin_feed_comment_sender"

INTENT_COMMENT_BUTTON = "feed_comment_button"
INTENT_OPEN_POST = "feed_post_open"
INTENT_COMMENT_DRAFT = "feed_comment_draft"
INTENT_COMMENT_CANCEL = "feed_comment_cancel"
INTENT_COMMENT_SUBMISSION = "feed_comment_submission"

INTENT_BY_ACTION_ID = {
    COMMENT_ACTION_ID: INTENT_COMMENT_BUTTON,
    OPEN_POST_ACTION_ID: INTENT_OPEN_POST,
    COMMENT_DRAFT_ACTION_ID: INTENT_COMMENT_DRAFT,
    COMMENT_CANCEL_ACTION_ID: INTENT_COMMENT_CANCEL,
}
VIEW_SUBMISSION_INTENTS = {
    COMMENT_MODAL_CALLBACK_ID: INTENT_COMMENT_SUBMISSION,
}
HANDLER_BY_INTENT = {
    INTENT_COMMENT_BUTTON: "_handle_feed_comment_button",
    INTENT_OPEN_POST: "_handle_feed_post_open",
    INTENT_COMMENT_DRAFT: "_handle_feed_comment_draft",
    INTENT_COMMENT_CANCEL: "_handle_feed_comment_cancel",
    INTENT_COMMENT_SUBMISSION: "_handle_feed_comment_submission",
}


def handle_post_open(responder, _body: str, **_kwargs) -> None:
    """Acknowledge Slack's interaction event for a URL-only button."""
    responder._respond_text(200, "")


_LLM_TIMEOUT_SECONDS = 12
_METADATA_LIMIT = 2800
_MODAL_TEXT_LIMIT = 2800

LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_API_BASE = (os.environ.get("LLM_API_BASE") or "https://api.openai.com/v1").rstrip("/")
AI_MODEL = os.environ.get("AI_MODEL", "")


@dataclass(frozen=True)
class FeedCommentEnqueueResult:
    task_id: int
    created: bool


def parse_comment_button(body: str) -> dict:
    """Extract the feed post and source-message coordinates from a button click."""
    payload = _decode_body(body)
    action = _first_action(payload, COMMENT_ACTION_ID)
    value = json.loads(action.get("value") or "{}")
    message = payload.get("message") or {}
    channel = payload.get("channel") or {}
    return {
        "post_id": int(value["post_id"]),
        "trigger_id": payload.get("trigger_id") or "",
        "response_url": payload.get("response_url") or "",
        "channel_id": channel.get("id") or "",
        "message_ts": message.get("ts") or "",
    }


def parse_comment_draft_button(body: str) -> dict:
    """Extract the current comment and selected sender from an open modal."""
    payload = _decode_body(body)
    _first_action(payload, COMMENT_DRAFT_ACTION_ID)
    view = payload.get("view") or {}
    metadata = json.loads(view.get("private_metadata") or "{}")
    state = ((view.get("state") or {}).get("values") or {})
    return {
        "post_id": int(metadata["post_id"]),
        "view_id": view.get("id") or "",
        "view_hash": view.get("hash") or "",
        "current_comment": _state_text(state, COMMENT_BODY_ACTION_ID),
        "selected_sender_key": _selected_sender_key(state, metadata),
        "metadata": metadata,
    }


def parse_comment_modal_submission(body: str) -> dict:
    """Extract a validated daemon-task payload from the modal submission."""
    payload = _decode_body(body)
    if payload.get("type") != "view_submission":
        raise ValueError("not a view_submission payload")
    view = payload.get("view") or {}
    if view.get("callback_id") != COMMENT_MODAL_CALLBACK_ID:
        raise ValueError("not a feed comment modal")

    metadata = json.loads(view.get("private_metadata") or "{}")
    state = ((view.get("state") or {}).get("values") or {})
    message = _state_text(state, COMMENT_BODY_ACTION_ID)
    if not message:
        raise ValueError("empty feed comment body")
    sender = _sender_for_key(metadata, _selected_sender_key(state, metadata))
    user = payload.get("user") or {}
    return {
        "post_id": int(metadata["post_id"]),
        "operator": sender["operator"],
        "account_username": sender.get("account_username") or "",
        "message": message,
        "slack_channel_id": metadata.get("channel_id") or "",
        "slack_message_ts": metadata.get("message_ts") or "",
        "slack_response_url": metadata.get("response_url") or "",
        "slack_user_id": user.get("id") or "",
    }


def parse_comment_cancel_button(body: str) -> dict:
    """Extract the pending task id from its threaded Slack status message."""
    payload = _decode_body(body)
    action = _first_action(payload, COMMENT_CANCEL_ACTION_ID)
    value = json.loads(action.get("value") or "{}")
    return {"task_id": int(value["task_id"])}


def fetch_feed_comment_context(conn, post_id: int) -> dict:
    """Read one collected post and its eligible sender observations using SQL."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, activity_urn, post_url, author_name, author_headline, "
            "author_profile_url, post_text, intent, audience, topics, "
            "relevance_reason, suggested_action "
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
            "intent": row[7] or "",
            "audience": row[8] or "",
            "topics": row[9] or [],
            "relevance_reason": row[10] or "",
            "suggested_action": row[11] or "",
        }

        cur.execute(
            "SELECT \"operator\", account_username "
            "FROM linkedin_linkedinfeedobservation "
            "WHERE post_id = %s "
            "ORDER BY \"operator\", account_username",
            (int(post_id),),
        )
        observation_rows = cur.fetchall()

    senders: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for operator, account_username in observation_rows:
        operator = (operator or "").strip()
        account_username = (account_username or "").strip()
        if not operator:
            continue
        key = (operator, account_username)
        if key in seen:
            continue
        seen.add(key)
        senders.append({
            "key": str(len(senders)),
            "operator": operator,
            "account_username": account_username,
            "label": _sender_label(operator, account_username),
        })
    if not senders:
        raise ValueError(f"feed post {post_id} has no sender observations")
    return {"post": post, "senders": senders}


def render_feed_comment_modal_blocks(
    context: dict,
    *,
    metadata: dict,
    initial_comment: str = "",
    selected_sender_key: str = "",
    loading: str = "",
    draft_error: str = "",
) -> list[dict]:
    """Render post context, sender control, comment editor, and draft action."""
    post = context["post"]
    senders = context["senders"]
    selected_sender_key = selected_sender_key or metadata.get("default_sender_key") or senders[0]["key"]
    author = _slack_escape(post.get("author_name") or "Unknown author")
    profile_url = post.get("author_profile_url") or ""
    author_md = f"<{profile_url}|{author}>" if profile_url else author
    headline = _compact(post.get("author_headline") or "", 240)
    excerpt = _compact(post.get("post_text") or "", 800)
    why = _compact(post.get("relevance_reason") or "No reason saved.", 600)
    suggested = _compact(post.get("suggested_action") or "Review the post.", 500)

    blocks: list[dict] = [{
        "type": "section",
        "block_id": "feed_comment_post_header",
        "text": {
            "type": "mrkdwn",
            "text": f"*Comment on {author_md}*" + (f"\n{_slack_escape(headline)}" if headline else ""),
        },
    }]
    blocks.extend([
        {
            "type": "section",
            "block_id": "feed_comment_post_excerpt",
            "text": {"type": "mrkdwn", "text": f"*Post*\n>{_slack_escape(excerpt or '(no text)')}"},
        },
        {
            "type": "section",
            "block_id": "feed_comment_reason",
            "text": {"type": "mrkdwn", "text": f"*Why it matters*\n{_slack_escape(why)}"},
        },
        {
            "type": "section",
            "block_id": "feed_comment_suggested_action",
            "text": {"type": "mrkdwn", "text": f"*Suggested action*\n{_slack_escape(suggested)}"},
        },
    ])
    if post.get("post_url"):
        blocks.append({
            "type": "section",
            "block_id": "feed_comment_post_link",
            "text": {"type": "mrkdwn", "text": f"<{post['post_url']}|Open LinkedIn post>"},
        })

    if len(senders) > 1:
        options = [_sender_option(sender) for sender in senders]
        initial_option = next(
            (option for option in options if option["value"] == selected_sender_key),
            options[0],
        )
        blocks.append({
            "type": "input",
            "block_id": "feed_comment_sender_input",
            "label": {"type": "plain_text", "text": "Comment as"},
            "element": {
                "type": "static_select",
                "action_id": COMMENT_SENDER_ACTION_ID,
                "options": options,
                "initial_option": initial_option,
            },
        })
    else:
        blocks.append({
            "type": "context",
            "block_id": "feed_comment_sender_context",
            "elements": [{
                "type": "mrkdwn",
                "text": f"*Comment as:* {_slack_escape(senders[0]['label'])}",
            }],
        })

    input_element = {
        "type": "plain_text_input",
        "action_id": COMMENT_BODY_ACTION_ID,
        "multiline": True,
        "placeholder": {"type": "plain_text", "text": "Write a public LinkedIn comment..."},
    }
    if initial_comment.strip():
        input_element["initial_value"] = initial_comment.strip()[:_MODAL_TEXT_LIMIT]
    blocks.append({
        "type": "input",
        "block_id": "feed_comment_message",
        "label": {"type": "plain_text", "text": "Comment"},
        "element": input_element,
    })

    if loading:
        blocks.append({
            "type": "section",
            "block_id": "feed_comment_draft_loading",
            "text": {"type": "mrkdwn", "text": f":hourglass_flowing_sand: *{_slack_escape(loading)}*"},
        })
    elif draft_error:
        blocks.append({
            "type": "section",
            "block_id": "feed_comment_draft_error",
            "text": {"type": "mrkdwn", "text": f":warning: *Draft failed* - `{_slack_escape(_compact(draft_error, 240))}`"},
        })

    blocks.append({
        "type": "actions",
        "block_id": "feed_comment_actions",
        "elements": [{
            "type": "button",
            "action_id": COMMENT_DRAFT_ACTION_ID,
            "text": {"type": "plain_text", "text": "Draft comment"},
            "value": json.dumps({"post_id": post["id"]}, separators=(",", ":")),
        }],
    })
    return blocks


def open_feed_comment_modal(*, slack_api, trigger_id: str, context: dict, metadata: dict) -> None:
    slack_api("views.open", {
        "trigger_id": trigger_id,
        "view": _feed_comment_view(context=context, metadata=metadata),
    })


def update_feed_comment_modal(
    *,
    slack_api,
    view_id: str,
    context: dict,
    metadata: dict,
    view_hash: str = "",
    initial_comment: str = "",
    selected_sender_key: str = "",
    loading: str = "",
    draft_error: str = "",
) -> None:
    payload = {
        "view_id": view_id,
        "view": _feed_comment_view(
            context=context,
            metadata=metadata,
            initial_comment=initial_comment,
            selected_sender_key=selected_sender_key,
            loading=loading,
            draft_error=draft_error,
        ),
    }
    if view_hash:
        payload["hash"] = view_hash
    slack_api("views.update", payload)


def enqueue_feed_comment_task(conn, payload: dict) -> FeedCommentEnqueueResult:
    """Create one sender task plus its queued ledger row, with atomic dedup."""
    post_id = int(payload["post_id"])
    operator = (payload.get("operator") or "").strip()
    account_username = (payload.get("account_username") or "").strip()
    message = (payload.get("message") or "").strip()
    if not operator or not message:
        raise ValueError("feed comment requires operator and message")

    dedup_key = f"feed-comment:{post_id}:{operator}:{' '.join(message.lower().split())}"
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (dedup_key,))
        cur.execute(
            "SELECT id FROM linkedin_task "
            "WHERE task_type = 'feed_comment' "
            "AND status IN ('pending', 'running') "
            "AND (payload->>'post_id')::int = %s "
            "AND payload->>'operator' = %s "
            "AND payload->>'message' = %s "
            "ORDER BY id LIMIT 1",
            (post_id, operator, message),
        )
        row = cur.fetchone()
        if row is not None:
            conn.commit()
            return FeedCommentEnqueueResult(task_id=int(row[0]), created=False)

        task_payload = {
            "post_id": post_id,
            "operator": operator,
            "account_username": account_username,
            "message": message,
            "slack_channel_id": payload.get("slack_channel_id", ""),
            "slack_message_ts": payload.get("slack_message_ts", ""),
            "slack_response_url": payload.get("slack_response_url", ""),
            "slack_user_id": payload.get("slack_user_id", ""),
        }
        cur.execute(
            "INSERT INTO linkedin_task "
            "(task_type, status, scheduled_at, payload, error, created_at) "
            "VALUES ('feed_comment', 'pending', now(), %s, '', now()) "
            "RETURNING id",
            (Jsonb(task_payload),),
        )
        task_id = int(cur.fetchone()[0])
        cur.execute(
            "INSERT INTO linkedin_linkedinfeedcomment "
            "(post_id, task_id, \"operator\", account_username, comment_text, status, "
            "slack_channel_id, slack_message_ts, slack_response_url, slack_user_id, "
            "submit_attempted_at, commented_at, error, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, 'queued', %s, %s, %s, %s, "
            "NULL, NULL, '', now(), now())",
            (
                post_id,
                task_id,
                operator,
                account_username,
                message,
                task_payload["slack_channel_id"],
                task_payload["slack_message_ts"],
                task_payload["slack_response_url"],
                task_payload["slack_user_id"],
            ),
        )
    conn.commit()
    return FeedCommentEnqueueResult(task_id=task_id, created=True)


def save_feed_comment_status_message(conn, task_id: int, message_ts: str) -> None:
    """Attach the cancellable Slack thread-status message to the queued task."""
    value = (message_ts or "").strip()
    if not value:
        return
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE linkedin_task SET payload = payload || %s "
            "WHERE id = %s AND task_type = 'feed_comment'",
            (Jsonb({"slack_status_message_ts": value}), int(task_id)),
        )
    conn.commit()


def cancel_feed_comment_task(conn, task_id: int) -> bool:
    """Delete a feed-comment task and close its ledger only while pending."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM linkedin_task "
            "WHERE id = %s AND task_type = 'feed_comment' AND status = 'pending' "
            "FOR UPDATE",
            (int(task_id),),
        )
        if cur.fetchone() is None:
            conn.commit()
            return False
        cur.execute(
            "UPDATE linkedin_linkedinfeedcomment "
            "SET task_id = NULL, status = 'skipped', error = %s, updated_at = now() "
            "WHERE task_id = %s",
            ("Cancelled from Slack before the sender claimed the task.", int(task_id)),
        )
        cur.execute("DELETE FROM linkedin_task WHERE id = %s", (int(task_id),))
    conn.commit()
    return True


def render_feed_comment_thread_status(
    status_text: str,
    *,
    cancel_task_id: int | None = None,
) -> list[dict]:
    """Render a standalone thread status without touching the source alert."""
    status: dict = {
        "type": "section",
        "text": {"type": "mrkdwn", "text": status_text},
    }
    if cancel_task_id is not None:
        status["accessory"] = {
            "type": "button",
            "action_id": COMMENT_CANCEL_ACTION_ID,
            "text": {"type": "plain_text", "text": "Cancel queued comment"},
            "style": "danger",
            "value": json.dumps({"task_id": int(cancel_task_id)}, separators=(",", ":")),
            "confirm": {
                "title": {"type": "plain_text", "text": "Cancel comment?"},
                "text": {
                    "type": "mrkdwn",
                    "text": "This removes the queued public comment if its sender has not started.",
                },
                "confirm": {"type": "plain_text", "text": "Cancel comment"},
                "deny": {"type": "plain_text", "text": "Keep queued"},
            },
        }
    return [status]


def generate_ai_feed_comment(context: dict, *, current_comment: str = "") -> str:
    """Generate one concise public comment, separate from the DM reply prompt."""
    if not LLM_API_KEY or not AI_MODEL:
        raise RuntimeError("LLM_API_KEY or AI_MODEL is not configured on Vercel")
    post = context["post"]
    prompt = (
        "Draft one public LinkedIn comment for the post below. Return only the comment. "
        "Keep it concise, useful, conversational, and non-salesy. Add a practitioner insight "
        "or a thoughtful question when the post supports one. Do not ask for a meeting, make a "
        "hard pitch, invent facts, or overclaim Boundera capabilities. Mention Boundera only when "
        "it is directly useful to the discussion. Do not use hashtags.\n\n"
        "Boundera context: Boundera helps software vendors reduce manual FedRAMP evidence work, "
        "KSI/package readiness friction, gap tracking, remediation ownership, and ongoing monitoring.\n\n"
        + json.dumps({
            "author": post.get("author_name", ""),
            "author_headline": post.get("author_headline", ""),
            "post": post.get("post_text", ""),
            "intent": post.get("intent", ""),
            "audience": post.get("audience", ""),
            "topics": post.get("topics", []),
            "why_it_matters": post.get("relevance_reason", ""),
            "suggested_action": post.get("suggested_action", ""),
            "operator_starting_draft": current_comment,
        }, ensure_ascii=False)
    )
    body = json.dumps({
        "model": AI_MODEL,
        "temperature": 0.5,
        "messages": [
            {"role": "system", "content": "You draft credible public LinkedIn comments for Boundera."},
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
    draft = (
        ((data.get("choices") or [{}])[0].get("message") or {}).get("content")
        or ""
    ).strip()
    if not draft:
        raise RuntimeError("No comment draft returned")
    return draft[:_MODAL_TEXT_LIMIT]


def handle_comment_button(responder, body: str, *, connect_factory, slack_api, **_kwargs) -> None:
    try:
        data = parse_comment_button(body)
        with connect_factory() as conn:
            context = fetch_feed_comment_context(conn, data["post_id"])
        metadata = _comment_metadata(data, context)
        open_feed_comment_modal(
            slack_api=slack_api,
            trigger_id=data["trigger_id"],
            context=context,
            metadata=metadata,
        )
    except (ValueError, KeyError, json.JSONDecodeError):
        responder._respond_text(400, "malformed feed comment action")
        return
    except Exception:
        responder._respond_text(500, "feed comment modal error")
        return
    responder._respond_text(200, "")


def handle_comment_draft(responder, body: str, *, connect_factory, slack_api, **_kwargs) -> None:
    try:
        data = parse_comment_draft_button(body)
        with connect_factory() as conn:
            context = fetch_feed_comment_context(conn, data["post_id"])
    except (ValueError, KeyError, json.JSONDecodeError):
        responder._respond_text(400, "malformed feed comment draft action")
        return
    except Exception:
        responder._respond_text(500, "feed comment context error")
        return

    try:
        update_feed_comment_modal(
            slack_api=slack_api,
            view_id=data["view_id"],
            view_hash=data["view_hash"],
            context=context,
            metadata=data["metadata"],
            initial_comment=data["current_comment"],
            selected_sender_key=data["selected_sender_key"],
            loading="Drafting public comment...",
        )
    except Exception:
        pass

    try:
        draft = generate_ai_feed_comment(
            context,
            current_comment=data["current_comment"],
        )
        update_feed_comment_modal(
            slack_api=slack_api,
            view_id=data["view_id"],
            context=context,
            metadata=data["metadata"],
            initial_comment=draft,
            selected_sender_key=data["selected_sender_key"],
        )
    except Exception as exc:
        try:
            update_feed_comment_modal(
                slack_api=slack_api,
                view_id=data["view_id"],
                context=context,
                metadata=data["metadata"],
                initial_comment=data["current_comment"],
                selected_sender_key=data["selected_sender_key"],
                draft_error=str(exc),
            )
        except Exception:
            responder._respond_text(500, "feed comment modal error")
            return
    responder._respond_text(200, "")


def handle_comment_submission(
    responder,
    body: str,
    *,
    connect_factory,
    slack_api,
    **_kwargs,
) -> None:
    try:
        payload = parse_comment_modal_submission(body)
    except (ValueError, KeyError, json.JSONDecodeError):
        responder._respond_json({
            "response_action": "errors",
            "errors": {"feed_comment_message": "Write a comment before queuing."},
        })
        return

    try:
        with connect_factory() as conn:
            enqueue_result = enqueue_feed_comment_task(conn, payload)
    except Exception:
        responder._respond_text(500, "database error")
        return

    if enqueue_result.created:
        try:
            result = slack_api("chat.postMessage", {
                "channel": payload["slack_channel_id"],
                "thread_ts": payload["slack_message_ts"],
                "text": "LinkedIn comment + Like queued",
                "blocks": render_feed_comment_thread_status(
                    ":hourglass_flowing_sand: *LinkedIn comment + Like queued* - "
                    "the selected sender daemon will apply both shortly.",
                    cancel_task_id=enqueue_result.task_id,
                ),
            })
            status_message_ts = (result.get("ts") or "").strip()
            if status_message_ts:
                with connect_factory() as conn:
                    save_feed_comment_status_message(
                        conn,
                        enqueue_result.task_id,
                        status_message_ts,
                    )
        except Exception:
            logger.exception(
                "Failed to post cancellable Slack feed-comment status for task %s",
                enqueue_result.task_id,
            )

    responder._respond_json({"response_action": "clear"})


def handle_comment_cancel(responder, body: str, *, connect_factory, **_kwargs) -> None:
    try:
        data = parse_comment_cancel_button(body)
    except (ValueError, KeyError, json.JSONDecodeError):
        responder._respond_text(400, "malformed feed comment cancel action")
        return
    try:
        with connect_factory() as conn:
            cancelled = cancel_feed_comment_task(conn, data["task_id"])
    except Exception:
        responder._respond_text(500, "database error")
        return

    if cancelled:
        status = ":no_entry: *LinkedIn feed comment cancelled* - it will not be posted."
        fallback = "LinkedIn feed comment cancelled"
    else:
        status = ":warning: *Could not cancel LinkedIn feed comment* - it may have started posting."
        fallback = "Could not cancel LinkedIn feed comment"
    responder._respond_blocks(
        render_feed_comment_thread_status(status),
        text=fallback,
    )


def _feed_comment_view(
    *,
    context: dict,
    metadata: dict,
    initial_comment: str = "",
    selected_sender_key: str = "",
    loading: str = "",
    draft_error: str = "",
) -> dict:
    return {
        "type": "modal",
        "callback_id": COMMENT_MODAL_CALLBACK_ID,
        "private_metadata": _compact_metadata(metadata),
        "title": {"type": "plain_text", "text": "LinkedIn comment"},
        "submit": {"type": "plain_text", "text": "Queue comment + Like"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": render_feed_comment_modal_blocks(
            context,
            metadata=metadata,
            initial_comment=initial_comment,
            selected_sender_key=selected_sender_key,
            loading=loading,
            draft_error=draft_error,
        ),
    }


def _comment_metadata(data: dict, context: dict) -> dict:
    senders = context["senders"]
    return {
        "post_id": int(data["post_id"]),
        "channel_id": data.get("channel_id") or "",
        "message_ts": data.get("message_ts") or "",
        "response_url": data.get("response_url") or "",
        "senders": senders,
        "default_sender_key": senders[0]["key"],
    }


def _compact_metadata(metadata: dict) -> str:
    value = dict(metadata)
    encoded = _json(value)
    if len(encoded) <= _METADATA_LIMIT:
        return encoded
    value["response_url"] = ""
    encoded = _json(value)
    if len(encoded) > _METADATA_LIMIT:
        raise ValueError("feed comment modal metadata exceeds Slack limit")
    return encoded


def _decode_body(body: str) -> dict:
    raw = (parse_qs(body).get("payload") or [None])[0]
    if not raw:
        raise ValueError("no payload field")
    return json.loads(raw)


def _first_action(payload: dict, expected_action_id: str) -> dict:
    actions = payload.get("actions") or []
    if not actions:
        raise ValueError("no actions in payload")
    action = actions[0]
    if action.get("action_id") != expected_action_id:
        raise ValueError(f"not a {expected_action_id} action")
    return action


def _state_text(state: dict, action_id: str) -> str:
    for block_values in state.values():
        field = block_values.get(action_id)
        if field:
            return (field.get("value") or "").strip()
    return ""


def _selected_sender_key(state: dict, metadata: dict) -> str:
    for block_values in state.values():
        field = block_values.get(COMMENT_SENDER_ACTION_ID)
        if field:
            return ((field.get("selected_option") or {}).get("value") or "").strip()
    return str(metadata.get("default_sender_key") or "")


def _sender_for_key(metadata: dict, key: str) -> dict:
    for sender in metadata.get("senders") or []:
        if str(sender.get("key")) == str(key):
            if not (sender.get("operator") or "").strip():
                break
            return sender
    raise ValueError("no valid feed comment sender selected")


def _sender_label(operator: str, account_username: str) -> str:
    display = "Eddy" if operator == "Chuka" else operator
    if account_username and account_username.lower() not in {operator.lower(), display.lower()}:
        return f"{display} ({account_username})"[:75]
    return display[:75]


def _sender_option(sender: dict) -> dict:
    return {
        "text": {"type": "plain_text", "text": (sender.get("label") or sender["operator"])[:75]},
        "value": str(sender["key"]),
    }


def _compact(value: str, limit: int) -> str:
    text = " ".join((value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _slack_escape(value: str) -> str:
    return (value or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _json(value: dict) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
