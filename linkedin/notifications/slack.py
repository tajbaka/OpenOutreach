"""Slack notifications via incoming webhook.

Four surfaces:

1. `notify_connection_accepted` — fires when a connection invite gets
   accepted (PENDING → CONNECTED via the sweep). Single Block Kit message
   with the lead's name, role, and profile link.
2. `notify_error` / `notify_on_error` — fires when any of our workflows
   (daemon task dispatch, backfill_messages, import_connections,
   export_sales_*, daemon top-level) raises an unexpected exception.
   Wraps a code block via the context manager; the underlying exception
   re-raises so the process still crashes (per project rule: crash on
   unexpected errors). 5-min in-process dedupe by
   (workflow, exception_type, last_traceback_frame) so a runaway loop
   doesn't spam the channel.
3. `notify_message_received` — fires when the realtime listener detects
   and persists a new inbound LinkedIn DM. Single Block Kit message with
   the lead's name and profile link, a quoted snippet of the message body,
   and a context block identifying the owning operator.
4. `notify_phone_enriched` — fires when the enrichment worker finishes a lead.

All four no-op when SLACK_WEBHOOK_URL is unset, so callers don't need to guard.
"""
from __future__ import annotations

import json
import logging
import time
import traceback
from contextlib import contextmanager
from urllib import request
from urllib.error import URLError

from linkedin.conf import SLACK_WEBHOOK_URL

logger = logging.getLogger(__name__)

# In-process dedupe: (workflow, exc_type_name, last_tb_frame_repr) → last seen epoch.
# Reset on process restart, which is fine — these are crash notifications, not
# durable state. We just want to avoid spamming a channel when one error fires
# 50 times in 30 seconds during a real burst.
_RECENT_ERRORS: dict[tuple[str, str, str], float] = {}
_DEDUPE_WINDOW_SECONDS = 300  # 5 min


def notify_connection_accepted(
    *,
    full_name: str,
    title: str,
    company: str,
    profile_url: str,
    campaign_name: str,
    reply_text: str | None = None,
    operator: str = "",
) -> None:
    """Post a 'connection accepted' message to Slack. Silent no-op if disabled.

    `reply_text` (when truthy) signals the lead also replied to your note —
    the notification then highlights the reply rather than just the accept.
    `operator` is the display name of the account that owns this lead
    (Chuka / Arian) — derived by the caller from `session.linkedin_profile`.
    Rendered alongside Campaign so the team knows whose lead it is at a glance.
    """
    if not SLACK_WEBHOOK_URL:
        return

    headline = " · ".join(p for p in (title, company) if p)
    operator_clean = (operator or "").strip()

    def _context_elements() -> list[dict]:
        elements: list[dict] = []
        if operator_clean:
            elements.append({"type": "mrkdwn", "text": f"*Lead for:* {operator_clean}"})
        elements.append({"type": "mrkdwn", "text": f"*Campaign:* {campaign_name}"})
        if headline:
            elements.append({"type": "mrkdwn", "text": f"*Role:* {headline}"})
        return elements

    op_suffix = f" — {operator_clean}'s lead" if operator_clean else ""

    if reply_text:
        emoji = ":speech_balloon:"
        action_line = f"{emoji} *<{profile_url}|{full_name}>* accepted *and replied*{op_suffix}"
        fallback = f"{emoji} {full_name} accepted and replied ({campaign_name}){op_suffix}"
        snippet = reply_text.strip().replace("\n", " ")
        if len(snippet) > 280:
            snippet = snippet[:277] + "..."
        blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": action_line}},
            {"type": "section", "text": {"type": "mrkdwn", "text": f"> {snippet}"}},
            {"type": "context", "elements": _context_elements()},
        ]
    else:
        emoji = ":handshake:"
        action_line = (
            f"{emoji} *<{profile_url}|{full_name}>* accepted your invite (no reply yet){op_suffix}"
        )
        fallback = f"{emoji} {full_name} accepted your invite ({campaign_name}){op_suffix}"
        blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": action_line}},
            {"type": "context", "elements": _context_elements()},
        ]

    payload = {"text": fallback, "blocks": blocks}

    body = json.dumps(payload).encode("utf-8")
    req = request.Request(
        SLACK_WEBHOOK_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=10) as resp:
            if resp.status != 200:
                logger.warning(
                    "Slack webhook returned %d for %s", resp.status, full_name
                )
    except (URLError, TimeoutError) as e:
        logger.warning("Slack webhook failed for %s: %s", full_name, e)


def notify_message_received(
    *,
    lead,
    text: str,
    operator: str = "",
) -> None:
    """Post an 'inbound message received' notification. No-op if disabled.

    Fired by the realtime listener when an inbound LinkedIn DM is detected
    and freshly persisted. `lead` is the crm.Lead; `text` is the message
    body; `operator` is the canonical handle of the account that owns the
    lead (rendered so the team knows whose lead replied).
    """
    if not SLACK_WEBHOOK_URL:
        return

    full_name = (
        f"{lead.first_name or ''} {lead.last_name or ''}".strip()
        or lead.public_identifier
        or "Unknown lead"
    )
    profile_url = lead.linkedin_url or ""
    operator_clean = (operator or "").strip()

    snippet = (text or "").strip().replace("\n", " ")
    if len(snippet) > 280:
        snippet = snippet[:277] + "..."

    op_suffix = f" — {operator_clean}'s lead" if operator_clean else ""
    name_md = f"<{profile_url}|{full_name}>" if profile_url else full_name
    action_line = f":envelope: *{name_md}* sent you a message{op_suffix}"
    fallback = f":envelope: {full_name} sent you a message{op_suffix}"

    elements: list[dict] = []
    if operator_clean:
        elements.append({"type": "mrkdwn", "text": f"*Lead for:* {operator_clean}"})
    if lead.company_name:
        elements.append({"type": "mrkdwn", "text": f"*Company:* {lead.company_name}"})

    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": action_line}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"> {snippet}"}},
    ]
    if elements:
        blocks.append({"type": "context", "elements": elements})

    # Operator-triggered phone enrichment — Slack POSTs the picked option to
    # the api/slack_enrich.py Vercel function. Each value encodes
    # "<lead_id>:<provider>". Always rendered (no feature flag).
    blocks.append({
        "type": "actions",
        "block_id": "enrich_phone_actions",
        "elements": [
            {
                "type": "static_select",
                "action_id": "enrich_phone_select",
                "placeholder": {
                    "type": "plain_text", "text": "📞 Get phone number",
                },
                "options": [
                    {
                        "text": {
                            "type": "plain_text",
                            "text": "📞 All providers (waterfall)",
                        },
                        "value": f"{lead.id}:waterfall",
                    },
                    {
                        "text": {
                            "type": "plain_text", "text": "BetterContact only",
                        },
                        "value": f"{lead.id}:bettercontact",
                    },
                    {
                        "text": {
                            "type": "plain_text",
                            "text": "LeadMagic only (cheapest)",
                        },
                        "value": f"{lead.id}:leadmagic",
                    },
                    {
                        "text": {"type": "plain_text", "text": "Prospeo only"},
                        "value": f"{lead.id}:prospeo",
                    },
                ],
            },
        ],
    })

    payload = {"text": fallback, "blocks": blocks}
    body = json.dumps(payload).encode("utf-8")
    req = request.Request(
        SLACK_WEBHOOK_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=10) as resp:
            if resp.status != 200:
                logger.warning(
                    "Slack webhook returned %d for message-received (%s)",
                    resp.status, full_name,
                )
    except (URLError, TimeoutError) as e:
        logger.warning("Slack message-received webhook failed for %s: %s", full_name, e)


def notify_phone_enriched(*, lead, result) -> None:
    """Post a 'phone enriched' notification. No-op if SLACK_WEBHOOK_URL unset.

    `result` is an enrichment EnrichmentResult. A FOUND result renders the
    number and the winning provider; a NOT_FOUND renders 'no number found'.
    API_FAILURE never reaches here — the enrichment worker marks the task
    failed without notifying.
    """
    if not SLACK_WEBHOOK_URL:
        return

    full_name = (
        f"{lead.first_name or ''} {lead.last_name or ''}".strip()
        or lead.public_identifier
        or "Unknown lead"
    )
    profile_url = lead.linkedin_url or ""
    name_md = f"<{profile_url}|{full_name}>" if profile_url else full_name

    if result.phone:
        action_line = f":telephone_receiver: Phone found for *{name_md}*: `{result.phone}`"
        fallback = f":telephone_receiver: Phone found for {full_name}: {result.phone}"
    else:
        action_line = f":telephone_receiver: No phone number found for *{name_md}*"
        fallback = f":telephone_receiver: No phone number found for {full_name}"

    elements: list[dict] = []
    if lead.company_name:
        elements.append({"type": "mrkdwn", "text": f"*Company:* {lead.company_name}"})
    elements.append({"type": "mrkdwn", "text": f"*Provider:* {result.provider}"})

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": action_line}},
        {"type": "context", "elements": elements},
    ]
    payload = {"text": fallback, "blocks": blocks}

    body = json.dumps(payload).encode("utf-8")
    req = request.Request(
        SLACK_WEBHOOK_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=10) as resp:
            if resp.status != 200:
                logger.warning(
                    "Slack phone-enriched webhook returned %d for %s",
                    resp.status, full_name,
                )
    except (URLError, TimeoutError) as e:
        logger.warning("Slack phone-enriched webhook failed for %s: %s", full_name, e)


def notify_error(
    workflow: str,
    exc: BaseException,
    context: dict | None = None,
) -> None:
    """Post a workflow-crash notification to Slack. Silent no-op if disabled.

    `workflow` is a short identifier like "daemon:follow_up", "backfill_messages",
    "import_connections", "export_sales_list", "export_sales_search", or
    "daemon:startup". `context` is an optional dict of supplementary fields
    (campaign, operator, payload, lead URL, etc.) rendered as a context block.

    Dedup window: a 5-min in-process cache suppresses repeats of the same
    (workflow, exception type, last traceback frame) so a tight error loop
    spams once, not N times.
    """
    if not SLACK_WEBHOOK_URL:
        return

    exc_type = type(exc).__name__
    tb_frames = traceback.extract_tb(exc.__traceback__) if exc.__traceback__ else []
    last_frame_repr = (
        f"{tb_frames[-1].filename}:{tb_frames[-1].lineno}:{tb_frames[-1].name}"
        if tb_frames else ""
    )
    key = (workflow, exc_type, last_frame_repr)

    now = time.time()
    # Garbage-collect stale entries so the dict can't grow unbounded.
    for k in list(_RECENT_ERRORS.keys()):
        if now - _RECENT_ERRORS[k] > _DEDUPE_WINDOW_SECONDS:
            del _RECENT_ERRORS[k]

    if key in _RECENT_ERRORS:
        logger.debug("Slack error notify deduped: %s", key)
        return
    _RECENT_ERRORS[key] = now

    tb_text = "".join(
        traceback.format_exception(type(exc), exc, exc.__traceback__)
    )
    # Slack section block has a 3000-char text limit; keep traceback under
    # that with room for the ``` fence and a truncation marker.
    if len(tb_text) > 2800:
        tb_text = tb_text[:2800] + "\n…(truncated)"

    exc_summary = f"{exc_type}: {exc}"
    summary_line = f":rotating_light: *{workflow} crashed* — `{exc_summary[:200]}`"
    fallback = f":rotating_light: {workflow} crashed: {exc_summary[:200]}"

    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": summary_line}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"```{tb_text}```"}},
    ]
    if context:
        elements = [
            {"type": "mrkdwn", "text": f"*{k}:* `{v}`"}
            for k, v in context.items()
        ]
        blocks.append({"type": "context", "elements": elements})

    payload = {"text": fallback, "blocks": blocks}

    body = json.dumps(payload).encode("utf-8")
    req = request.Request(
        SLACK_WEBHOOK_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=10) as resp:
            if resp.status != 200:
                logger.warning(
                    "Slack error webhook returned %d for %s", resp.status, workflow
                )
    except (URLError, TimeoutError) as e:
        # Never let a Slack outage mask the real exception — the caller's
        # re-raise will still take the process down with the right traceback.
        logger.warning("Slack error webhook failed for %s: %s", workflow, e)


@contextmanager
def notify_on_error(workflow: str, context: dict | None = None):
    """Context manager: notify Slack on any Exception, then re-raise.

    KeyboardInterrupt + SystemExit pass through untouched (a graceful Ctrl-C
    or sys.exit shouldn't spam the channel). Use around the body of a
    long-running entrypoint (daemon loop iteration, management command
    handle()) so a crash both shows up in Slack and still propagates out
    of the process per the project's "crash on unexpected" rule.
    """
    try:
        yield
    except Exception as exc:
        notify_error(workflow, exc, context=context)
        raise


def latest_reply_from_lead(messages: list[dict] | None, lead_full_name: str) -> dict | None:
    """Return the most recent message dict where the sender is the lead.

    `messages` is the list returned by `get_conversation()` —
    [{sender, text, timestamp}, ...] sorted oldest-first. None / empty means
    no conversation exists. Match is case-insensitive on the lead's name.

    Returns the raw message dict so callers can pull both `.text` and
    `.timestamp` (e.g. to update `Deal.last_reply_at`).
    """
    if not messages:
        return None
    target = lead_full_name.strip().lower()
    if not target:
        return None
    lead_messages = [
        m for m in messages
        if (m.get("sender") or "").strip().lower() == target and (m.get("text") or "").strip()
    ]
    if not lead_messages:
        return None
    return lead_messages[-1]
