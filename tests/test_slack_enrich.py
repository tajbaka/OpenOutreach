"""Unit tests for the api/slack_enrich.py Vercel function.

The function lives outside any package (Vercel treats every file in api/ as a
serverless function), so it is loaded by path with importlib rather than a
normal import.
"""
import hashlib
import hmac
import importlib.util
import json
import pathlib
from unittest.mock import MagicMock
from urllib.parse import urlencode

import pytest

_PATH = pathlib.Path(__file__).resolve().parent.parent / "api" / "slack_enrich.py"
_spec = importlib.util.spec_from_file_location("slack_enrich", _PATH)
slack_enrich = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(slack_enrich)


_SECRET = "test-signing-secret"


def _sign(body: str, timestamp: str, secret: str = _SECRET) -> str:
    basestring = f"v0:{timestamp}:{body}".encode("utf-8")
    return "v0=" + hmac.new(
        secret.encode("utf-8"), basestring, hashlib.sha256,
    ).hexdigest()


def test_verify_signature_accepts_a_valid_signature():
    now = 1_700_000_000
    body = "payload=%7B%7D"
    ts = str(now)
    sig = _sign(body, ts)
    assert slack_enrich.verify_signature(
        body, ts, sig, secret=_SECRET, now=now,
    ) is True


def test_verify_signature_rejects_a_bad_signature():
    now = 1_700_000_000
    assert slack_enrich.verify_signature(
        "payload=%7B%7D", str(now), "v0=deadbeef", secret=_SECRET, now=now,
    ) is False


def test_verify_signature_rejects_a_stale_timestamp():
    now = 1_700_000_000
    stale = now - 60 * 10  # 10 minutes old
    body = "payload=%7B%7D"
    sig = _sign(body, str(stale))
    assert slack_enrich.verify_signature(
        body, str(stale), sig, secret=_SECRET, now=now,
    ) is False


def test_verify_signature_rejects_missing_headers():
    assert slack_enrich.verify_signature(
        "body", "", "", secret=_SECRET, now=1_700_000_000,
    ) is False


def _interaction_body(value: str) -> str:
    payload = {"actions": [{"selected_option": {"value": value}}]}
    return urlencode({"payload": json.dumps(payload)})


def test_parse_interaction_extracts_lead_and_provider():
    body = _interaction_body("42:leadmagic")
    assert slack_enrich.parse_interaction(body) == (42, "leadmagic")


def test_parse_interaction_handles_waterfall():
    body = _interaction_body("7:waterfall")
    assert slack_enrich.parse_interaction(body) == (7, "waterfall")


def test_parse_interaction_rejects_missing_payload():
    with pytest.raises(ValueError):
        slack_enrich.parse_interaction("notpayload=x")


def test_parse_interaction_rejects_value_without_colon():
    with pytest.raises(ValueError):
        slack_enrich.parse_interaction(_interaction_body("nocolon"))


def _mock_conn(existing: bool):
    """A psycopg-shaped mock whose dedup SELECT returns a row iff `existing`."""
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value = (1,) if existing else None
    return conn, cur


def test_enqueue_task_inserts_when_none_exists():
    conn, cur = _mock_conn(existing=False)
    inserted = slack_enrich.enqueue_task(conn, 42, "leadmagic")
    assert inserted is True
    # Two execute calls: the dedup SELECT then the INSERT.
    assert cur.execute.call_count == 2
    insert_sql = cur.execute.call_args_list[1][0][0]
    assert "INSERT INTO linkedin_task" in insert_sql
    conn.commit.assert_called_once()


def test_enqueue_task_dedups_when_one_is_pending():
    conn, cur = _mock_conn(existing=True)
    inserted = slack_enrich.enqueue_task(conn, 42, "leadmagic")
    assert inserted is False
    # Only the dedup SELECT ran — no INSERT.
    assert cur.execute.call_count == 1
    conn.commit.assert_not_called()
