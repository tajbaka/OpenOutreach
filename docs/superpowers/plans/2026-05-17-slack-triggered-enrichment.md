# Slack-Triggered Phone Enrichment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make phone enrichment operator-initiated from a Slack select menu on the inbound-reply notification, instead of running automatically on every reply.

**Architecture:** A Slack interactive `static_select` is added to the reply notification. Slack POSTs the operator's choice to a Vercel Python serverless function (`api/slack_enrich.py`) which verifies the request signature and INSERTs an `enrich_phone` Task into Neon. The daemon's `EnrichmentWorker` — now always running — polls that table and routes to either the full waterfall or a single provider based on a new `provider` field in the task payload. Auto-enrichment stays in code, re-gated behind a renamed flag, default off.

**Tech Stack:** Python, Django ORM (daemon side), raw `psycopg` (Vercel side), Vercel serverless functions, Slack Block Kit + Interactivity, pytest.

Source spec: `docs/superpowers/specs/2026-05-17-slack-triggered-enrichment-design.md`.

---

## File Structure

**Modified:**
- `linkedin/conf.py` — rename `ENABLE_PHONE_ENRICHMENT` → `ENABLE_AUTO_PHONE_ENRICHMENT`.
- `linkedin/daemon.py` — always spawn `EnrichmentWorker`; drop the flag from the queue-empty exit guard; update the conf import.
- `linkedin/realtime/handler.py` — re-gate `_maybe_enqueue_enrichment` on the renamed flag; write `provider: "waterfall"` into the task payload.
- `linkedin/enrichment/waterfall.py` — add `PROVIDERS_BY_NAME` lookup.
- `linkedin/tasks/enrich_phone.py` — route on `task.payload["provider"]`.
- `linkedin/notifications/slack.py` — `notify_message_received` gains the `actions` select block.
- `tests/test_conf.py`, `tests/realtime/test_handler.py` — follow the rename.
- `CLAUDE.md`, `ARCHITECTURE.md` — doc sync.

**Created:**
- `api/slack_enrich.py` — Vercel serverless function (pure units + `handler`).
- `api/requirements.txt` — `psycopg[binary]` for the Vercel build.
- `vercel.json` — minimal no-framework Vercel config.
- `tests/test_slack_enrich.py` — unit tests for the Vercel function.

---

## Task 1: Rename the enrichment flag and split worker lifecycle from it

**Files:**
- Modify: `linkedin/conf.py:186-195`
- Modify: `linkedin/daemon.py:18-30` (import block), `linkedin/daemon.py:450-495`
- Modify: `linkedin/realtime/handler.py:19`, `linkedin/realtime/handler.py:36-67`
- Modify: `tests/test_conf.py:71-91`
- Modify: `tests/realtime/test_handler.py` (the three `ENABLE_PHONE_ENRICHMENT` patch targets)

This is one coherent task: the flag rename is inseparable from its three consumers, so the repo is only green once all of them move together. Auto-enqueue stays gated (renamed flag); the worker spawn and the queue-empty guard stop being gated entirely.

- [ ] **Step 1: Update the conf test class to the new flag name**

In `tests/test_conf.py`, replace the body of `test_flag_defaults_off` and `test_flag_truthy_strings_enable` inside `class TestPhoneEnrichmentConfig` so they reference `ENABLE_AUTO_PHONE_ENRICHMENT`:

```python
    def test_flag_defaults_off(self, monkeypatch):
        import importlib
        import linkedin.conf as conf
        monkeypatch.delenv("ENABLE_AUTO_PHONE_ENRICHMENT", raising=False)
        # conf.py runs load_dotenv() on import — stub it so the reload below
        # reflects the process env (the delenv above), not the on-disk .env.
        monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)
        importlib.reload(conf)
        assert conf.ENABLE_AUTO_PHONE_ENRICHMENT is False
        monkeypatch.undo()
        importlib.reload(conf)

    def test_flag_truthy_strings_enable(self, monkeypatch):
        import importlib
        import linkedin.conf as conf
        for raw in ("1", "true", "YES", "on"):
            monkeypatch.setenv("ENABLE_AUTO_PHONE_ENRICHMENT", raw)
            importlib.reload(conf)
            assert conf.ENABLE_AUTO_PHONE_ENRICHMENT is True
        importlib.reload(conf)
```

- [ ] **Step 2: Run the conf tests — verify they fail**

Run: `.venv/bin/python -m pytest tests/test_conf.py::TestPhoneEnrichmentConfig -v`
Expected: FAIL — `AttributeError: module 'linkedin.conf' has no attribute 'ENABLE_AUTO_PHONE_ENRICHMENT'`.

- [ ] **Step 3: Rename the flag in `conf.py`**

In `linkedin/conf.py`, replace the flag block (currently lines 186-195) with:

```python
# ----------------------------------------------------------------------
# Phone enrichment (multi-provider waterfall — see linkedin/enrichment/)
# ----------------------------------------------------------------------
# Gates ONLY the realtime listener's auto-enqueue of an enrich_phone task on
# every inbound reply. Default OFF — the operator triggers enrichment on
# demand from the Slack select menu instead (see api/slack_enrich.py). The
# EnrichmentWorker itself is NOT gated by this: it always runs, because the
# select menu is always present so enrichment must always be processable.
ENABLE_AUTO_PHONE_ENRICHMENT = os.getenv(
    "ENABLE_AUTO_PHONE_ENRICHMENT", "false",
).strip().lower() in {"1", "true", "yes", "on"}
```

- [ ] **Step 4: Run the conf tests — verify they pass**

Run: `.venv/bin/python -m pytest tests/test_conf.py::TestPhoneEnrichmentConfig -v`
Expected: PASS (all 4 tests in the class).

- [ ] **Step 5: Update the daemon's conf import**

In `linkedin/daemon.py`, in the `from linkedin.conf import (...)` block, replace the line `ENABLE_PHONE_ENRICHMENT,` with `ENABLE_AUTO_PHONE_ENRICHMENT,` (keep alphabetical position — it sorts the same).

- [ ] **Step 6: Make the worker spawn unconditionally**

In `linkedin/daemon.py` around line 453-456, replace:

```python
    from linkedin.enrichment.worker import EnrichmentWorker
    enrichment_worker = EnrichmentWorker()
    if ENABLE_PHONE_ENRICHMENT:
        enrichment_worker.start()
```

with:

```python
    # Always spawn the enrichment worker — the Slack select menu is always
    # available, so enrich_phone tasks must always be processable. The worker
    # is a cheap idle DB poll when no tasks exist.
    from linkedin.enrichment.worker import EnrichmentWorker
    enrichment_worker = EnrichmentWorker()
    enrichment_worker.start()
```

- [ ] **Step 7: Drop the flag from the queue-empty exit guard**

In `linkedin/daemon.py` around line 484, replace:

```python
                if ENABLE_PHONE_ENRICHMENT and Task.objects.filter(
                    task_type=Task.TaskType.ENRICH_PHONE,
                    status__in=[Task.Status.PENDING, Task.Status.RUNNING],
                ).exists():
```

with:

```python
                if Task.objects.filter(
                    task_type=Task.TaskType.ENRICH_PHONE,
                    status__in=[Task.Status.PENDING, Task.Status.RUNNING],
                ).exists():
```

- [ ] **Step 8: Re-gate the listener auto-enqueue on the renamed flag**

In `linkedin/realtime/handler.py` line 19, change the import:

```python
from linkedin.conf import ENABLE_AUTO_PHONE_ENRICHMENT
```

In `_maybe_enqueue_enrichment` (line ~36-67), update the docstring's flag name and the gate:

```python
def _maybe_enqueue_enrichment(lead) -> None:
    """Enqueue a phone-enrichment task for a freshly-replied lead.

    Gated by ENABLE_AUTO_PHONE_ENRICHMENT (default off — operators normally
    trigger enrichment from the Slack select menu). Skipped when the lead is
    already enriched, disqualified, or already has a PENDING/RUNNING
    enrich_phone task — the last guard prevents duplicate provider billing
    when a lead sends several messages before the EnrichmentWorker runs (the
    phone_enriched_at check alone cannot catch that — it is still None for
    both events).
    """
    if not ENABLE_AUTO_PHONE_ENRICHMENT:
        return
```

- [ ] **Step 9: Write `provider: "waterfall"` into the auto-enqueued payload**

In `linkedin/realtime/handler.py`, in the `Task.objects.create(...)` call inside `_maybe_enqueue_enrichment`, change the payload:

```python
    Task.objects.create(
        task_type=Task.TaskType.ENRICH_PHONE,
        scheduled_at=timezone.now(),
        payload={
            "lead_id": lead.id,
            "bettercontact_request_id": "",
            "provider": "waterfall",
        },
    )
```

- [ ] **Step 10: Update the handler tests' patch targets**

In `tests/realtime/test_handler.py`, replace every occurrence of
`linkedin.realtime.handler.ENABLE_PHONE_ENRICHMENT` with
`linkedin.realtime.handler.ENABLE_AUTO_PHONE_ENRICHMENT` (three `patch(...)` call sites — in `test_inbound_enqueues_enrichment_when_enabled`, `test_inbound_does_not_enqueue_when_disabled`, `test_enrichment_not_enqueued_for_already_enriched_lead`).

- [ ] **Step 11: Run the affected suites — verify they pass**

Run: `.venv/bin/python -m pytest tests/test_conf.py tests/realtime/test_handler.py -v`
Expected: PASS. Then confirm no stale references remain:
Run: `grep -rn "ENABLE_PHONE_ENRICHMENT" --include=*.py . | grep -v __pycache__`
Expected: no output.

- [ ] **Step 12: Commit**

```bash
git add linkedin/conf.py linkedin/daemon.py linkedin/realtime/handler.py tests/test_conf.py tests/realtime/test_handler.py
git commit -m "Split ENABLE_PHONE_ENRICHMENT into ENABLE_AUTO_PHONE_ENRICHMENT, always-on worker"
```

---

## Task 2: Provider routing in the waterfall and the task handler

**Files:**
- Modify: `linkedin/enrichment/waterfall.py:20-25`
- Modify: `linkedin/tasks/enrich_phone.py:14-43`
- Modify: `tests/enrichment/test_waterfall.py`
- Modify: `tests/tasks/test_enrich_phone.py`

- [ ] **Step 1: Write the failing test for `PROVIDERS_BY_NAME`**

Append to `tests/enrichment/test_waterfall.py`:

```python
def test_providers_by_name_maps_every_chain_provider():
    from linkedin.enrichment.waterfall import PROVIDER_CHAIN, PROVIDERS_BY_NAME

    assert set(PROVIDERS_BY_NAME) == {"bettercontact", "leadmagic", "prospeo"}
    for name, provider in PROVIDERS_BY_NAME.items():
        assert provider.name == name
        assert provider in PROVIDER_CHAIN
```

- [ ] **Step 2: Run it — verify it fails**

Run: `.venv/bin/python -m pytest tests/enrichment/test_waterfall.py::test_providers_by_name_maps_every_chain_provider -v`
Expected: FAIL — `ImportError: cannot import name 'PROVIDERS_BY_NAME'`.

- [ ] **Step 3: Add `PROVIDERS_BY_NAME` to `waterfall.py`**

In `linkedin/enrichment/waterfall.py`, immediately after the `PROVIDER_CHAIN = [...]` list, add:

```python
# Name → provider, for single-provider routing (the Slack "X only" options).
# See linkedin/tasks/enrich_phone.py and api/slack_enrich.py.
PROVIDERS_BY_NAME = {p.name: p for p in PROVIDER_CHAIN}
```

- [ ] **Step 4: Run it — verify it passes**

Run: `.venv/bin/python -m pytest tests/enrichment/test_waterfall.py -v`
Expected: PASS (all tests in the file).

- [ ] **Step 5: Write the failing tests for provider routing in `handle_enrich_phone`**

Append to `tests/tasks/test_enrich_phone.py`:

```python
def _task_with_provider(lead, provider):
    return Task.objects.create(
        task_type=Task.TaskType.ENRICH_PHONE,
        scheduled_at=timezone.now(),
        payload={
            "lead_id": lead.id,
            "bettercontact_request_id": "",
            "provider": provider,
        },
    )


@pytest.mark.django_db
def test_waterfall_provider_runs_full_chain():
    lead = _lead()
    task = _task_with_provider(lead, "waterfall")
    found = EnrichmentResult(
        status=EnrichmentStatus.FOUND, provider="bettercontact", phone="+1",
    )
    with patch("linkedin.tasks.enrich_phone.run_waterfall", return_value=found) as wf, \
         patch("linkedin.tasks.enrich_phone.notify_phone_enriched"):
        handle_enrich_phone(task)
    # waterfall → run_waterfall called with no `chain` kwarg.
    assert wf.call_count == 1
    assert "chain" not in wf.call_args.kwargs


@pytest.mark.django_db
def test_absent_provider_runs_full_chain():
    lead = _lead()
    task = _task(lead)  # legacy payload — no "provider" key
    found = EnrichmentResult(
        status=EnrichmentStatus.FOUND, provider="bettercontact", phone="+1",
    )
    with patch("linkedin.tasks.enrich_phone.run_waterfall", return_value=found) as wf, \
         patch("linkedin.tasks.enrich_phone.notify_phone_enriched"):
        handle_enrich_phone(task)
    assert "chain" not in wf.call_args.kwargs


@pytest.mark.django_db
def test_single_provider_runs_one_element_chain():
    from linkedin.enrichment.waterfall import PROVIDERS_BY_NAME

    lead = _lead()
    task = _task_with_provider(lead, "leadmagic")
    found = EnrichmentResult(
        status=EnrichmentStatus.FOUND, provider="leadmagic", phone="+1",
    )
    with patch("linkedin.tasks.enrich_phone.run_waterfall", return_value=found) as wf, \
         patch("linkedin.tasks.enrich_phone.notify_phone_enriched"):
        handle_enrich_phone(task)
    assert wf.call_args.kwargs["chain"] == [PROVIDERS_BY_NAME["leadmagic"]]


@pytest.mark.django_db
def test_unknown_provider_falls_back_to_full_chain():
    lead = _lead()
    task = _task_with_provider(lead, "bogus")
    found = EnrichmentResult(
        status=EnrichmentStatus.FOUND, provider="bettercontact", phone="+1",
    )
    with patch("linkedin.tasks.enrich_phone.run_waterfall", return_value=found) as wf, \
         patch("linkedin.tasks.enrich_phone.notify_phone_enriched"):
        handle_enrich_phone(task)
    assert "chain" not in wf.call_args.kwargs
```

- [ ] **Step 6: Run them — verify they fail**

Run: `.venv/bin/python -m pytest tests/tasks/test_enrich_phone.py -v -k "provider or chain"`
Expected: FAIL — `test_single_provider_runs_one_element_chain` fails (handler ignores `provider`, calls `run_waterfall` without `chain`).

- [ ] **Step 7: Add provider routing to `handle_enrich_phone`**

In `linkedin/tasks/enrich_phone.py`, change the import line:

```python
from linkedin.enrichment.waterfall import PROVIDERS_BY_NAME, run_waterfall
```

Replace the line `result = run_waterfall(lead, task)` with:

```python
    provider = task.payload.get("provider", "waterfall")
    if provider == "waterfall":
        result = run_waterfall(lead, task)
    else:
        chosen = PROVIDERS_BY_NAME.get(provider)
        if chosen is None:
            logger.warning(
                "enrich_phone: unknown provider %r — running full waterfall",
                provider,
            )
            result = run_waterfall(lead, task)
        else:
            result = run_waterfall(lead, task, chain=[chosen])
```

- [ ] **Step 8: Run the full task suite — verify it passes**

Run: `.venv/bin/python -m pytest tests/tasks/test_enrich_phone.py tests/enrichment/ -v`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add linkedin/enrichment/waterfall.py linkedin/tasks/enrich_phone.py tests/enrichment/test_waterfall.py tests/tasks/test_enrich_phone.py
git commit -m "Route enrich_phone tasks to a single provider via payload provider field"
```

---

## Task 3: Add the provider-select menu to the reply notification

**Files:**
- Modify: `linkedin/notifications/slack.py:123-185` (`notify_message_received`)
- Modify: `tests/test_slack_notify.py`

- [ ] **Step 1: Write the failing test for the select block**

Append to `tests/test_slack_notify.py` (it already imports `json`, `patch`, `pytest`, and `slack_mod`). Add a Lead-building helper if the file lacks one — use a minimal stub object so no DB is needed:

```python
class _StubLead:
    """Minimal duck-typed Lead for notify_message_received block assertions."""

    def __init__(self, lead_id=42):
        self.id = lead_id
        self.first_name = "Ada"
        self.last_name = "Lovelace"
        self.public_identifier = "ada"
        self.linkedin_url = "https://www.linkedin.com/in/ada/"
        self.company_name = "Analytical Engines"


def _captured_blocks(mock_urlopen):
    """Pull the Block Kit `blocks` list out of the mocked webhook POST."""
    req = mock_urlopen.call_args[0][0]
    return json.loads(req.data.decode("utf-8"))["blocks"]


def test_message_received_includes_provider_select_block():
    lead = _StubLead(lead_id=42)
    with patch.object(slack_mod, "SLACK_WEBHOOK_URL", "https://hooks.slack.test/x"), \
         patch.object(slack_mod.request, "urlopen") as mock_urlopen:
        mock_urlopen.return_value.__enter__.return_value.status = 200
        slack_mod.notify_message_received(lead=lead, text="hi", operator="Arian")

    blocks = _captured_blocks(mock_urlopen)
    actions = [b for b in blocks if b.get("type") == "actions"]
    assert len(actions) == 1
    select = actions[0]["elements"][0]
    assert select["type"] == "static_select"
    assert select["action_id"] == "enrich_phone_select"
    values = [opt["value"] for opt in select["options"]]
    assert values == [
        "42:waterfall", "42:bettercontact", "42:leadmagic", "42:prospeo",
    ]
```

- [ ] **Step 2: Run it — verify it fails**

Run: `.venv/bin/python -m pytest tests/test_slack_notify.py::test_message_received_includes_provider_select_block -v`
Expected: FAIL — `assert len(actions) == 1` fails (0 actions blocks).

- [ ] **Step 3: Add the actions block to `notify_message_received`**

In `linkedin/notifications/slack.py`, inside `notify_message_received`, after the existing `if elements: blocks.append({"type": "context", ...})` line and before `payload = {"text": fallback, "blocks": blocks}`, add:

```python
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
```

- [ ] **Step 4: Run the slack-notify suite — verify it passes**

Run: `.venv/bin/python -m pytest tests/test_slack_notify.py -v`
Expected: PASS (the new test plus all existing ones).

- [ ] **Step 5: Commit**

```bash
git add linkedin/notifications/slack.py tests/test_slack_notify.py
git commit -m "Add phone-enrichment provider select menu to the reply notification"
```

---

## Task 4: The Vercel serverless function

**Files:**
- Create: `api/slack_enrich.py`
- Create: `tests/test_slack_enrich.py`

- [ ] **Step 1: Write the Vercel function**

Create `api/slack_enrich.py`:

```python
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
    """INSERT an enrich_phone Task for `lead_id` unless one is already
    pending/running. Returns True if a row was inserted, False if deduped.

    Dedup is best-effort (a TOCTOU window exists across concurrent function
    invocations) — a duplicate Task is harmless: the single-threaded
    EnrichmentWorker runs tasks in series and the second sees
    phone_enriched_at already set, or re-attempts an unbilled API_FAILURE.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM linkedin_task "
            "WHERE task_type = 'enrich_phone' "
            "AND status IN ('pending', 'running') "
            "AND (payload->>'lead_id')::int = %s LIMIT 1",
            (lead_id,),
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
```

- [ ] **Step 2: Write the unit tests**

Create `tests/test_slack_enrich.py`:

```python
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
import time
from unittest.mock import MagicMock

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
    from urllib.parse import urlencode

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
```

- [ ] **Step 3: Run the tests — verify they pass**

Run: `.venv/bin/python -m pytest tests/test_slack_enrich.py -v`
Expected: PASS (all 10 tests). If `psycopg` import fails, the project venv is missing it — it should be present (Neon uses it); install with `.venv/bin/pip install 'psycopg[binary]'` only if needed.

- [ ] **Step 4: Commit**

```bash
git add api/slack_enrich.py tests/test_slack_enrich.py
git commit -m "Add api/slack_enrich.py Vercel function for Slack-triggered enrichment"
```

---

## Task 5: Vercel deployment config

**Files:**
- Create: `vercel.json`
- Create: `api/requirements.txt`

- [ ] **Step 1: Create `api/requirements.txt`**

```
psycopg[binary]
```

- [ ] **Step 2: Create `vercel.json`**

The repo is a Django app, not a Vercel framework project — pin no framework and no build so Vercel only deploys the `api/` function.

```json
{
  "$schema": "https://openapi.vercel.sh/vercel.json",
  "framework": null,
  "buildCommand": null,
  "installCommand": null
}
```

- [ ] **Step 3: Verify the JSON is well-formed**

Run: `.venv/bin/python -c "import json; json.load(open('vercel.json'))"`
Expected: no output, exit 0.

- [ ] **Step 4: Commit**

```bash
git add vercel.json api/requirements.txt
git commit -m "Add Vercel config and function requirements for slack_enrich"
```

---

## Task 6: Documentation sync

**Files:**
- Modify: `CLAUDE.md`
- Modify: `ARCHITECTURE.md`

- [ ] **Step 1: Update `CLAUDE.md`**

In the `conf.py` phone-enrichment constants list, change `ENABLE_PHONE_ENRICHMENT` to `ENABLE_AUTO_PHONE_ENRICHMENT`.

In the **Phone enrichment** architecture bullet, rewrite the trigger description to reflect the new flow. Replace the sentence describing the realtime-listener auto-enqueue + `ENABLE_PHONE_ENRICHMENT` gating with:

> Enrichment is **operator-triggered from Slack**: the inbound-reply notification carries a "📞 Get phone number" select menu (waterfall / bettercontact / leadmagic / prospeo). The pick is POSTed to a Vercel serverless function (`api/slack_enrich.py`) which verifies the Slack signature and INSERTs an `enrich_phone` Task into Neon — the `Task` table is the entire contract between the function and the daemon. The realtime listener can still auto-enqueue on every inbound reply, gated by `ENABLE_AUTO_PHONE_ENRICHMENT` (default off). The `EnrichmentWorker` always runs (no longer flag-gated). The task payload carries a `provider` field — `"waterfall"` runs the full `BetterContact → LeadMagic → Prospeo` chain; a specific provider name runs that provider only, with no failover.

- [ ] **Step 2: Update `ARCHITECTURE.md`**

Find the phone-enrichment section (search for `ENABLE_PHONE_ENRICHMENT` / `EnrichmentWorker`). Apply the same three changes: the flag rename, the always-on worker, and the new Slack-trigger flow via `api/slack_enrich.py` with the `provider` payload field. If `ARCHITECTURE.md` documents the realtime listener's `_maybe_enqueue_enrichment`, note it is now gated by `ENABLE_AUTO_PHONE_ENRICHMENT` and writes `provider: "waterfall"`.

Run first to locate the section:
Run: `grep -n "ENABLE_PHONE_ENRICHMENT\|EnrichmentWorker\|enrich_phone" ARCHITECTURE.md`

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md ARCHITECTURE.md
git commit -m "Document Slack-triggered phone enrichment"
```

---

## Final verification

- [ ] **Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS. Investigate any failure before considering the plan complete.

- [ ] **Confirm no stale flag references**

Run: `grep -rn "ENABLE_PHONE_ENRICHMENT" --include=*.py --include=*.md . | grep -v __pycache__ | grep -v docs/superpowers/specs`
Expected: no output (the design spec files under `specs/` may still use the old name historically — that is fine).

---

## Notes for the deploying operator (out of plan scope, manual)

After the code ships, one-time manual setup is required (per the design doc):
1. Create/point a Vercel project at this repo; set env vars `DATABASE_URL` (OpenOutreach's Neon connection string) and `SLACK_SIGNING_SECRET`.
2. On the Slack app that owns the incoming webhook: enable **Interactivity & Shortcuts** and set the **Request URL** to `https://<project>.vercel.app/api/slack_enrich`.
3. Copy the Slack app's **Signing Secret** into the Vercel project env.
4. Set `ENABLE_AUTO_PHONE_ENRICHMENT=false` in the daemon's `.env` (the target state — auto off, Slack menu always available).
