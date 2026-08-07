"""Slack workflow for sender-scoped, standalone LinkedIn feed Likes."""
from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.parse import parse_qs

from psycopg.types.json import Jsonb

from api.slack_feed_common import (
    best_effort_source_update,
    decode_source_blocks,
    encode_source_blocks,
)
from linkedin.feed_like_slack_status import render_feed_like_status_blocks

LIKE_ACTION_ID = "linkedin_feed_like_button"
LIKE_MODAL_CALLBACK_ID = "linkedin_feed_like_modal"
LIKE_SENDER_ACTION_ID = "linkedin_feed_like_sender"

INTENT_LIKE_BUTTON = "feed_like_button"
INTENT_LIKE_SUBMISSION = "feed_like_submission"

INTENT_BY_ACTION_ID = {
    LIKE_ACTION_ID: INTENT_LIKE_BUTTON,
}
VIEW_SUBMISSION_INTENTS = {
    LIKE_MODAL_CALLBACK_ID: INTENT_LIKE_SUBMISSION,
}
HANDLER_BY_INTENT = {
    INTENT_LIKE_BUTTON: "_handle_feed_like_button",
    INTENT_LIKE_SUBMISSION: "_handle_feed_like_submission",
}

_METADATA_LIMIT = 2800


@dataclass(frozen=True)
class FeedLikeEnqueueResult:
    task_id: int
    created: bool


def parse_like_button(body: str) -> dict:
    payload = _decode_body(body)
    action = _first_action(payload, LIKE_ACTION_ID)
    value = json.loads(action.get("value") or "{}")
    message = payload.get("message") or {}
    channel = payload.get("channel") or {}
    blocks = message.get("blocks") or []
    if not blocks or not all(isinstance(block, dict) for block in blocks):
        raise ValueError("feed Like source blocks are missing")
    return {
        "post_id": int(value["post_id"]),
        "trigger_id": payload.get("trigger_id") or "",
        "response_url": payload.get("response_url") or "",
        "channel_id": channel.get("id") or "",
        "message_ts": message.get("ts") or "",
        "blocks": blocks,
    }


def parse_like_submission(body: str) -> dict:
    payload = _decode_body(body)
    if payload.get("type") != "view_submission":
        raise ValueError("not a view_submission payload")
    view = payload.get("view") or {}
    if view.get("callback_id") != LIKE_MODAL_CALLBACK_ID:
        raise ValueError("not a feed Like modal")
    metadata = json.loads(view.get("private_metadata") or "{}")
    state = ((view.get("state") or {}).get("values") or {})
    sender = _sender_for_key(metadata, _selected_sender_key(state, metadata))
    user = payload.get("user") or {}
    return {
        "post_id": int(metadata["post_id"]),
        "operator": sender["operator"],
        "account_username": sender.get("account_username") or "",
        "slack_channel_id": metadata.get("channel_id") or "",
        "slack_message_ts": metadata.get("message_ts") or "",
        "slack_response_url": metadata.get("response_url") or "",
        "slack_user_id": user.get("id") or "",
        "slack_blocks": decode_source_blocks(metadata.get("source_blocks") or ""),
    }


def fetch_feed_like_context(conn, post_id: int) -> dict:
    """Read one post and the sender accounts that observed it."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, activity_urn, post_url, author_name, author_headline, "
            "author_profile_url, post_text "
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
        }
        cur.execute(
            "SELECT \"operator\", account_username "
            "FROM linkedin_linkedinfeedobservation "
            "WHERE post_id = %s "
            "ORDER BY \"operator\", account_username",
            (int(post_id),),
        )
        rows = cur.fetchall()

    senders: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for operator, account_username in rows:
        operator = (operator or "").strip()
        account_username = (account_username or "").strip()
        if not operator or (operator, account_username) in seen:
            continue
        seen.add((operator, account_username))
        senders.append({
            "key": str(len(senders)),
            "operator": operator,
            "account_username": account_username,
            "label": _sender_label(operator, account_username),
        })
    if not senders:
        raise ValueError(f"feed post {post_id} has no sender observations")
    return {"post": post, "senders": senders}


def render_feed_like_modal_blocks(context: dict, *, metadata: dict) -> list[dict]:
    post = context["post"]
    senders = context["senders"]
    author = _slack_escape(post.get("author_name") or "Unknown author")
    profile_url = post.get("author_profile_url") or ""
    author_md = f"<{profile_url}|{author}>" if profile_url else author
    excerpt = _compact(post.get("post_text") or "(no text)", 700)
    blocks: list[dict] = [
        {
            "type": "section",
            "block_id": "feed_like_post",
            "text": {
                "type": "mrkdwn",
                "text": f"*Like post by {author_md}*\n>{_slack_escape(excerpt)}",
            },
        },
    ]
    if len(senders) > 1:
        options = [_sender_option(sender) for sender in senders]
        blocks.append({
            "type": "input",
            "block_id": "feed_like_sender_input",
            "label": {"type": "plain_text", "text": "Like as"},
            "element": {
                "type": "static_select",
                "action_id": LIKE_SENDER_ACTION_ID,
                "options": options,
                "initial_option": options[0],
            },
        })
    else:
        blocks.append({
            "type": "context",
            "block_id": "feed_like_sender_context",
            "elements": [{
                "type": "mrkdwn",
                "text": f"*Like as:* {_slack_escape(senders[0]['label'])}",
            }],
        })
    blocks.append({
        "type": "context",
        "block_id": "feed_like_safety",
        "elements": [{
            "type": "mrkdwn",
            "text": "An existing Like or other reaction will not be toggled off.",
        }],
    })
    return blocks


def open_feed_like_modal(*, slack_api, trigger_id: str, context: dict, metadata: dict) -> None:
    slack_api("views.open", {
        "trigger_id": trigger_id,
        "view": {
            "type": "modal",
            "callback_id": LIKE_MODAL_CALLBACK_ID,
            "private_metadata": _compact_metadata(metadata),
            "title": {"type": "plain_text", "text": "Like on LinkedIn"},
            "submit": {"type": "plain_text", "text": "Queue Like"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": render_feed_like_modal_blocks(context, metadata=metadata),
        },
    })


def enqueue_feed_like_task(conn, payload: dict) -> FeedLikeEnqueueResult:
    post_id = int(payload["post_id"])
    operator = (payload.get("operator") or "").strip()
    if not operator:
        raise ValueError("feed Like requires an operator")
    dedup_key = f"feed-like:{post_id}:{operator}"
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (dedup_key,))
        cur.execute(
            "SELECT id FROM linkedin_task "
            "WHERE task_type = 'feed_like' "
            "AND status IN ('pending', 'running') "
            "AND (payload->>'post_id')::int = %s "
            "AND payload->>'operator' = %s "
            "ORDER BY id LIMIT 1",
            (post_id, operator),
        )
        row = cur.fetchone()
        if row is not None:
            conn.commit()
            return FeedLikeEnqueueResult(task_id=int(row[0]), created=False)

        task_payload = {
            "post_id": post_id,
            "operator": operator,
            "account_username": payload.get("account_username") or "",
            "slack_channel_id": payload.get("slack_channel_id") or "",
            "slack_message_ts": payload.get("slack_message_ts") or "",
            "slack_response_url": payload.get("slack_response_url") or "",
            "slack_user_id": payload.get("slack_user_id") or "",
            "slack_blocks": payload.get("slack_blocks") or [],
        }
        cur.execute(
            "INSERT INTO linkedin_task "
            "(task_type, status, scheduled_at, payload, error, created_at) "
            "VALUES ('feed_like', 'pending', now(), %s, '', now()) "
            "RETURNING id",
            (Jsonb(task_payload),),
        )
        task_id = int(cur.fetchone()[0])
    conn.commit()
    return FeedLikeEnqueueResult(task_id=task_id, created=True)


def handle_like_button(responder, body: str, *, connect_factory, slack_api, **_kwargs) -> None:
    try:
        data = parse_like_button(body)
        with connect_factory() as conn:
            context = fetch_feed_like_context(conn, data["post_id"])
        metadata = _like_metadata(data, context)
        open_feed_like_modal(
            slack_api=slack_api,
            trigger_id=data["trigger_id"],
            context=context,
            metadata=metadata,
        )
    except (ValueError, KeyError, json.JSONDecodeError):
        responder._respond_text(400, "malformed feed Like action")
        return
    except Exception:
        responder._respond_text(500, "feed Like modal error")
        return
    responder._respond_text(200, "")


def handle_like_submission(
    responder,
    body: str,
    *,
    connect_factory,
    slack_api,
    post_response_url,
    **_kwargs,
) -> None:
    try:
        payload = parse_like_submission(body)
    except (ValueError, KeyError, json.JSONDecodeError):
        responder._respond_text(400, "malformed feed Like submission")
        return
    try:
        with connect_factory() as conn:
            enqueue_feed_like_task(conn, payload)
    except Exception:
        responder._respond_text(500, "database error")
        return

    queued_blocks = render_feed_like_status_blocks(
        payload["slack_blocks"],
        ":hourglass_flowing_sand: *LinkedIn Like queued* - "
        "the selected sender daemon will apply it shortly.",
        suffix="queued",
    )
    best_effort_source_update(
        payload=payload,
        blocks=queued_blocks,
        text="LinkedIn Like queued",
        slack_api=slack_api,
        post_response_url=post_response_url,
        workflow="feed Like",
    )
    responder._respond_json({"response_action": "clear"})


def _like_metadata(data: dict, context: dict) -> dict:
    senders = context["senders"]
    return {
        "post_id": int(data["post_id"]),
        "channel_id": data.get("channel_id") or "",
        "message_ts": data.get("message_ts") or "",
        "response_url": data.get("response_url") or "",
        "source_blocks": encode_source_blocks(data.get("blocks") or []),
        "senders": senders,
        "default_sender_key": senders[0]["key"],
    }


def _compact_metadata(metadata: dict) -> str:
    value = dict(metadata)
    encoded = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    if len(encoded) <= _METADATA_LIMIT:
        return encoded
    value["response_url"] = ""
    encoded = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    if len(encoded) > _METADATA_LIMIT:
        raise ValueError("feed Like modal metadata exceeds Slack limit")
    return encoded


def _selected_sender_key(state: dict, metadata: dict) -> str:
    for block_values in state.values():
        field = block_values.get(LIKE_SENDER_ACTION_ID)
        if field:
            return ((field.get("selected_option") or {}).get("value") or "").strip()
    return str(metadata.get("default_sender_key") or "")


def _sender_for_key(metadata: dict, key: str) -> dict:
    for sender in metadata.get("senders") or []:
        if str(sender.get("key")) == str(key):
            return sender
    raise ValueError("no valid feed Like sender selected")


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


def _compact(value: str, limit: int) -> str:
    text = " ".join((value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _slack_escape(value: str) -> str:
    return (value or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
