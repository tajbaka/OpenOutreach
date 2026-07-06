"""Slack notifications via incoming webhook.

Nine surfaces:

1. `notify_connection_accepted` — fires when a connection invite gets
   accepted *and* the lead replied during the sweep. Single Block Kit message
   with the lead's name, role, profile link, and reply snippet.
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
   the lead's name and profile link, the full Slack-safe quoted message
   body, and a context block identifying the owning operator.
4. `notify_phone_enriched` — fires when the enrichment worker finishes a lead.
5. `notify_degraded` — fires from the monitoring layer when a peer node
   looks down, or this node is alive but failing (task-failure streak,
   stale realtime listener).
6. `notify_status_summary` — fires from the hourly account-agnostic
   `status_summary` task with all-sender send counts and pipeline counts.
7. `notify_sweep_summary` — legacy per-sweep analytics helper retained for
   compatibility; connection sweeps no longer call it.
8. `notify_connect_button_missing` — fires when the connect workflow lands
   on a profile that looks not-connected but no Connect CTA can be found.
9. `notify_connect_send_failed` — fires when the connect workflow opens
   the invite flow but cannot actually submit the request (missing note UI,
   missing send button, etc.).

Routing across two channels:
  - `notify_connection_accepted` + `notify_error` + `notify_degraded`
    + `notify_status_summary` + `notify_sweep_summary` + `notify_connect_button_missing`
    + `notify_connect_send_failed`
    → SLACK_WEBHOOK_URL (ops: bugs, invites, monitoring, sweep analytics).
  - `notify_message_received` + `notify_phone_enriched` → SLACK_REPLIES_WEBHOOK_URL
    (replies: a lead replied, and the enrichment results that follow).

Each surface no-ops when its target webhook is unset, so callers don't need
to guard. The two webhooks are independent — an unset SLACK_REPLIES_WEBHOOK_URL
silently drops reply/enrichment notifications rather than routing them to ops.
"""
from __future__ import annotations

import json
import logging
import re
import ssl
import time
import traceback
from contextlib import contextmanager
from urllib import request
from urllib.error import URLError

import certifi

from linkedin.conf import (
    SLACK_BOT_TOKEN,
    SLACK_HIGH_SIGNAL_URL,
    SLACK_REPLIES_WEBHOOK_URL,
    SLACK_WEBHOOK_URL,
)
from linkedin.icp_outbound import is_unknown_company_name

logger = logging.getLogger(__name__)

# In-process dedupe: (workflow, exc_type_name, last_tb_frame_repr) → last seen epoch.
# Reset on process restart, which is fine — these are crash notifications, not
# durable state. We just want to avoid spamming a channel when one error fires
# 50 times in 30 seconds during a real burst.
_RECENT_ERRORS: dict[tuple[str, str, str], float] = {}
_DEDUPE_WINDOW_SECONDS = 300  # 5 min
_SLACK_SECTION_TEXT_LIMIT = 3000
_SLACK_MESSAGE_BODY_LIMIT = 2900
_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())


def _urlopen(req: request.Request, *, timeout: int):
    """Open Slack HTTPS requests with a bundled CA store.

    Some daemon hosts have stale or missing system trust roots, which makes
    Slack webhooks fail with CERTIFICATE_VERIFY_FAILED. certifi keeps Slack
    posting independent of the local Python/macOS certificate setup.
    """
    return request.urlopen(req, timeout=timeout, context=_SSL_CONTEXT)


def _post_to_slack(webhook_url: str, payload: dict, label: str) -> None:
    """POST a Block Kit payload to a Slack incoming webhook.

    Silent no-op when `webhook_url` is empty. `label` identifies the surface
    in log messages. Network failures are logged and swallowed — a Slack
    outage must never mask the caller's real work (notify_error's caller
    re-raises the original exception regardless of what happens here).
    """
    if not webhook_url:
        return
    body = json.dumps(payload).encode("utf-8")
    req = request.Request(
        webhook_url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with _urlopen(req, timeout=10) as resp:
            if resp.status != 200:
                logger.warning("Slack webhook returned %d for %s", resp.status, label)
    except (URLError, TimeoutError) as e:
        logger.warning("Slack webhook failed for %s: %s", label, e)


def _post_slack_response_url(response_url: str, payload: dict, label: str) -> None:
    """POST to a Slack interaction response_url. Best-effort only."""
    if not response_url:
        return
    body = json.dumps(payload).encode("utf-8")
    req = request.Request(
        response_url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with _urlopen(req, timeout=10) as resp:
            resp.read()
    except (URLError, TimeoutError, OSError) as e:
        logger.warning("Slack response_url failed for %s: %s", label, e)


def _slack_api(method: str, payload: dict, label: str) -> bool:
    """Call Slack Web API. Returns False on missing token or recoverable failure."""
    if not SLACK_BOT_TOKEN:
        return False
    body = json.dumps(payload).encode("utf-8")
    req = request.Request(
        f"https://slack.com/api/{method}",
        data=body,
        headers={
            "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
            "Content-Type": "application/json; charset=utf-8",
        },
        method="POST",
    )
    try:
        with _urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
        logger.warning("Slack API %s failed for %s: %s", method, label, e)
        return False
    if not data.get("ok"):
        logger.warning("Slack API %s failed for %s: %s", method, label, data.get("error"))
        return False
    return True


def _manual_reply_status_blocks(original_blocks: list, status_text: str, suffix: str) -> list:
    status = {
        "type": "section",
        "block_id": f"reply_status:{suffix}",
        "text": {"type": "mrkdwn", "text": status_text},
    }
    out: list = []
    inserted = False
    for block in original_blocks or []:
        if block.get("block_id", "").startswith("reply_status:"):
            continue
        if block.get("type") == "actions" and not inserted:
            out.append(status)
            inserted = True
        out.append(block)
    if not inserted:
        out.append(status)
    return out


def _update_manual_reply_status(payload: dict, *, status_text: str, suffix: str, fallback: str) -> None:
    channel_id = payload.get("slack_channel_id") or ""
    message_ts = payload.get("slack_message_ts") or ""
    blocks = payload.get("slack_blocks") or []
    if channel_id and message_ts and blocks:
        updated = _slack_api(
            "chat.update",
            {
                "channel": channel_id,
                "ts": message_ts,
                "text": fallback,
                "blocks": _manual_reply_status_blocks(blocks, status_text, suffix),
            },
            f"manual reply {suffix}",
        )
        if updated:
            return

    _post_slack_response_url(
        payload.get("slack_response_url", ""),
        {
            "response_type": "in_channel",
            "replace_original": False,
            "text": fallback,
        },
        f"manual reply {suffix}",
    )


def notify_manual_reply_sent(payload: dict, *, lead_name: str = "") -> None:
    """Tell Slack that a queued manual LinkedIn reply was sent."""
    target = f" to {lead_name}" if lead_name else ""
    fallback = f":white_check_mark: LinkedIn reply sent{target}."
    _update_manual_reply_status(
        payload,
        status_text=f":white_check_mark: *LinkedIn reply sent*{target}.",
        suffix="sent",
        fallback=fallback,
    )


def notify_manual_reply_failed(payload: dict, error: str) -> None:
    """Tell Slack that a queued manual LinkedIn reply failed."""
    short = (error or "Unknown error").splitlines()[0][:240]
    fallback = f":warning: LinkedIn reply failed: `{short}`"
    _update_manual_reply_status(
        payload,
        status_text=f":warning: *LinkedIn reply failed* — `{short}`",
        suffix="failed",
        fallback=fallback,
    )


def _quote_slack_message_body(text: str) -> str:
    """Render a LinkedIn reply as Slack mrkdwn quote text.

    Slack section text is capped at 3000 chars. Keep a little room for
    the quote prefixes and an explicit truncation marker, while preserving
    line breaks for normal-length replies.
    """
    body = (text or "").strip()
    if not body:
        body = "(empty message)"
    truncated = len(body) > _SLACK_MESSAGE_BODY_LIMIT
    if truncated:
        body = body[:_SLACK_MESSAGE_BODY_LIMIT].rstrip()
    quoted = "\n".join(f"> {line}" if line else ">" for line in body.splitlines())
    if truncated:
        quoted = f"{quoted}\n> ...(truncated)"
    return quoted[:_SLACK_SECTION_TEXT_LIMIT]


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
    """Post an 'accepted and replied' message to Slack. Silent no-op if disabled.

    `reply_text` (when truthy) signals the lead also replied to your note —
    the notification then highlights the reply rather than just the accept.
    `operator` is the display name of the account that owns this lead
    (Chuka / Arian) — derived by the caller from `session.linkedin_profile`.
    Rendered alongside Campaign so the team knows whose lead it is at a glance.
    """
    if not SLACK_WEBHOOK_URL:
        return
    if not reply_text:
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

    payload = {"text": fallback, "blocks": blocks}
    _post_to_slack(SLACK_WEBHOOK_URL, payload, f"connection-accepted ({full_name})")


def notify_connect_button_missing(
    *,
    full_name: str,
    profile_url: str,
    campaign_name: str,
    operator: str = "",
    attempt: int | None = None,
    max_attempts: int | None = None,
) -> None:
    """Post a 'Connect button missing' alert to the ops channel.

    Fired from the connect task when a lead still appears eligible for
    outreach, but the browser cannot find any usable Connect CTA. This is
    primarily an operator/debugging signal for selector drift or unusual
    profile surfaces.
    """
    if not SLACK_WEBHOOK_URL:
        return

    operator_clean = (operator or "").strip()
    name_md = f"<{profile_url}|{full_name}>" if profile_url else full_name
    attempt_text = ""
    if attempt is not None and max_attempts is not None:
        attempt_text = f" (attempt {attempt}/{max_attempts})"

    action_line = f":mag: Connect button missing for *{name_md}*{attempt_text}"
    fallback = f":mag: Connect button missing for {full_name}{attempt_text}"

    elements: list[dict] = []
    if operator_clean:
        elements.append({"type": "mrkdwn", "text": f"*Lead for:* {operator_clean}"})
    elements.append({"type": "mrkdwn", "text": f"*Campaign:* {campaign_name}"})

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": action_line}},
        {"type": "context", "elements": elements},
    ]
    payload = {"text": fallback, "blocks": blocks}
    _post_to_slack(
        SLACK_WEBHOOK_URL, payload, f"connect-button-missing ({full_name})"
    )


def notify_connect_send_failed(
    *,
    full_name: str,
    profile_url: str,
    campaign_name: str,
    operator: str = "",
    reason: str = "",
) -> None:
    """Post a 'connect send failed' alert to the ops channel.

    Fired when the workflow positively reaches a connect flow but cannot
    actually submit the invite request, e.g. the expected note/send UI is
    missing on a LinkedIn variant we do not yet handle.
    """
    if not SLACK_WEBHOOK_URL:
        return

    operator_clean = (operator or "").strip()
    name_md = f"<{profile_url}|{full_name}>" if profile_url else full_name
    reason_clean = (reason or "").strip() or "Unknown connect-send failure"

    action_line = f":warning: Connect send failed for *{name_md}*"
    fallback = f":warning: Connect send failed for {full_name}"

    elements: list[dict] = []
    if operator_clean:
        elements.append({"type": "mrkdwn", "text": f"*Lead for:* {operator_clean}"})
    elements.append({"type": "mrkdwn", "text": f"*Campaign:* {campaign_name}"})

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": action_line}},
        {"type": "section", "text": {"type": "mrkdwn", "text": reason_clean}},
        {"type": "context", "elements": elements},
    ]
    payload = {"text": fallback, "blocks": blocks}
    _post_to_slack(
        SLACK_WEBHOOK_URL, payload, f"connect-send-failed ({full_name})"
    )


def notify_message_received(
    *,
    lead,
    text: str,
    operator: str = "",
    thread_external_id: str = "",
) -> None:
    """Post an 'inbound message received' notification. No-op if disabled.

    Fired by the realtime listener when an inbound LinkedIn DM is detected
    and freshly persisted. `lead` is the crm.Lead; `text` is the message
    body; `operator` is the canonical handle of the account that owns the
    lead (rendered so the team knows whose lead replied). `thread_external_id`
    scopes the Slack reply modal transcript when multiple senders share one
    Lead row but have separate LinkedIn DM threads.

    Posts to SLACK_REPLIES_WEBHOOK_URL — the channel where the team triages
    replies and decides whether to run phone enrichment.
    """
    if not SLACK_REPLIES_WEBHOOK_URL:
        return

    full_name = (
        f"{lead.first_name or ''} {lead.last_name or ''}".strip()
        or lead.public_identifier
        or "Unknown lead"
    )
    profile_url = lead.linkedin_url or ""
    operator_clean = (operator or "").strip()

    quoted_message = _quote_slack_message_body(text or "")

    op_suffix = f" — {operator_clean}'s lead" if operator_clean else ""
    name_md = f"<{profile_url}|{full_name}>" if profile_url else full_name
    action_line = f":envelope: *{name_md}* sent you a message{op_suffix}"
    fallback = f":envelope: {full_name} sent you a message{op_suffix}"

    elements: list[dict] = []
    if operator_clean:
        elements.append({"type": "mrkdwn", "text": f"*Lead for:* {operator_clean}"})
    if lead.company_name and not is_unknown_company_name(lead.company_name):
        elements.append({"type": "mrkdwn", "text": f"*Company:* {lead.company_name}"})

    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": action_line}},
        {"type": "section", "text": {"type": "mrkdwn", "text": quoted_message}},
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
                "type": "button",
                "action_id": "linkedin_reply_button",
                "text": {
                    "type": "plain_text",
                    "text": "Reply on LinkedIn",
                },
                "style": "primary",
                "value": json.dumps({
                    "lead_id": lead.id,
                    "operator": operator_clean,
                    "thread_external_id": thread_external_id or "",
                }, separators=(",", ":")),
            },
            {
                "type": "button",
                "action_id": "linkedin_lead_context_button",
                "text": {
                    "type": "plain_text",
                    "text": "Lead context",
                },
                "value": json.dumps({
                    "lead_id": lead.id,
                    "operator": operator_clean,
                    "thread_external_id": thread_external_id or "",
                }, separators=(",", ":")),
            },
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
    _post_to_slack(
        SLACK_REPLIES_WEBHOOK_URL, payload, f"message-received ({full_name})"
    )


def format_phone_display(raw: str) -> str:
    """Human-readable phone format for Slack messages. A NANP number renders
    as '+1 (905) 569-6193'; anything non-NANP is returned unchanged."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits[0] == "1":
        d = digits[1:]
    elif len(digits) == 10:
        d = digits
    else:
        return raw or ""
    return f"+1 ({d[0:3]}) {d[3:6]}-{d[6:10]}"


def notify_phone_enriched(*, lead, result) -> None:
    """Post a 'phone enriched' notification. No-op if no replies webhook set.

    `result` is an enrichment EnrichmentResult. A FOUND result renders the
    number and the winning provider; a NOT_FOUND renders 'no number found'.
    API_FAILURE never reaches here — the enrichment worker marks the task
    failed without notifying.

    Posts to SLACK_REPLIES_WEBHOOK_URL — enrichment is the follow-on step
    from a reply, so the result lands in the same channel as the reply.
    """
    if not SLACK_REPLIES_WEBHOOK_URL:
        return

    full_name = (
        f"{lead.first_name or ''} {lead.last_name or ''}".strip()
        or lead.public_identifier
        or "Unknown lead"
    )
    profile_url = lead.linkedin_url or ""
    name_md = f"<{profile_url}|{full_name}>" if profile_url else full_name

    if result.phone:
        phone_fmt = format_phone_display(result.phone)
        action_line = f":telephone_receiver: Phone found for *{name_md}*: `{phone_fmt}`"
        fallback = f":telephone_receiver: Phone found for {full_name}: {phone_fmt}"
    else:
        action_line = f":telephone_receiver: No phone number found for *{name_md}*"
        fallback = f":telephone_receiver: No phone number found for {full_name}"

    elements: list[dict] = []
    if lead.company_name and not is_unknown_company_name(lead.company_name):
        elements.append({"type": "mrkdwn", "text": f"*Company:* {lead.company_name}"})
    elements.append({"type": "mrkdwn", "text": f"*Provider:* {result.provider}"})

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": action_line}},
        {"type": "context", "elements": elements},
    ]
    payload = {"text": fallback, "blocks": blocks}
    _post_to_slack(
        SLACK_REPLIES_WEBHOOK_URL, payload, f"phone-enriched ({full_name})"
    )


def notify_feed_intent_signal(*, post) -> bool:
    """Post a high-intent LinkedIn feed signal to the high-signal channel."""
    if not SLACK_HIGH_SIGNAL_URL:
        return False

    author = post.author_name or "Unknown author"
    author_md = f"<{post.author_profile_url}|{author}>" if post.author_profile_url else author
    post_md = f"<{post.post_url}|Open post>" if post.post_url else "Post URL unavailable"
    topics = ", ".join(post.topics or []) or "uncategorized"
    headline = (post.author_headline or "").strip()
    excerpt = (post.post_text or "").strip().replace("\n", " ")
    if len(excerpt) > 500:
        excerpt = excerpt[:497].rstrip() + "..."

    observations = list(post.observations.order_by("operator", "account_username")[:8])
    seen_by = ", ".join(
        sorted({obs.operator or obs.account_username for obs in observations if obs.operator or obs.account_username})
    )
    context_elements: list[dict] = [
        {"type": "mrkdwn", "text": f"*Intent:* {post.intent or 'unknown'}"},
        {"type": "mrkdwn", "text": f"*Audience:* {post.audience or 'unknown'}"},
        {"type": "mrkdwn", "text": f"*Topics:* {topics}"},
    ]
    if seen_by:
        context_elements.append({"type": "mrkdwn", "text": f"*Seen by:* {seen_by}"})

    blocks: list[dict] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":rotating_light: *High-intent LinkedIn feed post*\n*{author_md}*",
            },
        },
    ]
    if headline:
        blocks.append({"type": "context", "elements": [
            {"type": "mrkdwn", "text": headline[:300]},
        ]})
    blocks.extend([
        {"type": "section", "text": {"type": "mrkdwn", "text": f"> {excerpt or '(no text)'}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Why it matters:*\n{post.relevance_reason or 'No reason saved.'}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Suggested action:*\n{post.suggested_action or 'Review the post.'}"}},
        {"type": "context", "elements": context_elements},
        {"type": "section", "text": {"type": "mrkdwn", "text": post_md}},
    ])
    payload = {"text": f"High-intent LinkedIn feed post: {author}", "blocks": blocks}
    _post_to_slack(SLACK_HIGH_SIGNAL_URL, payload, f"feed-intent ({author})")
    return True


def notify_degraded(*, sender: str, title: str, detail: str) -> None:
    """Post a monitoring alert to the ops channel (SLACK_WEBHOOK_URL).

    Used by `linkedin/monitoring/` for two kinds of problem:
      - a peer node looks down (the daemon for `sender` stopped beating);
      - this node is alive but degraded (consecutive task failures, a
        stalled realtime listener).

    `sender` is the affected node's operator handle. Silent no-op when the
    ops webhook is unset. Dedup/cooldown is the caller's responsibility —
    in-process for degraded checks, DB-claimed (`down_alerted_at`) for
    peer-down — so this function always posts when called.
    """
    if not SLACK_WEBHOOK_URL:
        return

    summary_line = f":warning: *{title}*"
    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": summary_line}},
        {"type": "section", "text": {"type": "mrkdwn", "text": detail}},
        {"type": "context", "elements": [
            {"type": "mrkdwn", "text": f"*Node:* {sender}"},
        ]},
    ]
    payload = {"text": f":warning: {title}", "blocks": blocks}
    _post_to_slack(SLACK_WEBHOOK_URL, payload, f"degraded ({sender})")


def notify_sweep_summary(
    *,
    sender: str,
    connects_today: int,
    followups_today: int,
    email_followups_today: int = 0,
    connect_runs_today: int | None = None,
    qualified: int | None = None,
    newly_connected: int | None = None,
) -> None:
    """Post a minimal per-sender send-count snapshot to the ops channel.

    Fired once per connection sweep (cadence = CONNECTION_SWEEP_INTERVAL_HOURS)
    by `handle_sweep_connections`. `sender` is the operator handle of the
    account that ran the sweep — all counts are scoped to that sender.
    Silent no-op when the ops webhook is unset.

      - connects_today / followups_today — LinkedIn actions sent today (ActionLog)
      - email_followups_today — Gmail follow-up sends today (crm.Message ledger)
      - connect_runs_today / qualified / newly_connected — optional pipeline
        context from the sweep task
    """
    if not SLACK_WEBHOOK_URL:
        return

    headline = f":bar_chart: *Connection sweep — {sender}*"
    body = (
        f"*Sent today:* {connects_today} invites\n"
        f"*LinkedIn follow-ups today:* {followups_today}\n"
        f"*Email follow-ups today:* {email_followups_today}"
    )
    if newly_connected is not None:
        body += f"\n*Newly accepted this sweep:* {newly_connected}"
    if connect_runs_today is not None:
        body += f"\n*Connect tasks run today:* {connect_runs_today}"
    if qualified is not None:
        body += f"\n*Qualified remaining:* {qualified}"
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": headline}},
        {"type": "section", "text": {"type": "mrkdwn", "text": body}},
    ]
    fallback = (
        f":bar_chart: Sweep ({sender}): "
        f"{connects_today} invites sent today, "
        f"{followups_today} LinkedIn follow-ups sent today, "
        f"{email_followups_today} email follow-ups sent today"
    )
    payload = {"text": fallback, "blocks": blocks}
    _post_to_slack(SLACK_WEBHOOK_URL, payload, f"sweep-summary ({sender})")


def notify_status_summary(*, rows: list[dict], since, generated_at) -> None:
    """Post an all-sender hourly send-count snapshot to the ops channel."""
    if not SLACK_WEBHOOK_URL:
        return

    def line(label: str, key: str) -> str:
        parts = [f"{row['sender']} - {row.get(key, 0)}" for row in rows]
        return f"*{label}:* " + ", ".join(parts)

    body = "\n".join([
        line("Sent today", "connects_today"),
        line("LinkedIn follow-ups today", "linkedin_followups_today"),
        line("Email follow-ups today", "email_followups_today"),
        line("Manual replies today", "manual_replies_today"),
        line("Newly accepted since last status", "newly_connected"),
        line("Connect tasks run today", "connect_runs_today"),
        line("Qualified remaining", "qualified_remaining"),
    ])
    window = (
        f"_Window: {since:%Y-%m-%d %H:%M} to {generated_at:%H:%M} "
        f"{generated_at.tzname() or 'local'}_"
    )
    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": ":bar_chart: *Hourly sender status*"},
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": body}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": window}]},
    ]
    fallback = "Hourly sender status: " + " | ".join(
        f"{row['sender']} {row.get('connects_today', 0)} invites, "
        f"{row.get('linkedin_followups_today', 0)} LI FUs, "
        f"{row.get('email_followups_today', 0)} email FUs"
        for row in rows
    )
    _post_to_slack(SLACK_WEBHOOK_URL, {"text": fallback, "blocks": blocks}, "status-summary")


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
    _post_to_slack(SLACK_WEBHOOK_URL, payload, f"error ({workflow})")


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
