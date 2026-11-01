"""RB2B -> durable intake queue; reviewed people become CRM contacts.

RB2B supports a self-contained HTTPS URL, not custom auth headers. Configure
?token=<RB2B_WEBHOOK_TOKEN>; never log the request path. The token only accepts
incoming visitor records. This endpoint has no read, send or campaign API.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler
import json
import os
import re
from urllib.parse import parse_qs, unquote, urlsplit

import psycopg
from psycopg.types.json import Jsonb


MAX_BODY_BYTES = 65536
META_KEY = "rb2b_intake"
FIELDS = (
    "LinkedIn URL", "First Name", "Last Name", "Title", "Company Name",
    "Business Email", "Website", "Industry", "Employee Count", "Estimate Revenue",
    "City", "State", "Zipcode", "Seen At", "Referrer", "Captured URL", "Tags",
    "is_repeat_visit", "is_repeat_visitor",
)


class InvalidVisitor(ValueError):
    pass


class IdentityConflict(ValueError):
    pass


def authorized(path: str, secret: str) -> bool:
    values = parse_qs(urlsplit(path).query).get("token", [])
    return bool(len(secret) >= 32 and len(values) == 1
                and hmac.compare_digest(values[0].encode(), secret.encode()))


def normalize_visitor(raw: object) -> dict:
    if not isinstance(raw, dict):
        raise InvalidVisitor("expected_object")
    data = {}
    for field in FIELDS:
        value = raw.get(field)
        if value is None:
            value = ""
        if not isinstance(value, (str, int, float, bool)):
            raise InvalidVisitor("invalid_field_type")
        if isinstance(value, str):
            value = value.strip()
        if len(str(value)) > 4096:
            raise InvalidVisitor("field_too_long")
        data[field] = value
    # The provider's documented test identifies a real founder profile. Never
    # insert that person as a visitor because somebody pressed Test.
    if (data["First Name"], data["Last Name"], data["Title"]) == (
        "RB2B", "Test Payload", "Webhook Test Payload from RB2B",
    ):
        return {"kind": "test", "data": data}
    raw_url = data["LinkedIn URL"]
    if not raw_url:
        return {"kind": "company_only", "data": data}
    if not isinstance(raw_url, str):
        raise InvalidVisitor("invalid_linkedin_url")
    url = urlsplit(raw_url)
    if (url.scheme not in {"http", "https"} or url.hostname not in {"linkedin.com", "www.linkedin.com"}
            or url.username or url.password or url.port):
        raise InvalidVisitor("invalid_linkedin_url")
    match = re.fullmatch(r"/in/([^/]+)/?", url.path)
    pid = unquote(match.group(1)).lower() if match else ""
    if not re.fullmatch(r"[\w-]{1,200}", pid, re.UNICODE):
        raise InvalidVisitor("invalid_linkedin_identity")
    canonical = "https://www.linkedin.com/in/" + pid + "/"
    if not isinstance(data["Seen At"], str) or not data["Seen At"]:
        raise InvalidVisitor("missing_seen_at")
    data["LinkedIn URL"] = canonical
    # Full normalized payload hash preserves edits as separate evidence; the
    # review must deduplicate outreach by person, not merely by event hash.
    fingerprint = hashlib.sha256(json.dumps(
        data, sort_keys=True, ensure_ascii=False, allow_nan=False,
    ).encode()).hexdigest()
    return {"kind": "person", "data": data, "public_id": pid,
            "url": canonical, "fingerprint": fingerprint}


def merge_receipt(profile_data: object, visitor: dict, *, received_at: str):
    # Django's psycopg JSON loader can return text; the standalone receiver's
    # loader normally returns a dict. Both must preserve the same JSON object.
    if isinstance(profile_data, str):
        try:
            profile_data = json.loads(profile_data)
        except ValueError:
            raise IdentityConflict("invalid_existing_profile_data") from None
    if not isinstance(profile_data, dict):
        raise IdentityConflict("invalid_existing_profile_data")
    merged = deepcopy(profile_data)
    meta = merged.setdefault(META_KEY, {"schema_version": 1, "events": {}})
    if (not isinstance(meta, dict) or meta.get("schema_version") != 1
            or not isinstance(meta.get("events"), dict)):
        raise IdentityConflict("conflicting_intake_metadata")
    events = meta["events"]
    key = visitor["fingerprint"]
    duplicate = key in events
    if duplicate:
        event = events[key]
        if not isinstance(event, dict) or not isinstance(event.get("receipt_count"), int):
            raise IdentityConflict("invalid_existing_receipt")
        # Do not reset review status, decisions, notes or send evidence.
        event["receipt_count"] += 1
        event["last_received_at"] = received_at
    else:
        events[key] = {"payload": visitor["data"], "review_status": "new",
                       "first_received_at": received_at, "last_received_at": received_at,
                       "receipt_count": 1, "review": {}}
    return merged, duplicate


def verify_storage(conn):
    """Read-only readiness check; deploying before migration fails closed."""
    conn.execute("SELECT events,lead_id,status FROM linkedin_rb2bvisitor LIMIT 0")
    profiles = conn.execute(
        "SELECT p.id FROM linkedin_linkedinprofile p "
        "JOIN auth_user u ON u.id=p.user_id WHERE u.username=%s AND p.active=true",
        ("ariantajbakh",),
    ).fetchall()
    if len(profiles) != 1:
        raise IdentityConflict("arian_owner_not_unique")
    return profiles[0][0]


def store_visitor(conn, visitor: dict) -> dict:
    # Main is auto-deployed by Vercel. Preserve intake until the connected
    # Windows host applies the additive schema. The migration commits atomically;
    # subsequent requests switch to the new table without dropping an event.
    if conn.execute("SELECT to_regclass('linkedin_rb2bvisitor')").fetchone()[0] is None:
        return store_legacy_visitor(conn, visitor)
    profile_id = verify_storage(conn)
    if visitor["kind"] != "person":
        return {"accepted": True, "kind": visitor["kind"], "stored": False, "storage_version": 2}
    pid, canonical = visitor["public_id"], visitor["url"]
    lock = int.from_bytes(hashlib.sha256(("rb2b:" + pid).encode()).digest()[:8], "big", signed=True)
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (lock,))
    rows = conn.execute(
        "SELECT id,events,linkedin_profile_id FROM linkedin_rb2bvisitor "
        "WHERE public_identifier=%s FOR UPDATE", (pid,),
    ).fetchall()
    now = datetime.now(timezone.utc).isoformat()
    events = rows[0][1] if rows else {}
    if isinstance(events, str):
        events = json.loads(events)
    if rows and rows[0][2] != profile_id:
        raise IdentityConflict("sender_conflict")
    merged, duplicate = merge_receipt({META_KEY: {"schema_version": 1, "events": events}},
                                      visitor, received_at=now)
    events = merged[META_KEY]["events"]
    if rows:
        record_id = rows[0][0]
        # Receipt changes cannot reset review, CRM linkage or send evidence.
        conn.execute("UPDATE linkedin_rb2bvisitor SET events=%s,updated_at=NOW() WHERE id=%s",
                     (Jsonb(events), record_id))
    else:
        record_id = conn.execute("""
            INSERT INTO linkedin_rb2bvisitor
            (public_identifier,linkedin_url,linkedin_profile_id,events,review,member_urn,note,status,
             outcome,created_at,updated_at)
            VALUES (%s,%s,%s,%s,'{}','','','new','{}',NOW(),NOW()) RETURNING id
        """, (pid, canonical, profile_id, Jsonb(events))).fetchone()[0]
    check = conn.execute("SELECT events FROM linkedin_rb2bvisitor WHERE id=%s", (record_id,)).fetchone()[0]
    if isinstance(check, str):
        check = json.loads(check)
    if check[visitor["fingerprint"]] != events[visitor["fingerprint"]]:
        raise IdentityConflict("receipt_readback_mismatch")
    return {"accepted": True, "kind": "person", "stored": True, "storage_version": 2,
            "visitor_id": record_id, "event_hash": visitor["fingerprint"], "duplicate": duplicate}


# Transitional v1 writer only while migration 0038 is absent. It may be
# removed after all hosts and the live receiver have verified the v2 cutover.
def verify_legacy_storage(conn):
    """Read-only readiness check for the existing table and exact owner."""
    conn.execute("SELECT profile_data, public_identifier, linkedin_url FROM linkedin_linkedindiscoverylead LIMIT 0")
    profiles = conn.execute(
        "SELECT p.linkedin_username FROM linkedin_linkedinprofile p "
        "JOIN auth_user u ON u.id=p.user_id WHERE u.username=%s AND p.active=true",
        ("ariantajbakh",),
    ).fetchall()
    if len(profiles) != 1:
        raise IdentityConflict("arian_owner_not_unique")
    return profiles[0][0]


def store_legacy_visitor(conn, visitor: dict) -> dict:
    username = verify_legacy_storage(conn)
    if visitor["kind"] != "person":
        return {"accepted": True, "kind": visitor["kind"], "stored": False, "storage_version": 1}
    pid, canonical = visitor["public_id"], visitor["url"]
    # Transaction-scoped only: released on commit/rollback, including through
    # poolers. Serializes webhook retries even before a discovery row exists.
    lock = int.from_bytes(hashlib.sha256(("rb2b:" + pid).encode()).digest()[:8], "big", signed=True)
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (lock,))
    rows = conn.execute(
        "SELECT id,profile_data FROM linkedin_linkedindiscoverylead "
        "WHERE lower(public_identifier)=%s OR lower(rtrim(linkedin_url,'/'))=%s FOR UPDATE",
        (pid, canonical.rstrip("/")),
    ).fetchall()
    if len(rows) > 1:
        raise IdentityConflict("ambiguous_discovery_identity")
    now = datetime.now(timezone.utc).isoformat()
    data = visitor["data"]
    if rows:
        record_id, profile = rows[0]
        merged, duplicate = merge_receipt(profile, visitor, received_at=now)
        # Preserve collector-owned profile fields and classification.
        conn.execute(
            "UPDATE linkedin_linkedindiscoverylead SET profile_data=%s,updated_at=NOW() WHERE id=%s",
            (Jsonb(merged), record_id),
        )
    else:
        crm_ids = [row[0] for row in conn.execute(
            "SELECT id FROM crm_lead WHERE lower(public_identifier)=%s "
            "OR lower(rtrim(linkedin_url,'/'))=%s",
            (pid, canonical.rstrip("/")),
        ).fetchall()]
        profile = {"source": "rb2b_webhook", "public_identifier": pid,
                   "url": canonical, "identity_verified": False,
                   "existing_crm_lead_ids_at_intake": crm_ids}
        merged, duplicate = merge_receipt(profile, visitor, received_at=now)
        first, last = str(data["First Name"])[:100], str(data["Last Name"])[:100]
        location = ", ".join(str(data[k]) for k in ("City", "State") if data[k])[:300]
        record_id = conn.execute("""
            INSERT INTO linkedin_linkedindiscoverylead
            (public_identifier,linkedin_url,member_urn,first_name,last_name,full_name,
             headline,company_name,location,profile_data,stored_by_operator,
             stored_by_account_username,potential_icp,last_seen_at,last_profiled_at,created_at,updated_at)
            VALUES (%s,%s,'',%s,%s,%s,%s,%s,%s,%s,'Arian',%s,'RB2B: needs review',NOW(),NOW(),NOW(),NOW())
            RETURNING id
        """, (pid, canonical, first, last, (first + " " + last).strip()[:220],
              str(data["Title"]), str(data["Company Name"])[:300], location, Jsonb(merged), username)).fetchone()[0]
    # Verify the saved receipt in this transaction before acknowledging it.
    check = conn.execute("SELECT profile_data FROM linkedin_linkedindiscoverylead WHERE id=%s", (record_id,)).fetchone()[0]
    if isinstance(check, str):
        check = json.loads(check)
    if check[META_KEY]["events"][visitor["fingerprint"]] != merged[META_KEY]["events"][visitor["fingerprint"]]:
        raise IdentityConflict("receipt_readback_mismatch")
    return {"accepted": True, "kind": "person", "stored": True,
            "storage_version": 1, "discovery_id": record_id, "event_hash": visitor["fingerprint"], "duplicate": duplicate}


_last_failure_alert = 0.0


def notify_intake_failure(reason):
    """Best-effort provider-independent alert when intake cannot be committed.

    No visitor data, request path, database exception or URL credential leaves
    the receiver. Warm-process throttling is not a global outage deduplicator.
    The non-2xx response remains the provider's retry signal even if Slack fails.
    """
    import time
    import ssl
    from urllib import request
    import certifi
    global _last_failure_alert
    token, channel = os.getenv("SLACK_BOT_TOKEN", ""), os.getenv("RB2B_SLACK_CHANNEL_ID", "")
    if not token or not channel or (_last_failure_alert and time.monotonic() - _last_failure_alert < 300):
        return
    _last_failure_alert = time.monotonic()
    req = request.Request("https://slack.com/api/chat.postMessage", method="POST",
        data=json.dumps({"channel": channel, "text": "RB2B intake failed: " + reason +
                         ". Visitor was not acknowledged; check receiver health."}).encode(),
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        with request.urlopen(req, timeout=3, context=ssl.create_default_context(cafile=certifi.where())) as response:
            response.read()
    except Exception:
        pass  # Never mask the original intake failure or expose secrets.


class handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # BaseHTTPRequestHandler logs paths, which contain RB2B's shared token.
        pass

    def _reply(self, status: int, payload: dict):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._reply(405, {"error": "method_not_allowed"})

    def do_POST(self):
        secret = os.environ.get("RB2B_WEBHOOK_TOKEN", "")
        # Pin intake to the current worker database without changing the
        # existing Slack function's independently configured DATABASE_URL.
        database = os.environ.get("RB2B_DATABASE_URL", "")
        if len(secret) < 32 or not database:
            self._reply(503, {"error": "intake_not_configured"})
            return
        if not authorized(self.path, secret):
            self._reply(401, {"error": "unauthorized"})
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            size = 0
        if not 0 < size <= MAX_BODY_BYTES:
            self._reply(413, {"error": "invalid_body_size"})
            return
        if self.headers.get("Content-Type", "").split(";", 1)[0].lower().strip() != "application/json":
            self._reply(415, {"error": "json_required"})
            return
        try:
            body = self.rfile.read(size)
            if len(body) != size:
                raise InvalidVisitor("incomplete_body")
            visitor = normalize_visitor(json.loads(body))
        except (ValueError, UnicodeError):
            self._reply(400, {"error": "invalid_visitor"})
            return
        try:
            with psycopg.connect(database, connect_timeout=5) as conn:
                conn.execute("SET LOCAL statement_timeout='8s'")
                conn.execute("SET LOCAL lock_timeout='3s'")
                result = store_visitor(conn, visitor)
            # The connection context commits before a 2xx acknowledgement.
        except IdentityConflict:
            notify_intake_failure("identity_or_storage_review_required")
            self._reply(409, {"error": "identity_or_storage_review_required"})
            return
        except psycopg.Error:
            notify_intake_failure("storage_unavailable")
            self._reply(503, {"error": "storage_unavailable"})
            return
        self._reply(200, result)
