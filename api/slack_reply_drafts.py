"""Bounded, draft-only post-response work for private Slack reply modals.

No daemon Task, campaign, lead classification or outbound delivery is written.
Vercel owns the background invocation; PostgreSQL owns dedupe and saved copy.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
import logging
import os
import re
import uuid

import httpx
import psycopg

from api.exceptions import DraftRuntimeUnavailable, DraftServiceError, DraftViewError

logger = logging.getLogger(__name__)
PREFIX = "private_reply_job_"
JOB_BUDGET = 90
MODEL_BUDGET = 55
MODEL_ATTEMPT = 25
LEASE_SECONDS = 110  # Longer than the full job, shorter than the function limit.


def parse_job(body, api, *, lead_context=False):
    payload = api.decode_slack_payload(body)
    view = payload.get("view") or {}
    expected = api._LEAD_CONTEXT_MODAL_CALLBACK_ID if lead_context else api._REPLY_MODAL_CALLBACK_ID
    if view.get("callback_id") != expected or not view.get("id") or not view.get("hash"):
        raise ValueError("a current private modal is required")
    action = (payload.get("actions") or [{}])[0]
    data = api.parse_lead_context_button(body) if lead_context else api.parse_reply_draft_button(body)
    value = json.loads(action.get("value") or "{}")
    if not data["operator"]:
        raise ValueError("sender is required")
    retry_key = value.get("draft_job_key", "")
    if retry_key and not re.fullmatch(r"[0-9a-f]{64}", retry_key):
        raise ValueError("invalid draft key")
    identity = [view["id"], view["hash"], data["lead_id"], data["operator"],
                data["thread_external_id"], expected]
    data.update(view=deepcopy(view), lead_context=lead_context,
                view_id=view["id"], view_hash=view["hash"], retry=bool(retry_key),
                request_key=retry_key or hashlib.sha256(json.dumps(identity).encode()).hexdigest())
    return data


def schedule(data, api):
    # Lazy import: other Slack actions do not depend on this runtime facility.
    from vercel.cache.context import get_context
    from vercel.functions import wait_until

    # Without the invocation collector wait_until deliberately does nothing.
    # Non-streaming legacy runtimes buffer the response until the work ends.
    if get_context().wait_until is None or not os.environ.get("VERCEL_IPC_PATH"):
        raise DraftRuntimeUnavailable("Private drafting requires the Vercel streaming background runtime")
    wait_until(run(data, api))


def connect(api):
    return psycopg.connect(api.DATABASE_URL, connect_timeout=4,
                           options="-c statement_timeout=4000 -c lock_timeout=1000")


def claim(data, api):
    token = uuid.uuid4().hex
    scope = (data["lead_id"], data["operator"], data["thread_external_id"], data["view_id"])
    with connect(api) as conn, conn.cursor() as cur:
        if not data["retry"]:
            cur.execute(
                "INSERT INTO linkedin_slackreplydraftjob "
                "(request_key, lead_id, operator, thread_external_id, view_id, status, "
                "lease_token, lease_until, content, error_code, created_at, updated_at) "
                "VALUES (%s,%s,%s,%s,%s,'running',%s,now() + %s * interval '1 second','','',now(),now()) "
                "ON CONFLICT (request_key) DO NOTHING RETURNING request_key",
                (data["request_key"], *scope, token, LEASE_SECONDS),
            )
            if cur.fetchone():
                return token, None
        cur.execute(
            "SELECT lead_id, operator, thread_external_id, view_id, status, content, lease_until > now() "
            "FROM linkedin_slackreplydraftjob WHERE request_key = %s FOR UPDATE",
            (data["request_key"],),
        )
        row = cur.fetchone()
        if not row or tuple(row[:4]) != scope:
            raise DraftServiceError("draft_not_found")
        if row[4] == "ready":
            return "", row[5]
        if row[4] == "running" and row[6]:
            return "", None
        # Only an explicit Check/Retry click may reclaim an expired/failed job.
        if not data["retry"]:
            raise DraftServiceError("retry_required")
        cur.execute(
            "UPDATE linkedin_slackreplydraftjob SET status='running', lease_token=%s, "
            "lease_until=now() + %s * interval '1 second', error_code='', updated_at=now() "
            "WHERE request_key=%s", (token, LEASE_SECONDS, data["request_key"]),
        )
        return token, None


def save_result(data, token, content, api):
    with connect(api) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE linkedin_slackreplydraftjob SET status='ready', content=%s, "
            "error_code='', updated_at=now() WHERE request_key=%s AND lease_token=%s "
            "AND status='running' AND lease_until > now() RETURNING created_at",
            (content, data["request_key"], token),
        )
        row = cur.fetchone()
        if not row:
            return False  # An expired/replaced worker cannot publish late copy.
        cur.execute(
            "INSERT INTO linkedin_slackleadcontextartifact "
            "(lead_id, operator, thread_external_id, kind, content, created_at, updated_at) "
            "VALUES (%s,%s,%s,'draft_reply',%s,now(),now()) "
            "ON CONFLICT (lead_id, operator, thread_external_id, kind) DO UPDATE "
            "SET content=EXCLUDED.content, updated_at=now() "
            "WHERE linkedin_slackleadcontextartifact.updated_at <= %s",
            (data["lead_id"], data["operator"], data["thread_external_id"], content, row[0]),
        )
        return True


def fail_job(data, token, code, api):
    if not token:
        return
    with connect(api) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE linkedin_slackreplydraftjob SET status='failed', error_code=%s, updated_at=now() "
            "WHERE request_key=%s AND lease_token=%s AND status='running'",
            (code, data["request_key"], token),
        )


def fetch_context(data, api):
    with connect(api) as conn:
        return api.fetch_reply_draft_context(conn, data["lead_id"], operator=data["operator"],
                                              thread_external_id=data["thread_external_id"])


def modal(data, api, *, state, content=""):
    """Replace the editor only on success; loading/errors retain typed text."""
    source = data["view"]
    view = {k: deepcopy(source[k]) for k in ("type", "callback_id", "title", "submit", "close", "private_metadata") if k in source}
    metadata = json.loads(view.get("private_metadata") or "{}")
    blocks = []
    draft_actions = {api._REPLY_DRAFT_ACTION_ID, api._LEAD_CONTEXT_DRAFT_ACTION_ID}
    for block in deepcopy(source.get("blocks") or []):
        bid = block.get("block_id", "")
        if bid.startswith(PREFIX) or bid in {"linkedin_reply_draft_loading", "linkedin_reply_draft_error"}:
            continue
        if block.get("type") == "actions":
            block["elements"] = [e for e in block["elements"] if e.get("action_id") not in draft_actions]
            if not block["elements"]:
                continue
        blocks.append(block)
    value = {k: data[k] for k in ("lead_id", "operator", "thread_external_id")}
    retry_value = json.dumps({**value, "draft_job_key": data["request_key"]})
    if state == "error" and not data.get("job_known"):
        retry_value = json.dumps(value)
    if state == "ready" and not data["lead_context"]:
        field = next(b for b in blocks if b.get("element", {}).get("action_id") == api._REPLY_BODY_ACTION_ID)
        # Draft reply authorizes replacement when generation succeeds. Slack
        # needs a new block ID to display initial_value instead of old input.
        field["block_id"] = "linkedin_reply_message:" + uuid.uuid4().hex
        field["element"]["initial_value"] = content
        status_text = ""
    elif state == "ready":
        status_text = "Draft reply (not sent):\n" + content
    elif state == "loading":
        status_text = "Drafting reply… The draft will replace the reply text when ready. Check draft if this takes longer than two minutes."
    else:
        status_text = "Couldn't finish displaying the draft. Your text is unchanged. Retry draft to recover or try again."
    if status_text:
        blocks.append({"type": "section", "block_id": PREFIX + "status",
                       "text": {"type": "plain_text", "text": status_text}})
    action_id = api._LEAD_CONTEXT_DRAFT_ACTION_ID if data["lead_context"] else api._REPLY_DRAFT_ACTION_ID
    buttons = [{"type": "button", "action_id": action_id,
                "text": {"type": "plain_text", "text": {"loading": "Check draft", "ready": "Draft again", "error": "Retry draft"}[state]},
                "value": json.dumps(value) if state == "ready" else retry_value}]
    blocks.append({"type": "actions", "block_id": PREFIX + "actions", "elements": buttons})
    view.update(blocks=blocks, private_metadata=api._compact_metadata(metadata))
    return view


async def update(data, view, api):
    """A single CAS retry is safe: a lost success cannot overwrite a newer view."""
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.post(api.SLACK_API_BASE + "/views.update",
                    headers={"Authorization": "Bearer " + api.SLACK_BOT_TOKEN},
                    json={"view_id": data["view_id"], "hash": data["view_hash"], "view": view})
            response.raise_for_status()
            result = response.json()
            if not result.get("ok"):
                code = result.get("error", "unknown")
                if code in {"internal_error", "service_unavailable"} and attempt == 0:
                    continue
                raise DraftViewError(code)
            new_hash = (result.get("view") or {}).get("hash")
            if not new_hash:
                raise ValueError("Slack update returned no view hash")
            data["view_hash"] = new_hash
            data["view"] = view
            return
        except httpx.TransportError:
            if attempt:
                raise
        except httpx.HTTPStatusError as exc:
            if attempt or exc.response.status_code not in {500, 502, 503, 504}:
                raise


async def generate(prompt, api):
    if not api.LLM_API_KEY or not api.AI_MODEL:
        raise DraftServiceError("model_not_configured")
    body = {"model": api.AI_MODEL, "temperature": 0.4,
            "messages": [{"role": "system", "content": "You draft concise Boundera LinkedIn sales replies."},
                         {"role": "user", "content": prompt}]}
    async with asyncio.timeout(MODEL_BUDGET):
        for attempt in range(2):
            try:
                async with asyncio.timeout(MODEL_ATTEMPT):
                    async with httpx.AsyncClient(timeout=httpx.Timeout(MODEL_ATTEMPT, connect=5)) as client:
                        response = await client.post(api.LLM_API_BASE + "/chat/completions",
                            headers={"Authorization": "Bearer " + api.LLM_API_KEY}, json=body)
                if response.status_code in {408, 429, 500, 502, 503, 504}:
                    try:
                        delay = float(response.headers.get("retry-after", "1"))
                    except ValueError:
                        # A dated/unknown Retry-After must not cause an early retry.
                        raise DraftServiceError("model_temporarily_unavailable") from None
                    if attempt or not 0 <= delay <= 3:
                        raise DraftServiceError("model_temporarily_unavailable")
                    await asyncio.sleep(delay)
                    continue
                if not response.is_success:
                    raise DraftServiceError("model_request_rejected")
                result = response.json()
                content = (((result.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
                if not content or len(content) > 2800:
                    raise DraftServiceError("invalid_draft_length")
                return content
            except (TimeoutError, httpx.TransportError):
                if attempt:
                    raise
                await asyncio.sleep(1)
    raise DraftServiceError("model_temporarily_unavailable")


async def run(data, api):
    token = ""
    saved = False
    stage = "claim"
    try:
        async with asyncio.timeout(JOB_BUDGET):
            token, content = await asyncio.to_thread(claim, data, api)
            data["job_known"] = True
            if not token and content is None:
                return  # Duplicate/in-flight request; no new model call.
            stage = "loading_view"
            await update(data, modal(data, api, state="loading"), api)
            if content is None:
                stage = "context"
                context = await asyncio.to_thread(fetch_context, data, api)
                stage = "model"
                content = await generate(api.reply_draft_prompt(context), api)
                stage = "save"
                saved = await asyncio.to_thread(save_result, data, token, content, api)
                if not saved:
                    return
            else:
                saved = True
            stage = "publish"
            await update(data, modal(data, api, state="ready", content=content), api)
            logger.info("Private draft ready job=%s", data["request_key"])
    except DraftViewError as exc:
        # Closed/stale views must never be reopened or updated without a hash.
        logger.warning("Private draft view unavailable job=%s stage=%s code=%s saved=%s", data["request_key"], stage, exc.code, saved)
    except (TimeoutError, httpx.TransportError, httpx.HTTPStatusError, psycopg.OperationalError, DraftServiceError) as exc:
        logger.warning("Private draft interrupted job=%s stage=%s type=%s code=%s saved=%s",
                       data["request_key"], stage, type(exc).__name__,
                       exc.code if isinstance(exc, DraftServiceError) else "transport", saved)
        if isinstance(exc, DraftServiceError) and exc.code == "retry_required":
            data["job_known"] = True
        try:
            await update(data, modal(data, api, state="error"), api)
        except (DraftViewError, httpx.TransportError, httpx.HTTPStatusError):
            logger.warning("Private draft error display unavailable job=%s", data["request_key"])
    finally:
        if token and not saved:
            try:
                await asyncio.to_thread(fail_job, data, token, "draft_incomplete", api)
            except psycopg.OperationalError:
                # A temporary DB outage leaves an expiring lease, not new work.
                logger.warning("Private draft failure receipt unavailable job=%s", data["request_key"])
