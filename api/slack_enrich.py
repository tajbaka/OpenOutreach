"""Slack interaction handler — enqueues phone-enrichment tasks.

Deployed as a Vercel serverless Python function. Slack POSTs an interaction
payload here when the operator picks a provider from the "📞 Get phone
number" select menu on an inbound-reply notification. The function verifies
the Slack request signature, parses the chosen (lead_id, provider), and
INSERTs an enrich_phone Task into Neon — the same table the daemon's
EnrichmentWorker polls. The Task table is the entire contract between this
function and the daemon; they never talk directly.

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
from urllib.parse import parse_qs

import psycopg
from psycopg.types.json import Jsonb

SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")

# Slack rejects interactions older than 5 minutes; we mirror that as a
# replay guard on our side.
_MAX_SKEW_SECONDS = 60 * 5


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


def parse_interaction(body: str) -> tuple[int, str]:
    """Extract (lead_id, provider) from a Slack block-actions POST body.

    Slack sends application/x-www-form-urlencoded with a single `payload`
    field holding URL-encoded JSON. Raises ValueError on anything malformed.
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
    return int(lead_part), provider


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
            lead_id, provider = parse_interaction(body)
        except (ValueError, json.JSONDecodeError):
            self._respond_text(400, "malformed interaction")
            return

        try:
            with psycopg.connect(DATABASE_URL) as conn:
                inserted = enqueue_task(conn, lead_id, provider)
        except Exception:  # noqa: BLE001 — surface any DB failure as a 500
            self._respond_text(500, "database error")
            return

        if inserted:
            text = f"⏳ Fetching phone number via {provider}…"
        else:
            text = "⏳ Enrichment already queued for this lead."
        self._respond_message(text)

    def _respond_text(self, code: int, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _respond_message(self, text: str) -> None:
        """200 with a Slack message-replacement body — swaps the menu out."""
        body = json.dumps({
            "replace_original": True,
            "text": text,
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": text}},
            ],
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
