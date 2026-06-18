"""Slack interaction handler — enrichment picks and manual LinkedIn replies.

Deployed as a Vercel serverless Python function. Slack POSTs an interaction
payload here when the operator picks a provider from the "📞 Get phone
number" select menu or clicks "Reply on LinkedIn" on an inbound-reply
notification. The function verifies the Slack request signature, then either
INSERTs an enrich_phone/manual_reply Task into Neon or opens a Slack modal.
The Task table is the entire contract between this function and the daemon;
they never talk directly.

The function never imports Django: it talks to Neon with raw psycopg so the
Vercel deploy stays small. verify_signature / parse_interaction / enqueue_task
are pure, importable units — exercised by tests/test_slack_enrich.py.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from http.server import BaseHTTPRequestHandler
from urllib import request
from urllib.error import URLError
from urllib.parse import parse_qs

import psycopg
from psycopg.types.json import Jsonb

SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET", "")
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_API_BASE = (os.environ.get("LLM_API_BASE") or "https://api.openai.com/v1").rstrip("/")
AI_MODEL = os.environ.get("AI_MODEL", "")
SLACK_API_BASE = "https://slack.com/api"

# Slack rejects interactions older than 5 minutes; we mirror that as a
# replay guard on our side.
_MAX_SKEW_SECONDS = 60 * 5

# The status line we add/update after a pick carries the set of requested
# providers in its block_id ("enrich_status:bettercontact,leadmagic") — a
# machine-readable place to accumulate state across picks, since Slack echoes
# the message blocks back on the next interaction.
_STATUS_PREFIX = "enrich_status"
_REPLY_STATUS_PREFIX = "reply_status"
_ENRICH_PHONE_ACTION_ID = "enrich_phone_select"
_REPLY_ACTION_ID = "linkedin_reply_button"
_REPLY_CANCEL_ACTION_ID = "linkedin_reply_cancel_button"
_REPLY_MODAL_CALLBACK_ID = "linkedin_reply_modal"
_REPLY_BODY_ACTION_ID = "linkedin_reply_body"
_LEAD_CONTEXT_ACTION_ID = "linkedin_lead_context_button"
_LEAD_CONTEXT_AI_ACTION_ID = "linkedin_lead_context_ai_button"
_LEAD_CONTEXT_DRAFT_ACTION_ID = "linkedin_lead_context_draft_button"
_LEAD_CONTEXT_MODAL_CALLBACK_ID = "linkedin_lead_context_modal"
_THREAD_PREVIEW_LIMIT = 8
_THREAD_MESSAGE_LIMIT = 320
_THREAD_SECTION_LIMIT = 2800
_CONTEXT_MESSAGE_LIMIT = 6
_LLM_TIMEOUT_SECONDS = 8

_INTENT_REPLY_SUBMISSION = "reply_submission"
_INTENT_ENRICH_PHONE = "enrich_phone"
_INTENT_REPLY_BUTTON = "reply_button"
_INTENT_REPLY_CANCEL = "reply_cancel"
_INTENT_LEAD_CONTEXT = "lead_context"
_INTENT_LEAD_CONTEXT_AI = "lead_context_ai"
_INTENT_LEAD_CONTEXT_DRAFT = "lead_context_draft"

_INTENT_BY_ACTION_ID = {
    _ENRICH_PHONE_ACTION_ID: _INTENT_ENRICH_PHONE,
    _REPLY_ACTION_ID: _INTENT_REPLY_BUTTON,
    _REPLY_CANCEL_ACTION_ID: _INTENT_REPLY_CANCEL,
    _LEAD_CONTEXT_ACTION_ID: _INTENT_LEAD_CONTEXT,
    _LEAD_CONTEXT_AI_ACTION_ID: _INTENT_LEAD_CONTEXT_AI,
    _LEAD_CONTEXT_DRAFT_ACTION_ID: _INTENT_LEAD_CONTEXT_DRAFT,
}

_HANDLER_BY_INTENT = {
    _INTENT_REPLY_SUBMISSION: "_handle_reply_submission",
    _INTENT_ENRICH_PHONE: "_handle_enrichment_pick",
    _INTENT_REPLY_BUTTON: "_handle_reply_button",
    _INTENT_REPLY_CANCEL: "_handle_reply_cancel",
    _INTENT_LEAD_CONTEXT: "_handle_lead_context_button",
    _INTENT_LEAD_CONTEXT_AI: "_handle_lead_context_ai",
    _INTENT_LEAD_CONTEXT_DRAFT: "_handle_lead_context_draft",
}


def verify_signature(
    body: str,
    timestamp: str,
    signature: str,
    *,
    secret: str,
    now: float | None = None,
) -> bool:
    """True iff `signature` is a valid Slack v0 HMAC over `body` + `timestamp`.

    Returns False on a missing secret/timestamp/signature or a timestamp more
    than 5 minutes from `now` (replay guard). `now` is injectable for tests.
    """
    if not secret or not timestamp or not signature:
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    current = time.time() if now is None else now
    if abs(current - ts) > _MAX_SKEW_SECONDS:
        return False
    basestring = f"v0:{timestamp}:{body}".encode("utf-8")
    expected = "v0=" + hmac.new(
        secret.encode("utf-8"), basestring, hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def parse_interaction(body: str) -> tuple[int, str, list]:
    """Extract (lead_id, provider, original_blocks) from a Slack block-actions
    POST body.

    Slack sends application/x-www-form-urlencoded with a single `payload`
    field holding URL-encoded JSON. `original_blocks` is the Block Kit list
    of the message the operator interacted with — re-posted (menu and all)
    so the dropdown survives the click. Raises ValueError on anything
    malformed.
    """
    fields = parse_qs(body)
    raw = (fields.get("payload") or [None])[0]
    if not raw:
        raise ValueError("no payload field")
    payload = json.loads(raw)
    actions = payload.get("actions") or []
    if not actions:
        raise ValueError("no actions in payload")
    value = (actions[0].get("selected_option") or {}).get("value")
    if not value or ":" not in value:
        raise ValueError(f"unparseable action value: {value!r}")
    lead_part, provider = value.rsplit(":", 1)
    original_blocks = (payload.get("message") or {}).get("blocks") or []
    return int(lead_part), provider, original_blocks


def decode_slack_payload(body: str) -> dict:
    """Decode Slack's URL-encoded interaction payload."""
    try:
        return json.loads((parse_qs(body).get("payload") or ["{}"])[0])
    except json.JSONDecodeError as exc:
        raise ValueError("malformed interaction") from exc


def interaction_intent(payload: dict) -> str:
    """Return the endpoint intent for a decoded Slack interaction.

    This is the single routing switch for the endpoint. New Slack actions add
    one action-id mapping and one handler entry, rather than adding nested
    conditionals to `handler.do_POST`.
    """
    if payload.get("type") == "view_submission":
        view = payload.get("view") or {}
        if view.get("callback_id") == _REPLY_MODAL_CALLBACK_ID:
            return _INTENT_REPLY_SUBMISSION
        raise ValueError("unsupported view_submission")

    actions = payload.get("actions") or []
    if not actions:
        raise ValueError("no actions in payload")
    action = actions[0]
    action_id = action.get("action_id") or ""
    if action_id in _INTENT_BY_ACTION_ID:
        return _INTENT_BY_ACTION_ID[action_id]
    raise ValueError(f"unsupported Slack action: {action_id!r}")


def render_response_blocks(original_blocks: list, provider: str) -> list:
    """Rebuild the notification's blocks after a provider pick.

    Keeps every original block — crucially the `actions` select menu — so the
    operator can pick another provider on the same message, and accumulates a
    status line listing every provider requested so far. This is what makes
    the menu multi-use instead of one-shot.
    """
    requested: set[str] = set()
    for b in original_blocks:
        bid = b.get("block_id", "")
        if bid.startswith(_STATUS_PREFIX + ":"):
            requested |= {p for p in bid.split(":", 1)[1].split(",") if p}
    requested.add(provider)
    ordered = sorted(requested)
    status = {
        "type": "section",
        "block_id": _STATUS_PREFIX + ":" + ",".join(ordered),
        "text": {
            "type": "mrkdwn",
            "text": ":hourglass_flowing_sand: *Enrichment requested:* "
                    + ", ".join(ordered)
                    + " — results post as they arrive.",
        },
    }
    out: list = []
    inserted = False
    for b in original_blocks:
        if b.get("block_id", "").startswith(_STATUS_PREFIX):
            continue  # drop the old status line; the rebuilt one replaces it
        if b.get("type") == "actions" and not inserted:
            out.append(status)  # status sits just above the still-live menu
            inserted = True
        out.append(b)
    if not inserted:
        out.append(status)
    return out


def render_reply_status_blocks(
    original_blocks: list,
    status_text: str,
    *,
    block_id_suffix: str = "queued",
    cancel_task_id: int | None = None,
) -> list:
    """Return blocks with one manual-reply status line above the actions."""
    status = {
        "type": "section",
        "block_id": f"{_REPLY_STATUS_PREFIX}:{block_id_suffix}",
        "text": {
            "type": "mrkdwn",
            "text": status_text,
        },
    }
    if cancel_task_id is not None:
        status["accessory"] = {
            "type": "button",
            "action_id": _REPLY_CANCEL_ACTION_ID,
            "text": {"type": "plain_text", "text": "Cancel queued reply"},
            "style": "danger",
            "value": json.dumps({"task_id": cancel_task_id}, separators=(",", ":")),
            "confirm": {
                "title": {"type": "plain_text", "text": "Cancel reply?"},
                "text": {
                    "type": "mrkdwn",
                    "text": "This removes the queued LinkedIn reply if it has not started sending.",
                },
                "confirm": {"type": "plain_text", "text": "Cancel reply"},
                "deny": {"type": "plain_text", "text": "Keep queued"},
            },
        }
    out: list = []
    inserted = False
    for b in original_blocks:
        if b.get("block_id", "").startswith(_REPLY_STATUS_PREFIX):
            continue
        if b.get("type") == "actions" and not inserted:
            out.append(status)
            inserted = True
        out.append(b)
    if not inserted:
        out.append(status)
    return out


def _slack_api(method: str, payload: dict) -> dict:
    """Call Slack Web API with the configured bot token."""
    if not SLACK_BOT_TOKEN:
        raise RuntimeError("SLACK_BOT_TOKEN is not configured")
    body = json.dumps(payload).encode("utf-8")
    req = request.Request(
        f"{SLACK_API_BASE}/{method}",
        data=body,
        headers={
            "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
            "Content-Type": "application/json; charset=utf-8",
        },
        method="POST",
    )
    with request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not data.get("ok"):
        raise RuntimeError(f"Slack {method} failed: {data.get('error')}")
    return data


def _post_response_url(response_url: str, payload: dict) -> None:
    """Use Slack's interaction response_url to update the source message."""
    if not response_url:
        raise RuntimeError("response_url missing")
    body = json.dumps(payload).encode("utf-8")
    req = request.Request(
        response_url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with request.urlopen(req, timeout=10) as resp:
        if resp.status >= 400:
            raise RuntimeError(f"Slack response_url returned {resp.status}")


def _compact_metadata(metadata: dict) -> str:
    """Slack private_metadata is small; drop blocks if the payload is large."""
    encoded = json.dumps(metadata, separators=(",", ":"))
    if len(encoded) <= 2800:
        return encoded
    metadata = dict(metadata)
    metadata["blocks"] = []
    return json.dumps(metadata, separators=(",", ":"))


def _slack_escape(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _compact_message(text: str, *, limit: int = _THREAD_MESSAGE_LIMIT) -> str:
    compact = " ".join((text or "").split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3].rstrip() + "..."


def _latest_linkedin_thread_external_id(conn, lead_id: int) -> str:
    """Best-effort legacy fallback for old Slack buttons with no thread id."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT thread_external_id "
            "FROM crm_message "
            "WHERE lead_id = %s AND source = 'linkedin' "
            "AND direction = 'inbound' "
            "AND COALESCE(thread_external_id, '') <> '' "
            "ORDER BY sent_at DESC, id DESC "
            "LIMIT 1",
            (lead_id,),
        )
        row = cur.fetchone()
    return (row[0] if row else "") or ""


def fetch_linkedin_thread_preview(
    conn,
    lead_id: int,
    *,
    thread_external_id: str = "",
    limit: int = _THREAD_PREVIEW_LIMIT,
) -> list[dict]:
    """Fetch recent LinkedIn messages for the manual-reply modal.

    The Vercel function intentionally avoids Django imports, so this uses the
    raw table. When a thread id is available, scope to that exact LinkedIn
    conversation so shared Lead rows across operators do not mix transcripts.
    Returned oldest-first for natural reading.
    """
    thread_external_id = (thread_external_id or "").strip()
    if not thread_external_id:
        thread_external_id = _latest_linkedin_thread_external_id(conn, lead_id)

    with conn.cursor() as cur:
        if thread_external_id:
            cur.execute(
                "SELECT direction, sender, body, sent_at "
                "FROM crm_message "
                "WHERE lead_id = %s AND source = 'linkedin' "
                "AND thread_external_id = %s "
                "ORDER BY sent_at DESC, id DESC "
                "LIMIT %s",
                (lead_id, thread_external_id, limit),
            )
        else:
            cur.execute(
                "SELECT direction, sender, body, sent_at "
                "FROM crm_message "
                "WHERE lead_id = %s AND source = 'linkedin' "
                "ORDER BY sent_at DESC, id DESC "
                "LIMIT %s",
                (lead_id, limit),
            )
        rows = cur.fetchall()
    messages = [
        {
            "direction": row[0] or "",
            "sender": row[1] or "",
            "body": row[2] or "",
            "sent_at": row[3],
        }
        for row in rows
    ]
    return list(reversed(messages))


def render_thread_preview_blocks(messages: list[dict]) -> list[dict]:
    """Render a compact recent-thread transcript for a Slack modal."""
    if not messages:
        return []

    blocks: list[dict] = [
        {
            "type": "section",
            "block_id": "linkedin_thread_preview_header",
            "text": {"type": "mrkdwn", "text": "*Recent LinkedIn thread*"},
        },
    ]

    used = 0
    for msg in messages:
        direction = (msg.get("direction") or "").lower()
        fallback = "Lead" if direction == "inbound" else "Sender"
        speaker = _slack_escape(msg.get("sender") or fallback)
        body = _slack_escape(_compact_message(msg.get("body") or ""))
        if not body:
            body = "_No text body_"
        text = f"*{speaker}*\n\n>{body}"
        used += len(text)
        if used > _THREAD_SECTION_LIMIT:
            blocks.append({
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": "...thread preview truncated"}],
            })
            break
        blocks.append({
            "type": "section",
            "block_id": f"linkedin_thread_preview:{len(blocks)}",
            "text": {"type": "mrkdwn", "text": text},
        })

    blocks.append({"type": "divider"})
    return blocks


def open_reply_modal(
    *,
    trigger_id: str,
    lead_id: int,
    operator: str,
    channel_id: str,
    message_ts: str,
    response_url: str = "",
    original_blocks: list,
    thread_external_id: str = "",
    thread_blocks: list | None = None,
) -> None:
    """Open the Slack modal used to collect a manual LinkedIn reply."""
    metadata = _compact_metadata({
        "lead_id": lead_id,
        "operator": operator,
        "channel_id": channel_id,
        "message_ts": message_ts,
        "response_url": response_url,
        "thread_external_id": thread_external_id or "",
        "blocks": original_blocks,
    })
    _slack_api("views.open", {
        "trigger_id": trigger_id,
        "view": {
            "type": "modal",
            "callback_id": _REPLY_MODAL_CALLBACK_ID,
            "private_metadata": metadata,
            "title": {"type": "plain_text", "text": "LinkedIn reply"},
            "submit": {"type": "plain_text", "text": "Queue reply"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                *(thread_blocks or []),
                {
                    "type": "input",
                    "block_id": "linkedin_reply_message",
                    "label": {"type": "plain_text", "text": "Reply"},
                    "element": {
                        "type": "plain_text_input",
                        "action_id": _REPLY_BODY_ACTION_ID,
                        "multiline": True,
                        "placeholder": {
                            "type": "plain_text",
                            "text": "Type the LinkedIn reply to queue...",
                        },
                    },
                },
            ],
        },
    })


def update_slack_message(*, channel_id: str, message_ts: str, blocks: list, text: str) -> None:
    """Best-effort Slack message update using chat.update."""
    _slack_api("chat.update", {
        "channel": channel_id,
        "ts": message_ts,
        "text": text,
        "blocks": blocks,
    })


def update_slack_view(*, view_id: str, view_hash: str = "", blocks: list, title: str = "Lead context") -> None:
    """Update an already-open Slack modal."""
    payload = {
        "view_id": view_id,
        "view": {
            "type": "modal",
            "callback_id": _LEAD_CONTEXT_MODAL_CALLBACK_ID,
            "title": {"type": "plain_text", "text": title[:24] or "Lead context"},
            "close": {"type": "plain_text", "text": "Close"},
            "blocks": blocks,
        },
    }
    if view_hash:
        payload["hash"] = view_hash
    _slack_api("views.update", payload)


def enqueue_task(conn, lead_id: int, provider: str) -> bool:
    """INSERT an enrich_phone Task for `(lead_id, provider)` unless one is
    already pending/running. Returns True if a row was inserted, False if
    deduped.

    Dedup is per (lead, provider) — BetterContact and LeadMagic can be
    queued for the same lead at once, just not two of the same provider. It
    is best-effort (a TOCTOU window exists across concurrent function
    invocations) — a duplicate Task is harmless: the single-threaded
    EnrichmentWorker runs tasks in series and the second sees the provider
    already in phone_providers_tried, or re-attempts an unbilled API_FAILURE.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM linkedin_task "
            "WHERE task_type = 'enrich_phone' "
            "AND status IN ('pending', 'running') "
            "AND (payload->>'lead_id')::int = %s "
            "AND payload->>'provider' = %s LIMIT 1",
            (lead_id, provider),
        )
        if cur.fetchone() is not None:
            return False
        cur.execute(
            "INSERT INTO linkedin_task "
            "(task_type, status, scheduled_at, payload, error, created_at) "
            "VALUES ('enrich_phone', 'pending', now(), %s, '', now())",
            (Jsonb({
                "lead_id": lead_id,
                "bettercontact_request_id": "",
                "provider": provider,
            }),),
        )
    conn.commit()
    return True


def parse_reply_button(body: str) -> dict:
    """Extract metadata needed to open the manual-reply modal."""
    fields = parse_qs(body)
    raw = (fields.get("payload") or [None])[0]
    if not raw:
        raise ValueError("no payload field")
    payload = json.loads(raw)
    actions = payload.get("actions") or []
    if not actions:
        raise ValueError("no actions in payload")
    action = actions[0]
    if action.get("action_id") != _REPLY_ACTION_ID:
        raise ValueError("not a reply button action")
    value = action.get("value") or ""
    thread_external_id = ""
    if value.strip().startswith("{"):
        value_data = json.loads(value)
        lead_id = int(value_data["lead_id"])
        operator = value_data.get("operator") or ""
        thread_external_id = value_data.get("thread_external_id") or ""
    else:
        if ":" not in value:
            raise ValueError(f"unparseable reply value: {value!r}")
        lead_part, operator = value.split(":", 1)
        lead_id = int(lead_part)
    channel = payload.get("channel") or {}
    message = payload.get("message") or {}
    return {
        "lead_id": lead_id,
        "operator": operator,
        "thread_external_id": thread_external_id,
        "trigger_id": payload.get("trigger_id") or "",
        "response_url": payload.get("response_url") or "",
        "channel_id": channel.get("id") or "",
        "message_ts": message.get("ts") or "",
        "blocks": message.get("blocks") or [],
    }


def parse_reply_cancel_button(body: str) -> dict:
    """Extract metadata for cancelling a queued manual-reply task."""
    fields = parse_qs(body)
    raw = (fields.get("payload") or [None])[0]
    if not raw:
        raise ValueError("no payload field")
    payload = json.loads(raw)
    actions = payload.get("actions") or []
    if not actions:
        raise ValueError("no actions in payload")
    action = actions[0]
    if action.get("action_id") != _REPLY_CANCEL_ACTION_ID:
        raise ValueError("not a reply cancel action")
    value = json.loads(action.get("value") or "{}")
    message = payload.get("message") or {}
    return {
        "task_id": int(value["task_id"]),
        "blocks": message.get("blocks") or [],
    }


def _parse_action_value(value: str) -> dict:
    if value.strip().startswith("{"):
        return json.loads(value)
    if ":" not in value:
        raise ValueError(f"unparseable action value: {value!r}")
    lead_part, operator = value.split(":", 1)
    return {"lead_id": int(lead_part), "operator": operator}


def parse_lead_context_button(body: str) -> dict:
    """Extract metadata needed to open/update the lead context modal."""
    fields = parse_qs(body)
    raw = (fields.get("payload") or [None])[0]
    if not raw:
        raise ValueError("no payload field")
    payload = json.loads(raw)
    actions = payload.get("actions") or []
    if not actions:
        raise ValueError("no actions in payload")
    action = actions[0]
    action_id = action.get("action_id")
    if action_id not in {
        _LEAD_CONTEXT_ACTION_ID,
        _LEAD_CONTEXT_AI_ACTION_ID,
        _LEAD_CONTEXT_DRAFT_ACTION_ID,
    }:
        raise ValueError("not a lead context action")
    value = _parse_action_value(action.get("value") or "")
    view = payload.get("view") or {}
    return {
        "lead_id": int(value["lead_id"]),
        "operator": value.get("operator") or "",
        "thread_external_id": value.get("thread_external_id") or "",
        "trigger_id": payload.get("trigger_id") or "",
        "view_id": view.get("id") or "",
        "view_hash": view.get("hash") or "",
        "action_id": action_id,
    }


def parse_reply_modal_submission(body: str) -> dict:
    """Extract manual-reply task payload from a Slack view_submission."""
    fields = parse_qs(body)
    raw = (fields.get("payload") or [None])[0]
    if not raw:
        raise ValueError("no payload field")
    payload = json.loads(raw)
    if payload.get("type") != "view_submission":
        raise ValueError("not a view_submission payload")
    view = payload.get("view") or {}
    if view.get("callback_id") != _REPLY_MODAL_CALLBACK_ID:
        raise ValueError("not a LinkedIn reply modal")
    metadata = json.loads(view.get("private_metadata") or "{}")
    values = ((view.get("state") or {}).get("values") or {})
    body_text = ""
    for block_values in values.values():
        field = block_values.get(_REPLY_BODY_ACTION_ID)
        if field:
            body_text = (field.get("value") or "").strip()
            break
    if not body_text:
        raise ValueError("empty reply body")
    user = payload.get("user") or {}
    return {
        "lead_id": int(metadata["lead_id"]),
        "operator": metadata.get("operator") or "",
        "message": body_text,
        "slack_channel_id": metadata.get("channel_id") or "",
        "slack_message_ts": metadata.get("message_ts") or "",
        "slack_response_url": metadata.get("response_url") or "",
        "thread_external_id": metadata.get("thread_external_id") or "",
        "slack_user_id": user.get("id") or "",
        "blocks": metadata.get("blocks") or [],
    }


def enqueue_manual_reply_task(conn, payload: dict) -> int:
    """Insert a manual_reply Task unless the same pending reply already exists.

    Returns the pending/running task id. Returning the id lets Slack render a
    cancellable queued state without introducing a second lookup.
    """
    lead_id = int(payload["lead_id"])
    operator = payload["operator"]
    message = payload["message"]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM linkedin_task "
            "WHERE task_type = 'manual_reply' "
            "AND status IN ('pending', 'running') "
            "AND (payload->>'lead_id')::int = %s "
            "AND payload->>'operator' = %s "
            "AND payload->>'message' = %s LIMIT 1",
            (lead_id, operator, message),
        )
        row = cur.fetchone()
        if row is not None:
            return int(row[0])
        cur.execute(
            "INSERT INTO linkedin_task "
            "(task_type, status, scheduled_at, payload, error, created_at) "
            "VALUES ('manual_reply', 'pending', now(), %s, '', now()) "
            "RETURNING id",
            (Jsonb({
                "lead_id": lead_id,
                "operator": operator,
                "message": message,
                "slack_channel_id": payload.get("slack_channel_id", ""),
                "slack_message_ts": payload.get("slack_message_ts", ""),
                "slack_response_url": payload.get("slack_response_url", ""),
                "thread_external_id": payload.get("thread_external_id", ""),
                "slack_user_id": payload.get("slack_user_id", ""),
                "slack_blocks": payload.get("blocks", []),
            }),),
        )
        task_id = int(cur.fetchone()[0])
    conn.commit()
    return task_id


def cancel_manual_reply_task(conn, task_id: int) -> bool:
    """Delete a queued manual_reply Task if it has not started sending."""
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM linkedin_task "
            "WHERE id = %s "
            "AND task_type = 'manual_reply' "
            "AND status = 'pending' "
            "RETURNING id",
            (task_id,),
        )
        deleted = cur.fetchone() is not None
    conn.commit()
    return deleted


def fetch_lead_context(
    conn,
    lead_id: int,
    *,
    operator: str = "",
    thread_external_id: str = "",
) -> dict:
    """Fetch deterministic lead context for the Slack modal using raw SQL."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, first_name, last_name, company_name, linkedin_url, "
            "public_identifier, description, icp, disqualified "
            "FROM crm_lead WHERE id = %s",
            (lead_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"lead {lead_id} not found")
        lead = {
            "id": row[0],
            "first_name": row[1] or "",
            "last_name": row[2] or "",
            "company_name": row[3] or "",
            "linkedin_url": row[4] or "",
            "public_identifier": row[5] or "",
            "description": row[6] or "",
            "icp": row[7] or "",
            "disqualified": bool(row[8]),
        }

        cur.execute(
            "SELECT d.id, d.state, d.sent_note, d.last_reply_at, d.connected_at, "
            "c.name, u.username "
            "FROM crm_deal d "
            "JOIN linkedin_campaign c ON c.id = d.campaign_id "
            "JOIN auth_user u ON u.id = c.user_id "
            "WHERE d.lead_id = %s "
            "ORDER BY d.update_date DESC, d.id DESC "
            "LIMIT 5",
            (lead_id,),
        )
        deals = [
            {
                "id": r[0],
                "state": r[1] or "",
                "sent_note": r[2] or "",
                "last_reply_at": r[3],
                "connected_at": r[4],
                "campaign": r[5] or "",
                "owner": r[6] or "",
            }
            for r in cur.fetchall()
        ]

    messages = fetch_linkedin_thread_preview(
        conn,
        lead_id,
        thread_external_id=thread_external_id,
        limit=_CONTEXT_MESSAGE_LIMIT,
    )
    return {
        "lead": lead,
        "deals": deals,
        "messages": messages,
        "operator": operator,
        "thread_external_id": thread_external_id,
    }


def _profile_bits(description: str) -> dict:
    if isinstance(description, dict):
        data = description
    else:
        try:
            data = json.loads(description or "{}")
        except (TypeError, json.JSONDecodeError):
            return {}
    if not isinstance(data, dict):
        return {}
    positions = data.get("positions") or []
    current = positions[0] if isinstance(positions, list) and positions else {}
    return {
        "headline": data.get("headline") or "",
        "summary": data.get("summary") or "",
        "title": current.get("title", "") if isinstance(current, dict) else "",
        "position_company": current.get("company_name", "") if isinstance(current, dict) else "",
    }


def _full_name(lead: dict) -> str:
    return (
        f"{lead.get('first_name', '')} {lead.get('last_name', '')}".strip()
        or lead.get("public_identifier", "")
        or "Unknown lead"
    )


def _brief_message_line(message: dict) -> str:
    direction = message.get("direction") or ""
    speaker = message.get("sender") or ("Lead" if direction == "inbound" else "Us")
    body = _compact_message(message.get("body") or "", limit=180)
    return f"*{_slack_escape(speaker)}* ({_slack_escape(direction)}): {_slack_escape(body)}"


def render_lead_context_blocks(
    context: dict,
    *,
    ai_summary: str = "",
    ai_error: str = "",
    draft_reply: str = "",
    draft_error: str = "",
    loading: str = "",
) -> list[dict]:
    """Render deterministic lead context, optionally with AI-generated output."""
    lead = context["lead"]
    bits = _profile_bits(lead.get("description", ""))
    name = _slack_escape(_full_name(lead))
    company = lead.get("company_name") or ""
    if company.lower().strip() in {"unknown company", "unknown"}:
        company = ""
    profile = lead.get("linkedin_url") or ""
    title = bits.get("title") or bits.get("headline") or ""

    fields = [
        f"*Name:*\n<{profile}|{name}>" if profile else f"*Name:*\n{name}",
        f"*ICP:*\n{_slack_escape(lead.get('icp') or 'Unknown')}",
    ]
    if company:
        fields.append(f"*Company:*\n{_slack_escape(company)}")
    if title:
        fields.append(f"*Headline/title:*\n{_slack_escape(_compact_message(title, limit=220))}")
    if context.get("operator"):
        fields.append(f"*Lead for:*\n{_slack_escape(context['operator'])}")

    blocks: list[dict] = [
        {
            "type": "section",
            "block_id": "lead_context_header",
            "text": {"type": "mrkdwn", "text": f"*Lead context: {name}*"},
        },
        {"type": "section", "block_id": "lead_context_fields", "fields": [
            {"type": "mrkdwn", "text": item} for item in fields[:10]
        ]},
    ]

    if bits.get("summary"):
        blocks.append({
            "type": "section",
            "block_id": "lead_context_profile_summary",
            "text": {
                "type": "mrkdwn",
                "text": f"*Profile summary*\n{_slack_escape(_compact_message(bits['summary'], limit=650))}",
            },
        })

    if context.get("deals"):
        deal_lines = []
        for deal in context["deals"][:3]:
            owner = deal.get("owner") or "unknown owner"
            campaign = deal.get("campaign") or "unknown campaign"
            state = deal.get("state") or "unknown state"
            deal_lines.append(
                f"*{_slack_escape(owner)}* — {_slack_escape(state)} — {_slack_escape(campaign)}"
            )
        blocks.append({
            "type": "section",
            "block_id": "lead_context_deals",
            "text": {"type": "mrkdwn", "text": "*Campaign/deal context*\n" + "\n".join(deal_lines)},
        })

    if context.get("messages"):
        message_lines = [_brief_message_line(m) for m in context["messages"][-4:]]
        blocks.append({
            "type": "section",
            "block_id": "lead_context_messages",
            "text": {"type": "mrkdwn", "text": "*Recent LinkedIn messages*\n" + "\n".join(message_lines)},
        })

    if loading:
        blocks.append({"type": "divider"})
        blocks.append({
            "type": "section",
            "block_id": "lead_context_loading",
            "text": {"type": "mrkdwn", "text": f":hourglass_flowing_sand: *{_slack_escape(loading)}*"},
        })

    if ai_summary:
        blocks.append({"type": "divider"})
        blocks.append({
            "type": "section",
            "block_id": "lead_context_ai_summary",
            "text": {"type": "mrkdwn", "text": f"*AI summary*\n{_slack_escape(ai_summary)}"},
        })
    elif ai_error:
        blocks.append({"type": "divider"})
        blocks.append({
            "type": "section",
            "block_id": "lead_context_ai_error",
            "text": {"type": "mrkdwn", "text": f":warning: *AI summary failed* — `{_slack_escape(ai_error)}`"},
        })

    if draft_reply:
        blocks.append({"type": "divider"})
        blocks.append({
            "type": "section",
            "block_id": "lead_context_draft_reply",
            "text": {
                "type": "mrkdwn",
                "text": f"*Suggested LinkedIn reply*\n```{_slack_escape(draft_reply)}```",
            },
        })
    elif draft_error:
        blocks.append({"type": "divider"})
        blocks.append({
            "type": "section",
            "block_id": "lead_context_draft_error",
            "text": {"type": "mrkdwn", "text": f":warning: *Draft failed* — `{_slack_escape(draft_error)}`"},
        })

    if not loading:
        value = json.dumps({
            "lead_id": lead["id"],
            "operator": context.get("operator") or "",
            "thread_external_id": context.get("thread_external_id") or "",
        }, separators=(",", ":"))
        blocks.append({
            "type": "actions",
            "block_id": "lead_context_actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": _LEAD_CONTEXT_AI_ACTION_ID,
                    "text": {"type": "plain_text", "text": "Generate AI summary"},
                    "value": value,
                },
                {
                    "type": "button",
                    "action_id": _LEAD_CONTEXT_DRAFT_ACTION_ID,
                    "text": {"type": "plain_text", "text": "Draft reply"},
                    "style": "primary",
                    "value": value,
                },
            ],
        })
    return blocks[:100]


def open_lead_context_modal(*, trigger_id: str, context: dict) -> None:
    _slack_api("views.open", {
        "trigger_id": trigger_id,
        "view": {
            "type": "modal",
            "callback_id": _LEAD_CONTEXT_MODAL_CALLBACK_ID,
            "title": {"type": "plain_text", "text": "Lead context"},
            "close": {"type": "plain_text", "text": "Close"},
            "blocks": render_lead_context_blocks(context),
        },
    })


def _ai_context_payload(context: dict) -> dict:
    lead = context["lead"]
    bits = _profile_bits(lead.get("description", ""))
    return {
        "lead": {
            "name": _full_name(lead),
            "company": lead.get("company_name", ""),
            "linkedin_url": lead.get("linkedin_url", ""),
            "icp": lead.get("icp", ""),
            "headline": bits.get("headline", ""),
            "title": bits.get("title", ""),
            "profile_summary": bits.get("summary", ""),
        },
        "deals": [
            {
                "owner": d.get("owner", ""),
                "campaign": d.get("campaign", ""),
                "state": d.get("state", ""),
                "sent_note": _compact_message(d.get("sent_note", ""), limit=300),
            }
            for d in context.get("deals", [])[:3]
        ],
        "messages": [
            {
                "direction": m.get("direction", ""),
                "sender": m.get("sender", ""),
                "body": _compact_message(m.get("body", ""), limit=700),
            }
            for m in context.get("messages", [])
        ],
        "operator": context.get("operator") or "",
    }


def _llm_chat(*, system: str, user: str, temperature: float = 0.2) -> str:
    if not LLM_API_KEY or not AI_MODEL:
        raise RuntimeError("LLM_API_KEY or AI_MODEL is not configured on Vercel")

    body = json.dumps({
        "model": AI_MODEL,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
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
    with request.urlopen(req, timeout=_LLM_TIMEOUT_SECONDS) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return (
        ((data.get("choices") or [{}])[0].get("message") or {}).get("content")
        or ""
    ).strip()


def generate_ai_lead_summary(context: dict) -> str:
    """Generate a concise lead summary through the configured LLM endpoint."""
    prompt = (
        "Summarize this LinkedIn lead for a FedRAMP automation sales rep. "
        "Be concise and practical. Include: who they are, likely ICP/persona, "
        "what happened in the conversation, and the recommended reply posture. "
        "Do not invent facts.\n\n"
        + json.dumps(_ai_context_payload(context), ensure_ascii=False)
    )
    return _llm_chat(
        system="You are a concise B2B sales analyst for Boundera.",
        user=prompt,
    ) or "No summary returned."


def generate_ai_draft_reply(context: dict) -> str:
    """Draft a short LinkedIn reply grounded in Boundera sales posture."""
    prompt = (
        "Draft one LinkedIn reply for this conversation. Return only the message text. "
        "Write like a direct founder, not a sequence. Use short paragraphs. "
        "Acknowledge the latest message first, add at most one useful Boundera context "
        "sentence, and ask one clear question. Do not invent facts or claim outcomes. "
        "If the prospect says no, respect it and reduce friction. If they are confused, "
        "clarify the ask before adding product detail.\n\n"
        "Boundera context: Boundera helps software vendors working through FedRAMP "
        "reduce manual evidence work, KSI/package readiness friction, gap tracking, "
        "remediation ownership, and ongoing monitoring. For FedRAMP 20x, focus on "
        "evidence, KSI validation, current posture, findings/gaps, and remediation "
        "workflow; avoid framing POA&Ms as the main 20x artifact.\n\n"
        "Persona angles: CSP/security teams care about authorization, cloud evidence, "
        "remediation, and monitoring. Advisors care about repeatable delivery and "
        "client readiness. 3PAOs/assessors care about evidence quality, traceability, "
        "and review friction. Channel partners care about routing vendors to the right owner.\n\n"
        + json.dumps(_ai_context_payload(context), ensure_ascii=False)
    )
    return _llm_chat(
        system="You draft concise Boundera LinkedIn sales replies.",
        user=prompt,
        temperature=0.4,
    ) or "No draft returned."


class handler(BaseHTTPRequestHandler):
    """Vercel Python entrypoint — Vercel routes POST /api/slack_enrich here."""

    def do_POST(self) -> None:  # noqa: N802 — name dictated by BaseHTTPRequestHandler
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8")
        timestamp = self.headers.get("X-Slack-Request-Timestamp", "")
        signature = self.headers.get("X-Slack-Signature", "")

        if not verify_signature(
            body, timestamp, signature, secret=SLACK_SIGNING_SECRET,
        ):
            self._respond_text(401, "invalid signature")
            return

        try:
            payload = decode_slack_payload(body)
            intent = interaction_intent(payload)
            handler_name = _HANDLER_BY_INTENT[intent]
        except (KeyError, ValueError):
            self._respond_text(400, "malformed interaction")
            return

        getattr(self, handler_name)(body)

    def _handle_enrichment_pick(self, body: str) -> None:
        try:
            lead_id, provider, original_blocks = parse_interaction(body)
        except (ValueError, json.JSONDecodeError):
            self._respond_text(400, "malformed interaction")
            return

        try:
            with psycopg.connect(DATABASE_URL) as conn:
                enqueue_task(conn, lead_id, provider)
        except Exception:  # noqa: BLE001 — surface any DB failure as a 500
            self._respond_text(500, "database error")
            return

        # Re-post the message with the select menu intact so the operator can
        # request another provider on the same notification.
        self._respond_blocks(render_response_blocks(original_blocks, provider))

    def _handle_reply_button(self, body: str) -> None:
        try:
            data = parse_reply_button(body)
            thread_blocks: list = []
            try:
                with psycopg.connect(DATABASE_URL) as conn:
                    thread_blocks = render_thread_preview_blocks(
                        fetch_linkedin_thread_preview(
                            conn,
                            data["lead_id"],
                            thread_external_id=data.get("thread_external_id", ""),
                        ),
                    )
            except Exception as exc:  # noqa: BLE001 — modal should still open
                print(f"manual reply thread preview failed: {exc}")
            open_reply_modal(
                trigger_id=data["trigger_id"],
                lead_id=data["lead_id"],
                operator=data["operator"],
                channel_id=data["channel_id"],
                message_ts=data["message_ts"],
                response_url=data["response_url"],
                original_blocks=data["blocks"],
                thread_external_id=data.get("thread_external_id", ""),
                thread_blocks=thread_blocks,
            )
        except (ValueError, json.JSONDecodeError):
            self._respond_text(400, "malformed reply action")
            return
        except Exception:  # noqa: BLE001 — Slack API failure
            self._respond_text(500, "slack modal error")
            return
        self._respond_text(200, "")

    def _handle_lead_context_button(self, body: str) -> None:
        try:
            data = parse_lead_context_button(body)
            with psycopg.connect(DATABASE_URL) as conn:
                context = fetch_lead_context(
                    conn,
                    data["lead_id"],
                    operator=data.get("operator", ""),
                    thread_external_id=data.get("thread_external_id", ""),
                )
            open_lead_context_modal(trigger_id=data["trigger_id"], context=context)
        except (ValueError, json.JSONDecodeError):
            self._respond_text(400, "malformed lead context action")
            return
        except Exception:  # noqa: BLE001 — Slack API/DB failure
            self._respond_text(500, "lead context error")
            return
        self._respond_text(200, "")

    def _handle_lead_context_ai(self, body: str) -> None:
        try:
            data = parse_lead_context_button(body)
            with psycopg.connect(DATABASE_URL) as conn:
                context = fetch_lead_context(
                    conn,
                    data["lead_id"],
                    operator=data.get("operator", ""),
                    thread_external_id=data.get("thread_external_id", ""),
                )
        except (ValueError, json.JSONDecodeError):
            self._respond_text(400, "malformed lead context action")
            return
        except Exception:  # noqa: BLE001 — DB failure
            self._respond_text(500, "lead context error")
            return

        view_id = data.get("view_id") or ""
        if view_id:
            try:
                update_slack_view(
                    view_id=view_id,
                    blocks=render_lead_context_blocks(
                        context, loading="Generating AI summary..."
                    ),
                )
            except Exception:
                pass

        try:
            ai_summary = generate_ai_lead_summary(context)
            blocks = render_lead_context_blocks(context, ai_summary=ai_summary)
        except Exception as exc:  # noqa: BLE001 — show recoverable model failure in modal
            blocks = render_lead_context_blocks(context, ai_error=str(exc))

        try:
            update_slack_view(view_id=view_id, blocks=blocks)
        except Exception:
            self._respond_text(500, "slack modal error")
            return
        self._respond_text(200, "")

    def _handle_lead_context_draft(self, body: str) -> None:
        try:
            data = parse_lead_context_button(body)
            with psycopg.connect(DATABASE_URL) as conn:
                context = fetch_lead_context(
                    conn,
                    data["lead_id"],
                    operator=data.get("operator", ""),
                    thread_external_id=data.get("thread_external_id", ""),
                )
        except (ValueError, json.JSONDecodeError):
            self._respond_text(400, "malformed lead context action")
            return
        except Exception:  # noqa: BLE001 — DB failure
            self._respond_text(500, "lead context error")
            return

        view_id = data.get("view_id") or ""
        if view_id:
            try:
                update_slack_view(
                    view_id=view_id,
                    blocks=render_lead_context_blocks(
                        context, loading="Drafting reply..."
                    ),
                )
            except Exception:
                pass

        try:
            draft = generate_ai_draft_reply(context)
            blocks = render_lead_context_blocks(context, draft_reply=draft)
        except Exception as exc:  # noqa: BLE001 — show recoverable model failure in modal
            blocks = render_lead_context_blocks(context, draft_error=str(exc))

        try:
            update_slack_view(view_id=view_id, blocks=blocks)
        except Exception:
            self._respond_text(500, "slack modal error")
            return
        self._respond_text(200, "")

    def _handle_reply_submission(self, body: str) -> None:
        try:
            payload = parse_reply_modal_submission(body)
        except (ValueError, KeyError, json.JSONDecodeError):
            self._respond_json({
                "response_action": "errors",
                "errors": {
                    "linkedin_reply_message": "Type a reply before queuing.",
                },
            })
            return

        try:
            with psycopg.connect(DATABASE_URL) as conn:
                task_id = enqueue_manual_reply_task(conn, payload)
        except Exception:  # noqa: BLE001 — surface any DB failure as a 500
            self._respond_text(500, "database error")
            return

        blocks = payload.get("blocks") or []
        if blocks:
            queued_blocks = render_reply_status_blocks(
                blocks,
                ":hourglass_flowing_sand: *LinkedIn reply queued* — the daemon will send it shortly. You can cancel it before it starts sending.",
                block_id_suffix="queued",
                cancel_task_id=task_id,
            )
            updated = False
            response_url = payload.get("slack_response_url") or ""
            if response_url:
                try:
                    _post_response_url(response_url, {
                        "replace_original": True,
                        "text": "LinkedIn reply queued",
                        "blocks": queued_blocks,
                    })
                    updated = True
                except Exception:
                    updated = False
            if not updated:
                channel_id = payload.get("slack_channel_id") or ""
                message_ts = payload.get("slack_message_ts") or ""
                if channel_id and message_ts:
                    try:
                        update_slack_message(
                            channel_id=channel_id,
                            message_ts=message_ts,
                            blocks=queued_blocks,
                            text="LinkedIn reply queued",
                        )
                    except Exception:
                        pass

        self._respond_json({"response_action": "clear"})

    def _handle_reply_cancel(self, body: str) -> None:
        try:
            data = parse_reply_cancel_button(body)
        except (ValueError, KeyError, json.JSONDecodeError):
            self._respond_text(400, "malformed reply cancel action")
            return

        try:
            with psycopg.connect(DATABASE_URL) as conn:
                cancelled = cancel_manual_reply_task(conn, data["task_id"])
        except Exception:  # noqa: BLE001 — surface any DB failure as a 500
            self._respond_text(500, "database error")
            return

        if cancelled:
            status_text = ":no_entry: *LinkedIn reply cancelled* — it will not be sent."
            fallback = "LinkedIn reply cancelled"
            suffix = "cancelled"
        else:
            status_text = (
                ":warning: *Could not cancel LinkedIn reply* — it may have already started sending."
            )
            fallback = "Could not cancel LinkedIn reply"
            suffix = "cancel_failed"
        self._respond_blocks(
            render_reply_status_blocks(
                data["blocks"],
                status_text,
                block_id_suffix=suffix,
            ),
            text=fallback,
        )

    def _respond_text(self, code: int, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _respond_blocks(self, blocks: list, *, text: str = "Phone enrichment requested") -> None:
        """200 with a Slack message-replacement body — keeps the select menu
        live so the operator can request more providers."""
        body = json.dumps({
            "replace_original": True,
            "text": text,
            "blocks": blocks,
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _respond_json(self, payload: dict, code: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
