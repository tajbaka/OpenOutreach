# Realtime Inbound Message Listener Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Detect inbound LinkedIn DMs within seconds of arrival during the daemon's active hours by observing the browser's existing realtime SSE stream via Chrome DevTools Protocol.

**Architecture:** The daemon opens a second tab in its *existing* browser context, navigates it to `/messaging/` (which makes LinkedIn's own web client open a Server-Sent Events connection), attaches a CDP session, and subscribes to `Network.eventSourceMessageReceived`. A pure parser turns each raw event into a structured message or `None`; a handler persists inbound messages to `crm.Message` and posts a Slack notification. The daemon's idle `time.sleep()` is replaced with a chunked Playwright-pumping wait so CDP callbacks fire promptly, and that wait also refreshes a heartbeat file used by a daemon-startup catch-up to surface (and optionally backfill) any window the listener missed.

**Tech Stack:** Python 3.13, Django, Playwright (sync API) + `new_cdp_session`, pytest. No new dependencies.

**Source spec:** `docs/superpowers/specs/2026-05-16-realtime-message-listener-design.md`

---

## Wire format (captured 2026-05-16 — supersedes the design spec's assumption)

The design spec's chosen approach — CDP `Network.eventSourceMessageReceived` —
**does not work**. Task 2's capture spike established that LinkedIn's web
client does not use the native `EventSource` API; the realtime feed is a
long-lived streaming `fetch()` (`GET /realtime/connect`, response
`type=Fetch`, `mimeType=text/event-stream`), and `eventSourceMessageReceived`
never fires for it.

The working approach (still pure observation — no injected script, no second
connection): watch `Network.requestWillBeSent` for a `/realtime/connect`
URL → on `Network.responseReceived` call CDP `Network.streamResourceContent`
for that `requestId` → subsequent `Network.dataReceived` events then carry
the body bytes as base64. Decode → Server-Sent-Events text → frame into
individual `data: <json>` events → parse each.

Full findings, field paths, and the event taxonomy are in
`tests/fixtures/realtime/SHAPE.md`. The captured fixtures in
`tests/fixtures/realtime/` are the contract for the SSE-buffer and parser
tests. Tasks 3, 3B, 7, and 8 below reflect this corrected approach.

## File structure

New module `linkedin/realtime/` (one responsibility per file):

- `linkedin/realtime/__init__.py` — package marker.
- `linkedin/realtime/sse.py` — `RealtimeSSEBuffer`: accumulates raw stream
  text and yields decoded JSON events, handling cross-chunk splits. Pure.
- `linkedin/realtime/parser.py` — pure function `parse_realtime_event(event) -> ParsedRealtimeMessage | None`, where `event` is one decoded SSE event dict. No Django, no I/O.
- `linkedin/realtime/heartbeat.py` — read/write the per-account heartbeat JSON file in `data/`. Pure file-I/O.
- `linkedin/realtime/lead_lookup.py` — resolve a `crm.Lead` from a realtime event's conversation/sender URNs.
- `linkedin/realtime/handler.py` — `handle_realtime_event(event, *, operator)`: parse → resolve lead → persist → Slack. Try/except wrapped.
- `linkedin/realtime/listener.py` — `RealtimeListener` class (listener tab + CDP `streamResourceContent` wiring + SSE buffer + chunked pump) and `ensure_realtime_listener` / `stop_realtime_listener` lifecycle helpers.
- `linkedin/realtime/catchup.py` — daemon-startup gap computation + prompt/log + optional `backfill_messages` invocation.

Modified files:

- `linkedin/conf.py` — new `ENABLE_REALTIME_LISTENER` flag + two tuning constants.
- `linkedin/notifications/slack.py` — new `notify_message_received`.
- `linkedin/management/commands/backfill_messages.py` — new `--account` and `--skip-prereq-gate` options.
- `linkedin/daemon.py` — startup catch-up call; chunked pump replacing the queue-idle `time.sleep`; listener open/close around off-hours sleep.
- `CLAUDE.md`, `ARCHITECTURE.md` — doc sync.

New scripts / fixtures / tests:

- `scripts/capture_realtime_events.py` — one-off capture spike (Task 2).
- `tests/fixtures/realtime/*.json` — captured sample payloads.
- `tests/realtime/` — `__init__.py`, `test_parser.py`, `test_heartbeat.py`, `test_lead_lookup.py`, `test_handler.py`, `test_catchup.py`, `test_listener.py`.
- `tests/test_backfill_messages.py` — coverage for the new `backfill_messages` flags.

---

## Task 1: Config flags

**Files:**
- Modify: `linkedin/conf.py` (append after the `ENABLE_AUTO_DISCOVERY` block, ~line 157)
- Test: `tests/test_conf.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_conf.py`:

```python
class TestRealtimeListenerConfig:
    def test_flag_defaults_off(self, monkeypatch):
        monkeypatch.delenv("ENABLE_REALTIME_LISTENER", raising=False)
        import importlib
        import linkedin.conf as conf
        importlib.reload(conf)
        assert conf.ENABLE_REALTIME_LISTENER is False
        # Restore module state for other tests.
        importlib.reload(conf)

    def test_flag_truthy_strings_enable(self, monkeypatch):
        import importlib
        import linkedin.conf as conf
        for raw in ("1", "true", "YES", "on"):
            monkeypatch.setenv("ENABLE_REALTIME_LISTENER", raw)
            importlib.reload(conf)
            assert conf.ENABLE_REALTIME_LISTENER is True
        importlib.reload(conf)

    def test_tuning_constants_have_defaults(self, monkeypatch):
        import importlib
        import linkedin.conf as conf
        monkeypatch.delenv("LISTENER_CATCHUP_GAP_MINUTES", raising=False)
        monkeypatch.delenv("LISTENER_PUMP_SLICE_SECONDS", raising=False)
        importlib.reload(conf)
        assert conf.LISTENER_CATCHUP_GAP_MINUTES == 30
        assert conf.LISTENER_PUMP_SLICE_SECONDS == 30
        importlib.reload(conf)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_conf.py::TestRealtimeListenerConfig -v`
Expected: FAIL with `AttributeError: module 'linkedin.conf' has no attribute 'ENABLE_REALTIME_LISTENER'`

- [ ] **Step 3: Write minimal implementation**

In `linkedin/conf.py`, after the `ENABLE_AUTO_DISCOVERY` block (the line ending `}` near line 157), add:

```python
# Kill-switch for the realtime inbound-message listener. When true, the
# daemon opens a second browser tab on /messaging/ and observes LinkedIn's
# own realtime SSE stream via CDP, persisting + Slack-notifying inbound
# DMs within seconds. Default OFF — the listener is an enhancement; with
# it disabled the daemon behaves exactly as before (polling only).
# Mirrors the existing ENABLE_* gates.
ENABLE_REALTIME_LISTENER = os.getenv("ENABLE_REALTIME_LISTENER", "false").strip().lower() in {
    "1", "true", "yes", "on",
}

# Startup catch-up threshold. If the listener heartbeat is older than this
# many minutes when the daemon boots, the catch-up surfaces the gap (prompt
# on a TTY, WARNING log when headless). A quick restart stays below it.
LISTENER_CATCHUP_GAP_MINUTES = int(os.getenv("LISTENER_CATCHUP_GAP_MINUTES") or 30)

# Granularity of the daemon's chunked Playwright-pumping idle wait. The wait
# loops in slices of this many seconds so CDP callbacks fire promptly and
# the heartbeat file is refreshed each slice.
LISTENER_PUMP_SLICE_SECONDS = int(os.getenv("LISTENER_PUMP_SLICE_SECONDS") or 30)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_conf.py::TestRealtimeListenerConfig -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add linkedin/conf.py tests/test_conf.py
git commit -m "Add ENABLE_REALTIME_LISTENER config flag and listener tuning constants"
```

---

## Task 2: Capture spike — record real realtime payloads as fixtures

This is a **manual research task**, not TDD. Its deliverable is a set of committed fixture files that ground every later parser test. The capture script is fully specified below; the *running* of it requires a live LinkedIn login and a human sending test messages.

**Files:**
- Create: `scripts/__init__.py` (empty, if `scripts/` does not already exist as a package)
- Create: `scripts/capture_realtime_events.py`
- Create: `tests/fixtures/realtime/` (directory, populated by hand from capture output)

- [ ] **Step 1: Write the capture script**

Create `scripts/capture_realtime_events.py`:

```python
"""One-off capture spike for the realtime message listener.

Opens the daemon's browser, navigates a tab to /messaging/, attaches a CDP
session, and dumps every `Network.eventSourceMessageReceived` payload to
data/realtime-samples/event-NNN.json as it arrives. Not part of the daemon.

Usage:
    .venv/bin/python -m scripts.capture_realtime_events

While it runs: from a SECOND LinkedIn account, (a) send a DM to this
account, (b) start typing without sending, (c) open/read this account's
reply. Also send one DM FROM this account. Ctrl-C to stop. Then copy the
most representative event-NNN.json files into tests/fixtures/realtime/
with descriptive names (see Step 3).
"""
from __future__ import annotations

import json
import logging
import os
import signal

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "linkedin.django_settings")

import django

django.setup()

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger("capture")

from linkedin.conf import ROOT_DIR, get_daemon_handle
from linkedin.browser.registry import get_or_create_session

MESSAGING_URL = "https://www.linkedin.com/messaging/"
OUT_DIR = ROOT_DIR / "data" / "realtime-samples"


def main():
    handle = get_daemon_handle()
    if not handle:
        raise SystemExit("No daemon handle — set LINKEDIN_USERNAME in .env")

    session = get_or_create_session(handle=handle)
    session.campaign = session.campaigns.first()
    session.ensure_browser()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    counter = {"n": 0}

    page = session.context.new_page()
    page.goto(MESSAGING_URL, wait_until="domcontentloaded")
    cdp = session.context.new_cdp_session(page)
    cdp.send("Network.enable")

    def on_event(params):
        counter["n"] += 1
        path = OUT_DIR / f"event-{counter['n']:03d}.json"
        path.write_text(json.dumps(params, indent=2, ensure_ascii=False), encoding="utf-8")
        data_preview = (params.get("data") or "")[:200]
        logger.info("captured %s — data[:200]=%s", path.name, data_preview)

    cdp.on("Network.eventSourceMessageReceived", on_event)
    logger.info("Listening. Send test messages now. Ctrl-C to stop.")

    stop = {"flag": False}
    signal.signal(signal.SIGINT, lambda *_: stop.__setitem__("flag", True))
    while not stop["flag"]:
        page.wait_for_timeout(1000)

    logger.info("Captured %d events into %s", counter["n"], OUT_DIR)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run the capture (manual)**

Run: `.venv/bin/python -m scripts.capture_realtime_events`

From a second LinkedIn account, perform — one at a time, noting which `event-NNN.json` appears for each:
1. Send a DM **to** the daemon account → expect an inbound-message event.
2. Start typing a DM (don't send) → expect a typing-indicator event.
3. Open and read the daemon account's last message → expect a read-receipt event.
4. From the daemon account itself, send a DM → expect an outbound-echo event.
5. Let it idle ~1 min → expect presence/heartbeat events.

Ctrl-C to stop.

- [ ] **Step 3: Curate fixtures**

Create directory `tests/fixtures/realtime/` and copy in **one** representative captured file per category, renamed:
- `inbound_message.json` — the inbound DM event from action 1.
- `outbound_echo.json` — the self-sent DM event from action 4.
- `typing_indicator.json` — from action 2.
- `read_receipt.json` — from action 3.
- `presence.json` — an idle heartbeat event from action 5.

Each file is the full CDP `params` dict (`{requestId, timestamp, eventName, eventId, data}`) exactly as the script wrote it.

- [ ] **Step 4: Document the payload shape**

Open `tests/fixtures/realtime/inbound_message.json`, `json.loads()` its `data` string, and record findings as a comment block at the top of a new file `tests/fixtures/realtime/SHAPE.md`: where in the decoded JSON the message text, message URN, conversation URN, sender display name, sender member/profile URN, and delivered-timestamp live; and which key(s) distinguish a message event from typing/presence/read-receipt. Task 3's parser is verified against exactly these paths.

- [ ] **Step 5: Commit**

```bash
git add scripts/capture_realtime_events.py tests/fixtures/realtime/
git commit -m "Add realtime-event capture spike and captured CDP fixtures"
```

> If `scripts/` did not exist, also `git add scripts/__init__.py` in this commit.
> `data/realtime-samples/` is scratch output — confirm `data/` is gitignored (it holds cookie files); if not, do not commit `data/realtime-samples/`.

---

## Task 3: Realtime SSE framing buffer

**Files:**
- Create: `linkedin/realtime/__init__.py` (empty, only if it does not already exist)
- Create: `linkedin/realtime/sse.py`
- Create: `tests/realtime/__init__.py` (empty, only if it does not already exist)
- Test: `tests/realtime/test_sse.py`

CDP delivers the realtime stream as raw text chunks (`Network.dataReceived`). One chunk may hold several SSE events; one event may straddle two chunks. `RealtimeSSEBuffer` accumulates text and yields complete, JSON-decoded events.

- [ ] **Step 1: Write the failing test**

Create `tests/realtime/test_sse.py`:

```python
"""Tests for the realtime SSE framing buffer."""
from __future__ import annotations

from pathlib import Path

from linkedin.realtime.sse import RealtimeSSEBuffer

FIXTURES = Path(__file__).parent.parent / "fixtures" / "realtime"


def test_single_event_one_feed():
    buf = RealtimeSSEBuffer()
    assert buf.feed('data: {"a": 1}\n\n') == [{"a": 1}]


def test_two_events_one_feed():
    buf = RealtimeSSEBuffer()
    assert buf.feed('data: {"a": 1}\n\ndata: {"b": 2}\n\n') == [{"a": 1}, {"b": 2}]


def test_event_split_across_feeds():
    buf = RealtimeSSEBuffer()
    assert buf.feed('data: {"a":') == []
    assert buf.feed(' 1}\n\n') == [{"a": 1}]


def test_non_data_lines_ignored():
    buf = RealtimeSSEBuffer()
    assert buf.feed(':comment\nevent: ping\ndata: {"ok": true}\n\n') == [{"ok": True}]


def test_malformed_json_skipped_no_raise():
    buf = RealtimeSSEBuffer()
    assert buf.feed('data: {bad\n\ndata: {"good": 1}\n\n') == [{"good": 1}]


def test_crlf_normalized():
    buf = RealtimeSSEBuffer()
    assert buf.feed('data: {"a": 1}\r\n\r\n') == [{"a": 1}]


def test_real_raw_chunk_yields_decorated_event():
    raw = (FIXTURES / "raw_stream_chunk.txt").read_text(encoding="utf-8")
    buf = RealtimeSSEBuffer()
    events = buf.feed(raw + "\n\n")
    assert events
    assert any("com.linkedin.realtimefrontend.DecoratedEvent" in e for e in events)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/realtime/test_sse.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'linkedin.realtime.sse'`

- [ ] **Step 3: Write the implementation**

Create `linkedin/realtime/__init__.py` if missing (empty). Create `linkedin/realtime/sse.py`:

```python
"""Frames LinkedIn's realtime Server-Sent-Events stream.

CDP delivers the /realtime/connect response body as raw text chunks via
Network.dataReceived. One chunk may contain several SSE events, and a
single event may be split across two chunks. RealtimeSSEBuffer accumulates
the text and yields complete, JSON-decoded events.

SSE framing: events are separated by a blank line; payload lines start
with `data:`. LinkedIn sends one compact (single-line) JSON object per
event's `data:` line.
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)


class RealtimeSSEBuffer:
    """Stateful buffer: feed() raw stream text, get back decoded events."""

    def __init__(self):
        self._pending = ""

    def feed(self, text: str) -> list[dict]:
        """Append `text`; return every complete event decoded since last call.

        Incomplete trailing data is held until the next feed().
        """
        self._pending += text.replace("\r\n", "\n")
        events: list[dict] = []
        while "\n\n" in self._pending:
            block, self._pending = self._pending.split("\n\n", 1)
            payload = self._data_payload(block)
            if not payload:
                continue
            try:
                events.append(json.loads(payload))
            except json.JSONDecodeError as e:
                logger.debug("realtime SSE: undecodable data line skipped: %s", e)
        return events

    @staticmethod
    def _data_payload(block: str) -> str:
        """Join the `data:` lines of one SSE event block into one string."""
        parts = [
            line[len("data:"):].lstrip(" ")
            for line in block.split("\n")
            if line.startswith("data:")
        ]
        return "\n".join(parts)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/realtime/test_sse.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add linkedin/realtime/sse.py tests/realtime/test_sse.py
git commit -m "Add realtime SSE framing buffer"
```
(Include `linkedin/realtime/__init__.py` / `tests/realtime/__init__.py` in `git add` only if you created them.)

---

## Task 3B: Event parser

**Files:**
- Create: `linkedin/realtime/parser.py`
- Test: `tests/realtime/test_parser.py`

Parses one decoded SSE event (output of Task 3's buffer) into a structured message, or `None` for non-message events. Field paths are confirmed against `tests/fixtures/realtime/SHAPE.md` — read it before starting.

- [ ] **Step 1: Write the failing test**

Create `tests/realtime/test_parser.py`:

```python
"""Parser tests, driven by the fixtures captured in Task 2.

The fixture files ARE the contract. If an assertion fails, the captured
payload at tests/fixtures/realtime/<name>.json is authoritative.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from linkedin.realtime.parser import ParsedRealtimeMessage, parse_realtime_event

FIXTURES = Path(__file__).parent.parent / "fixtures" / "realtime"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_inbound_message_parses():
    r = parse_realtime_event(_load("inbound_message.json"))
    assert isinstance(r, ParsedRealtimeMessage)
    assert r.entity_urn.startswith("urn:li:messagingMessage:")
    assert r.conversation_urn.startswith("urn:li:msg_conversation:")
    assert r.text == "hey im interested"
    assert r.sender_name == "Arian Taj"
    assert r.sender_member_urn.startswith("urn:li:fsd_profile:")
    # timestamp must be persist_thread-compatible: "YYYY-MM-DD HH:MM"
    assert len(r.timestamp) == 16 and r.timestamp[4] == "-"


def test_outbound_echo_parses():
    """Outbound echoes still parse — the handler decides direction from the
    persisted row. The parser returns None only for NON-message events."""
    r = parse_realtime_event(_load("outbound_echo.json"))
    assert isinstance(r, ParsedRealtimeMessage)
    assert r.sender_name == "Chuka Agu"
    assert r.text == "okay thanks"


@pytest.mark.parametrize("name", ["typing_indicator.json", "read_receipt.json", "presence.json"])
def test_non_message_events_return_none(name):
    assert parse_realtime_event(_load(name)) is None


def test_garbage_returns_none():
    assert parse_realtime_event(None) is None
    assert parse_realtime_event({}) is None
    assert parse_realtime_event({"com.linkedin.realtimefrontend.DecoratedEvent": {}}) is None
    assert parse_realtime_event(
        {"com.linkedin.realtimefrontend.DecoratedEvent":
         {"topic": "urn:li-realtime:messagesTopic:x", "payload": {}}}
    ) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/realtime/test_parser.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'linkedin.realtime.parser'`

- [ ] **Step 3: Write the implementation**

Create `linkedin/realtime/parser.py`:

```python
"""Pure parser for one decoded LinkedIn realtime event.

Input is a single SSE event already JSON-decoded (see linkedin/realtime/sse.py).
Output is a ParsedRealtimeMessage for `messagesTopic` events, or None for
everything else (heartbeats, typing indicators, read receipts, badge
updates, conversation updates, unrecognised shapes).

Field paths confirmed against tests/fixtures/realtime/SHAPE.md.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone as dt_timezone

logger = logging.getLogger(__name__)

_DECORATED_EVENT_KEY = "com.linkedin.realtimefrontend.DecoratedEvent"
_MESSAGES_TOPIC = "messagesTopic"


@dataclass(frozen=True)
class ParsedRealtimeMessage:
    """A message extracted from one realtime event.

    `timestamp` is formatted "YYYY-MM-DD HH:MM" so it feeds straight into
    `linkedin.db.messages.persist_thread` (which expects that format).
    """

    entity_urn: str          # message URN (backendUrn) — idempotency key
    conversation_urn: str    # conversation.entityUrn — matches Message.thread_external_id
    sender_name: str
    sender_member_urn: str
    text: str
    timestamp: str


def _epoch_ms_to_str(value) -> str:
    """Format an epoch-millisecond timestamp as 'YYYY-MM-DD HH:MM'."""
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return ""
    return datetime.fromtimestamp(ms / 1000, tz=dt_timezone.utc).strftime("%Y-%m-%d %H:%M")


def _attributed_text(value) -> str:
    """Extract `.text` from a LinkedIn AttributedText dict, else ''."""
    if isinstance(value, dict):
        text = value.get("text")
        if isinstance(text, str):
            return text
    return ""


def parse_realtime_event(event) -> ParsedRealtimeMessage | None:
    """Parse one decoded realtime event.

    Returns a ParsedRealtimeMessage for `messagesTopic` events, None for
    everything else. Never raises — malformed input yields None.
    """
    if not isinstance(event, dict):
        return None
    decorated = event.get(_DECORATED_EVENT_KEY)
    if not isinstance(decorated, dict):
        return None  # Heartbeat / ClientConnection / other envelope

    topic = decorated.get("topic")
    if not isinstance(topic, str) or _MESSAGES_TOPIC not in topic:
        return None  # typing / seen-receipt / badge / conversation / etc.

    decoration = (
        (decorated.get("payload") or {}).get("data") or {}
    ).get("doDecorateMessageMessengerRealtimeDecoration") or {}
    result = decoration.get("result")
    if not isinstance(result, dict):
        return None

    text = _attributed_text(result.get("body"))
    entity_urn = result.get("backendUrn")
    if not isinstance(entity_urn, str) or not entity_urn or not text:
        return None

    conversation = result.get("conversation")
    conversation_urn = ""
    if isinstance(conversation, dict):
        conversation_urn = conversation.get("entityUrn") or ""
    if not conversation_urn:
        conversation_urn = result.get("backendConversationUrn") or ""

    actor = result.get("actor") or {}
    sender_member_urn = actor.get("hostIdentityUrn") or ""
    member = (actor.get("participantType") or {}).get("member") or {}
    sender_name = (
        f"{_attributed_text(member.get('firstName'))} "
        f"{_attributed_text(member.get('lastName'))}"
    ).strip()

    return ParsedRealtimeMessage(
        entity_urn=entity_urn,
        conversation_urn=conversation_urn,
        sender_name=sender_name,
        sender_member_urn=sender_member_urn,
        text=text,
        timestamp=_epoch_ms_to_str(result.get("deliveredAt")),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/realtime/test_parser.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add linkedin/realtime/parser.py tests/realtime/test_parser.py
git commit -m "Add realtime event parser"
```

---

## Task 4: Heartbeat module

**Files:**
- Create: `linkedin/realtime/heartbeat.py`
- Test: `tests/realtime/test_heartbeat.py`

- [ ] **Step 1: Write the failing test**

Create `tests/realtime/test_heartbeat.py`:

```python
"""Tests for the listener heartbeat file (pure file-I/O)."""
from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from linkedin.realtime import heartbeat


def test_path_is_per_username(tmp_path, monkeypatch):
    monkeypatch.setattr(heartbeat, "ROOT_DIR", tmp_path)
    p1 = heartbeat.heartbeat_path_for("arian@tryfedrampgpt.com")
    p2 = heartbeat.heartbeat_path_for("chukyjack@gmail.com")
    assert p1 != p2
    assert p1.name == "listener-heartbeat-arian-tryfedrampgpt-com.json"


def test_empty_username_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(heartbeat, "ROOT_DIR", tmp_path)
    import pytest
    with pytest.raises(ValueError):
        heartbeat.heartbeat_path_for("")


def test_write_then_read_roundtrips(tmp_path, monkeypatch):
    monkeypatch.setattr(heartbeat, "ROOT_DIR", tmp_path)
    heartbeat.write_heartbeat("arian@x.com")
    last = heartbeat.read_heartbeat("arian@x.com")
    assert last is not None
    assert abs((timezone.now() - last).total_seconds()) < 5


def test_read_missing_file_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(heartbeat, "ROOT_DIR", tmp_path)
    assert heartbeat.read_heartbeat("nobody@x.com") is None


def test_read_corrupt_file_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(heartbeat, "ROOT_DIR", tmp_path)
    path = heartbeat.heartbeat_path_for("arian@x.com")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")
    assert heartbeat.read_heartbeat("arian@x.com") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/realtime/test_heartbeat.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'linkedin.realtime.heartbeat'`

- [ ] **Step 3: Write the implementation**

Create `linkedin/realtime/heartbeat.py`:

```python
"""Per-account 'listener was last alive at' timestamp file.

One JSON file per LinkedIn username at
data/listener-heartbeat-<safe_username>.json — same data/ + safe-name
convention as the cookie store. Refreshed every pump slice while the
listener runs; read once at daemon startup by the catch-up.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path

from django.utils import timezone

from linkedin.conf import ROOT_DIR

logger = logging.getLogger(__name__)

_SAFE_NAME_RE = re.compile(r"[^a-z0-9]+")


def heartbeat_path_for(username: str) -> Path:
    """data/listener-heartbeat-<safe>.json for a LinkedIn username.

    Safe-name rule matches linkedin.browser.cookie_store.cookie_path_for.
    """
    safe = _SAFE_NAME_RE.sub("-", (username or "").lower()).strip("-")
    if not safe:
        raise ValueError("cannot derive heartbeat path from empty username")
    return ROOT_DIR / "data" / f"listener-heartbeat-{safe}.json"


def write_heartbeat(username: str) -> None:
    """Stamp the heartbeat file with the current time. Best-effort."""
    path = heartbeat_path_for(username)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"last_alive": timezone.now().isoformat()}),
        encoding="utf-8",
    )


def read_heartbeat(username: str) -> datetime | None:
    """Return the last-alive datetime, or None if missing / unreadable."""
    path = heartbeat_path_for(username)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return datetime.fromisoformat(data["last_alive"])
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        logger.warning("Unreadable heartbeat file %s: %s", path, e)
        return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/realtime/test_heartbeat.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add linkedin/realtime/heartbeat.py tests/realtime/test_heartbeat.py
git commit -m "Add per-account listener heartbeat file module"
```

---

## Task 5: Lead-resolution helper

**Files:**
- Create: `linkedin/realtime/lead_lookup.py`
- Test: `tests/realtime/test_lead_lookup.py`

- [ ] **Step 1: Write the failing test**

Create `tests/realtime/test_lead_lookup.py`:

```python
"""Tests for resolving a Lead from a realtime event's URNs."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from crm.models import Lead, Message

from linkedin.realtime.lead_lookup import resolve_lead_for_realtime


def test_resolves_by_conversation_urn(db):
    lead = Lead.objects.create(
        first_name="Waylon", last_name="Krush",
        linkedin_url="https://www.linkedin.com/in/waylonkrush/",
    )
    Message.objects.create(
        lead=lead, source=Message.Source.LINKEDIN, external_id="urn:li:msg:1",
        direction=Message.Direction.OUTBOUND, sender="Arian", body="hi",
        sent_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        thread_external_id="urn:li:msg_conversation:(x,2-abc)",
    )
    got = resolve_lead_for_realtime(
        conversation_urn="urn:li:msg_conversation:(x,2-abc)",
        sender_member_urn="",
    )
    assert got == lead


def test_resolves_by_sender_member_urn_from_profile_json(db):
    lead = Lead.objects.create(
        first_name="Dana", linkedin_url="https://www.linkedin.com/in/dana/",
        description=json.dumps({"urn": "urn:li:fsd_profile:DANA123"}),
    )
    got = resolve_lead_for_realtime(
        conversation_urn="urn:li:msg_conversation:(x,2-unknown)",
        sender_member_urn="urn:li:fsd_profile:DANA123",
    )
    assert got == lead


def test_returns_none_when_no_match(db):
    assert resolve_lead_for_realtime(
        conversation_urn="urn:li:msg_conversation:(x,2-nope)",
        sender_member_urn="urn:li:fsd_profile:NOBODY",
    ) is None


def test_conversation_urn_match_wins_over_sender(db):
    by_conv = Lead.objects.create(
        first_name="ByConv", linkedin_url="https://www.linkedin.com/in/byconv/",
    )
    Message.objects.create(
        lead=by_conv, source=Message.Source.LINKEDIN, external_id="m1",
        direction=Message.Direction.OUTBOUND, sender="us", body="hi",
        sent_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        thread_external_id="urn:li:msg_conversation:(x,2-shared)",
    )
    Lead.objects.create(
        first_name="BySender", linkedin_url="https://www.linkedin.com/in/bysender/",
        description=json.dumps({"urn": "urn:li:fsd_profile:S1"}),
    )
    got = resolve_lead_for_realtime(
        conversation_urn="urn:li:msg_conversation:(x,2-shared)",
        sender_member_urn="urn:li:fsd_profile:S1",
    )
    assert got == by_conv
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/realtime/test_lead_lookup.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'linkedin.realtime.lead_lookup'`

- [ ] **Step 3: Write the implementation**

Create `linkedin/realtime/lead_lookup.py`:

```python
"""Resolve a crm.Lead from a realtime event's conversation / sender URNs.

Strategy, in priority order:

1. Conversation URN → the most reliable match. Every thread persisted by
   `linkedin.db.messages.persist_thread` stamps `Message.thread_external_id`
   with the conversation URN. If we have ever seen this thread (via the
   sweep, follow-up, backfill, or an earlier realtime event), one query
   finds the Lead.
2. Sender member/profile URN → fallback for a thread we have never
   persisted. The enriched profile JSON on `Lead.description` carries the
   member URN; a substring match on that text column finds it (works on
   both SQLite dev and Postgres prod).

No match → None; the handler logs + skips.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def resolve_lead_for_realtime(*, conversation_urn: str, sender_member_urn: str):
    """Return the matching Lead, or None."""
    from crm.models import Lead, Message

    if conversation_urn:
        msg = (
            Message.objects.filter(
                source=Message.Source.LINKEDIN,
                thread_external_id=conversation_urn,
            )
            .select_related("lead")
            .first()
        )
        if msg is not None:
            return msg.lead

    if sender_member_urn:
        lead = Lead.objects.filter(description__contains=sender_member_urn).first()
        if lead is not None:
            return lead

    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/realtime/test_lead_lookup.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add linkedin/realtime/lead_lookup.py tests/realtime/test_lead_lookup.py
git commit -m "Add realtime lead-resolution helper"
```

---

## Task 6: Slack `notify_message_received`

**Files:**
- Modify: `linkedin/notifications/slack.py` (add a function after `notify_connection_accepted`, ~line 116)
- Test: `tests/test_slack_notify.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_slack_notify.py`:

```python
class TestNotifyMessageReceived:
    def test_noop_when_webhook_unset(self, db):
        """conftest._silence_slack clears the webhook — must not POST."""
        from crm.models import Lead
        lead = Lead.objects.create(
            first_name="Waylon", last_name="Krush",
            linkedin_url="https://www.linkedin.com/in/waylonkrush/",
        )
        with patch("linkedin.notifications.slack.request.urlopen") as mock_open:
            slack_mod.notify_message_received(
                lead=lead, text="hello there", operator="Arian",
            )
        mock_open.assert_not_called()

    def test_posts_block_kit_when_webhook_set(self, db, slack_url):
        from crm.models import Lead
        lead = Lead.objects.create(
            first_name="Waylon", last_name="Krush",
            linkedin_url="https://www.linkedin.com/in/waylonkrush/",
        )
        with patch("linkedin.notifications.slack.request.urlopen") as mock_open:
            mock_open.return_value.__enter__.return_value.status = 200
            slack_mod.notify_message_received(
                lead=lead, text="hello there", operator="Arian",
            )
        mock_open.assert_called_once()
        sent = json.loads(mock_open.call_args[0][0].data.decode("utf-8"))
        assert "blocks" in sent
        assert "Waylon Krush" in sent["text"]
        body = json.dumps(sent)
        assert "hello there" in body
        assert "waylonkrush" in body  # profile link

    def test_long_text_is_truncated(self, db, slack_url):
        from crm.models import Lead
        lead = Lead.objects.create(
            first_name="A", linkedin_url="https://www.linkedin.com/in/a-long/",
        )
        with patch("linkedin.notifications.slack.request.urlopen") as mock_open:
            mock_open.return_value.__enter__.return_value.status = 200
            slack_mod.notify_message_received(
                lead=lead, text="x" * 600, operator="",
            )
        sent = json.loads(mock_open.call_args[0][0].data.decode("utf-8"))
        body = json.dumps(sent)
        assert "..." in body
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_slack_notify.py::TestNotifyMessageReceived -v`
Expected: FAIL with `AttributeError: module 'linkedin.notifications.slack' has no attribute 'notify_message_received'`

- [ ] **Step 3: Write the implementation**

In `linkedin/notifications/slack.py`, after `notify_connection_accepted` ends (just before `def notify_error`, ~line 117), add:

```python
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

    full_name = lead.full_name or lead.public_identifier or "Unknown lead"
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_slack_notify.py::TestNotifyMessageReceived -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add linkedin/notifications/slack.py tests/test_slack_notify.py
git commit -m "Add notify_message_received Slack notification"
```

---

## Task 7: Event handler

**Files:**
- Create: `linkedin/realtime/handler.py`
- Test: `tests/realtime/test_handler.py`

- [ ] **Step 1: Write the failing test**

Create `tests/realtime/test_handler.py`:

```python
"""Tests for the realtime event handler (parse → resolve → persist → Slack)."""
from __future__ import annotations

from unittest.mock import patch

from crm.models import Lead, Message

from linkedin.realtime.handler import handle_realtime_event
from linkedin.realtime.parser import ParsedRealtimeMessage

CONV = "urn:li:msg_conversation:(x,2-handler)"


def _inbound(lead):
    """A ParsedRealtimeMessage whose sender matches `lead` (→ INBOUND)."""
    return ParsedRealtimeMessage(
        entity_urn="urn:li:msg:rt1",
        conversation_urn=CONV,
        sender_name=f"{lead.first_name} {lead.last_name}".strip(),
        sender_member_urn="urn:li:fsd_profile:LEAD1",
        text="Hey, interested — let's talk",
        timestamp="2026-05-16 14:30",
    )


def _seed_lead(db):
    lead = Lead.objects.create(
        first_name="Waylon", last_name="Krush",
        linkedin_url="https://www.linkedin.com/in/waylonkrush/",
    )
    # An existing outbound message so conversation-URN resolution works.
    Message.objects.create(
        lead=lead, source=Message.Source.LINKEDIN, external_id="seed",
        direction=Message.Direction.OUTBOUND, sender="Arian", body="hi",
        sent_at=__import__("django.utils.timezone", fromlist=["now"]).now(),
        thread_external_id=CONV,
    )
    return lead


def test_inbound_message_persists_and_notifies(db):
    lead = _seed_lead(db)
    with patch("linkedin.realtime.handler.parse_realtime_event", return_value=_inbound(lead)), \
         patch("linkedin.realtime.handler.notify_message_received") as mock_notify:
        handle_realtime_event({"data": "ignored"}, operator="Arian")

    msg = Message.objects.get(external_id="urn:li:msg:rt1")
    assert msg.direction == Message.Direction.INBOUND
    assert msg.lead == lead
    mock_notify.assert_called_once()


def test_duplicate_event_is_idempotent(db):
    lead = _seed_lead(db)
    with patch("linkedin.realtime.handler.parse_realtime_event", return_value=_inbound(lead)), \
         patch("linkedin.realtime.handler.notify_message_received") as mock_notify:
        handle_realtime_event({"data": "x"}, operator="Arian")
        handle_realtime_event({"data": "x"}, operator="Arian")

    assert Message.objects.filter(external_id="urn:li:msg:rt1").count() == 1
    assert mock_notify.call_count == 1  # second event already persisted → no re-notify


def test_non_message_event_is_skipped(db):
    with patch("linkedin.realtime.handler.parse_realtime_event", return_value=None), \
         patch("linkedin.realtime.handler.notify_message_received") as mock_notify:
        handle_realtime_event({"data": "presence"}, operator="Arian")
    assert Message.objects.count() == 0
    mock_notify.assert_not_called()


def test_unresolved_sender_is_skipped_no_crash(db):
    unmatched = ParsedRealtimeMessage(
        entity_urn="urn:li:msg:rt9", conversation_urn="urn:li:msg_conversation:(x,2-none)",
        sender_name="Ghost", sender_member_urn="urn:li:fsd_profile:GHOST",
        text="hi", timestamp="2026-05-16 14:30",
    )
    with patch("linkedin.realtime.handler.parse_realtime_event", return_value=unmatched), \
         patch("linkedin.realtime.handler.notify_message_received") as mock_notify:
        handle_realtime_event({"data": "x"}, operator="Arian")  # must not raise
    assert Message.objects.count() == 0
    mock_notify.assert_not_called()


def test_outbound_echo_persisted_but_not_notified(db):
    """An echo of our own send: sender != lead → persist_thread marks it
    OUTBOUND → no Slack notification."""
    lead = _seed_lead(db)
    echo = ParsedRealtimeMessage(
        entity_urn="urn:li:msg:rtecho", conversation_urn=CONV,
        sender_name="Arian Tajbakhsh", sender_member_urn="urn:li:fsd_profile:US",
        text="our own message", timestamp="2026-05-16 14:31",
    )
    with patch("linkedin.realtime.handler.parse_realtime_event", return_value=echo), \
         patch("linkedin.realtime.handler.notify_message_received") as mock_notify:
        handle_realtime_event({"data": "x"}, operator="Arian")
    msg = Message.objects.get(external_id="urn:li:msg:rtecho")
    assert msg.direction == Message.Direction.OUTBOUND
    mock_notify.assert_not_called()


def test_handler_swallows_exceptions_and_notifies_error(db):
    with patch("linkedin.realtime.handler.parse_realtime_event", side_effect=RuntimeError("boom")), \
         patch("linkedin.realtime.handler.notify_error") as mock_err:
        handle_realtime_event({"data": "x"}, operator="Arian")  # must not raise
    mock_err.assert_called_once()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/realtime/test_handler.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'linkedin.realtime.handler'`

- [ ] **Step 3: Write the implementation**

Create `linkedin/realtime/handler.py`:

```python
"""Realtime event handler: parse → resolve Lead → persist → Slack.

Called by the listener once per decoded SSE event. Every call is wrapped
in try/except — a bad event logs, Slack-error-notifies (deduped), and is
dropped; it never unwinds the daemon loop. Realtime is an enhancement,
never a hard dependency.

Outbound echoes (our own sends mirrored back on the stream) are still
persisted — that is harmless and idempotent with backfill_messages — but
direction is derived by `persist_thread` from the sender, and only INBOUND
messages trigger a Slack notification.
"""
from __future__ import annotations

import logging

from linkedin.notifications.slack import notify_error, notify_message_received
from linkedin.realtime.lead_lookup import resolve_lead_for_realtime
from linkedin.realtime.parser import parse_realtime_event

logger = logging.getLogger(__name__)


def handle_realtime_event(event: dict, *, operator: str = "") -> None:
    """Process one decoded realtime SSE event. Never raises."""
    try:
        _handle(event, operator=operator)
    except Exception as exc:
        logger.exception("handle_realtime_event failed — event dropped")
        notify_error("daemon:realtime_listener", exc, context={"operator": operator})


def _handle(event: dict, *, operator: str) -> None:
    from crm.models import Message
    from linkedin.db.messages import persist_thread

    parsed = parse_realtime_event(event)
    if parsed is None:
        return  # presence / typing / read-receipt / unparseable

    if not parsed.entity_urn:
        logger.debug("realtime: message event with no URN — skipped")
        return

    lead = resolve_lead_for_realtime(
        conversation_urn=parsed.conversation_urn,
        sender_member_urn=parsed.sender_member_urn,
    )
    if lead is None:
        logger.warning(
            "Realtime message from %r (conv=%s) — no matching Lead, skipped",
            parsed.sender_name, parsed.conversation_urn,
        )
        return

    created = persist_thread(
        lead=lead,
        parsed=[{
            "entity_urn": parsed.entity_urn,
            "sender": parsed.sender_name,
            "text": parsed.text,
            "timestamp": parsed.timestamp,
        }],
        thread_external_id=parsed.conversation_urn,
    )
    if not created:
        logger.debug("Realtime event already persisted (%s)", parsed.entity_urn)
        return

    msg = Message.objects.get(
        source=Message.Source.LINKEDIN, external_id=parsed.entity_urn,
    )
    if msg.direction == Message.Direction.INBOUND:
        logger.info("Realtime inbound message persisted for %s", lead)
        notify_message_received(lead=lead, text=parsed.text, operator=operator)
    else:
        logger.debug("Realtime outbound echo persisted for %s — no notify", lead)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/realtime/test_handler.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add linkedin/realtime/handler.py tests/realtime/test_handler.py
git commit -m "Add realtime event handler"
```

---

## Task 8: Realtime listener + lifecycle helpers

The `RealtimeListener` class wires the listener tab and CDP `streamResourceContent` capture; the lifecycle helpers let the daemon (re)create or tear it down idempotently. The browser/CDP tab wiring (`start`/`stop`) is integration-tested manually (Task 12); `pump()`, `is_alive`, and the `_dispatch` SSE path are unit-tested here.

**Files:**
- Create: `linkedin/realtime/listener.py`
- Test: `tests/realtime/test_listener.py`

- [ ] **Step 1: Write the failing test**

Create `tests/realtime/test_listener.py`:

```python
"""Unit coverage for RealtimeListener — pump, is_alive, and the SSE
dispatch path. The browser/CDP tab wiring (start/stop) is integration-
tested by hand (see the plan's Task 12).
"""
from __future__ import annotations

import base64
from unittest.mock import MagicMock, patch

from linkedin.realtime.listener import RealtimeListener


class _FakePage:
    def __init__(self):
        self.total_waited_ms = 0
        self._closed = False

    def wait_for_timeout(self, ms):
        self.total_waited_ms += ms

    def is_closed(self):
        return self._closed


def _listener_with_fake_page():
    session = MagicMock()
    session.linkedin_profile.linkedin_username = "arian@x.com"
    listener = RealtimeListener(session, operator="Arian")
    listener.page = _FakePage()
    return listener


def test_pump_waits_the_full_duration(monkeypatch):
    monkeypatch.setattr("linkedin.realtime.listener.LISTENER_PUMP_SLICE_SECONDS", 30)
    listener = _listener_with_fake_page()
    with patch("linkedin.realtime.heartbeat.write_heartbeat") as mock_hb:
        listener.pump(70)
    assert listener.page.total_waited_ms == 70_000
    # 30 + 30 + 10 → three slices → three heartbeat writes
    assert mock_hb.call_count == 3


def test_pump_zero_or_negative_is_noop(monkeypatch):
    monkeypatch.setattr("linkedin.realtime.listener.LISTENER_PUMP_SLICE_SECONDS", 30)
    listener = _listener_with_fake_page()
    with patch("linkedin.realtime.heartbeat.write_heartbeat") as mock_hb:
        listener.pump(0)
        listener.pump(-5)
    assert listener.page.total_waited_ms == 0
    mock_hb.assert_not_called()


def test_is_alive_false_when_no_page():
    listener = RealtimeListener(MagicMock())
    assert listener.is_alive is False


def test_dispatch_decodes_frames_and_calls_handler():
    """_dispatch: base64 → SSE framing → one handle_realtime_event per event."""
    listener = RealtimeListener(MagicMock(), operator="Arian")
    sse_text = 'data: {"a": 1}\n\ndata: {"b": 2}\n\n'
    chunk_b64 = base64.b64encode(sse_text.encode("utf-8")).decode("ascii")
    with patch("linkedin.realtime.listener.handle_realtime_event") as mock_handle:
        listener._dispatch(chunk_b64)
    assert mock_handle.call_count == 2
    assert mock_handle.call_args_list[0].args[0] == {"a": 1}
    assert mock_handle.call_args_list[1].kwargs == {"operator": "Arian"}


def test_dispatch_buffers_event_split_across_chunks():
    """An event split across two stream chunks is handled once, on completion."""
    listener = RealtimeListener(MagicMock(), operator="Arian")
    part1 = base64.b64encode(b'data: {"a":').decode("ascii")
    part2 = base64.b64encode(b' 1}\n\n').decode("ascii")
    with patch("linkedin.realtime.listener.handle_realtime_event") as mock_handle:
        listener._dispatch(part1)
        assert mock_handle.call_count == 0
        listener._dispatch(part2)
        assert mock_handle.call_count == 1
        assert mock_handle.call_args.args[0] == {"a": 1}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/realtime/test_listener.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'linkedin.realtime.listener'`

- [ ] **Step 3: Write the implementation**

Create `linkedin/realtime/listener.py`:

```python
"""Realtime listener tab: a second page in the daemon's existing browser
context, observing LinkedIn's own realtime stream via CDP.

LinkedIn's web client opens a long-lived streaming fetch to
/realtime/connect (text/event-stream) when /messaging/ is loaded. It is
NOT a native EventSource, so CDP's Network.eventSourceMessageReceived
never fires. The listener instead:
  - watches Network.requestWillBeSent for the /realtime/connect URL,
  - on Network.responseReceived calls Network.streamResourceContent for
    that request — after which Network.dataReceived events carry the body
    bytes as base64 (and the call's result holds whatever was buffered),
  - decodes those bytes, frames them with RealtimeSSEBuffer, and feeds
    each decoded event to handle_realtime_event.

Same browser context = same cookies, fingerprint, TLS, and IP as the
daemon. We open nothing LinkedIn's own client wouldn't.

The listener enqueues no tasks. Its callbacks fire whenever Python is
parked inside any Playwright call — including the daemon's chunked idle
pump() and (for free) during connect/follow-up task execution.
"""
from __future__ import annotations

import base64
import logging

from linkedin.conf import ENABLE_REALTIME_LISTENER, LISTENER_PUMP_SLICE_SECONDS
from linkedin.realtime.handler import handle_realtime_event
from linkedin.realtime.sse import RealtimeSSEBuffer

logger = logging.getLogger(__name__)

MESSAGING_URL = "https://www.linkedin.com/messaging/"
_REALTIME_CONNECT_PATH = "/realtime/connect"


class RealtimeListener:
    """Owns the listener tab + CDP session for one AccountSession."""

    def __init__(self, session, *, operator: str = ""):
        self.session = session
        self.operator = operator
        self.page = None
        self.cdp = None
        self._buffer = RealtimeSSEBuffer()
        self._stream_request_ids: set = set()

    @property
    def is_alive(self) -> bool:
        return self.page is not None and not self.page.is_closed()

    def start(self) -> None:
        """Open the listener tab, attach CDP, subscribe. Raises on failure —
        the caller (ensure_realtime_listener) degrades gracefully."""
        context = self.session.context
        self.page = context.new_page()
        self.cdp = context.new_cdp_session(self.page)
        self.cdp.send("Network.enable")
        self.cdp.on("Network.requestWillBeSent", self._on_request)
        self.cdp.on("Network.responseReceived", self._on_response)
        self.cdp.on("Network.dataReceived", self._on_data)
        self.page.goto(MESSAGING_URL, wait_until="domcontentloaded")
        logger.info("Realtime listener tab opened — observing %s", _REALTIME_CONNECT_PATH)

    def _on_request(self, params: dict) -> None:
        url = (params.get("request") or {}).get("url", "")
        if _REALTIME_CONNECT_PATH in url:
            self._stream_request_ids.add(params.get("requestId"))

    def _on_response(self, params: dict) -> None:
        request_id = params.get("requestId")
        if request_id not in self._stream_request_ids:
            return
        # Enable body streaming for the realtime connection. The result
        # carries whatever was already buffered; subsequent dataReceived
        # events then carry the live chunks.
        try:
            result = self.cdp.send(
                "Network.streamResourceContent", {"requestId": request_id},
            )
        except Exception as e:
            logger.warning("streamResourceContent failed for %s: %s", request_id, e)
            return
        buffered = result.get("bufferedData")
        if buffered:
            self._dispatch(buffered)

    def _on_data(self, params: dict) -> None:
        if params.get("requestId") not in self._stream_request_ids:
            return
        data_b64 = params.get("data")
        if data_b64:
            self._dispatch(data_b64)

    def _dispatch(self, data_b64: str) -> None:
        """Decode a base64 stream chunk, frame it, handle each event.

        Never raises — handle_realtime_event is itself try/except wrapped,
        and a bad chunk here is logged and dropped.
        """
        try:
            text = base64.b64decode(data_b64).decode("utf-8", errors="replace")
        except Exception as e:
            logger.warning("realtime: undecodable stream chunk dropped: %s", e)
            return
        for event in self._buffer.feed(text):
            handle_realtime_event(event, operator=self.operator)

    def pump(self, seconds: float) -> None:
        """Sleep `seconds` in slices via the listener page, refreshing the
        heartbeat each slice. Keeps the Playwright event loop pumped so CDP
        callbacks fire promptly. Used in place of the daemon's idle sleep."""
        from linkedin.realtime.heartbeat import write_heartbeat

        username = self.session.linkedin_profile.linkedin_username
        remaining = seconds
        while remaining > 0:
            chunk = min(LISTENER_PUMP_SLICE_SECONDS, remaining)
            self.page.wait_for_timeout(int(chunk * 1000))
            write_heartbeat(username)
            remaining -= chunk

    def stop(self) -> None:
        """Detach CDP and close the listener tab. Idempotent, never raises."""
        try:
            if self.cdp is not None:
                self.cdp.detach()
        except Exception as e:
            logger.debug("Realtime listener CDP detach failed: %s", e)
        try:
            if self.page is not None and not self.page.is_closed():
                self.page.close()
        except Exception as e:
            logger.debug("Realtime listener tab close failed: %s", e)
        self.page = self.cdp = None
        logger.info("Realtime listener tab closed")


def ensure_realtime_listener(session, *, operator: str = ""):
    """Return a live RealtimeListener for `session`, creating/recovering it.

    No-op (returns None) when ENABLE_REALTIME_LISTENER is false. A failure
    to open the tab is logged and swallowed — the daemon continues without
    realtime (degrades to polling). The listener is cached on
    `session.realtime_listener`.
    """
    if not ENABLE_REALTIME_LISTENER:
        return None

    listener = getattr(session, "realtime_listener", None)
    if listener is not None and listener.is_alive:
        return listener

    if listener is not None:
        listener.stop()  # dead tab — clean up before recreating

    listener = RealtimeListener(session, operator=operator)
    try:
        listener.start()
    except Exception as e:
        logger.warning(
            "Realtime listener failed to start — continuing without realtime: %s", e,
        )
        session.realtime_listener = None
        return None

    session.realtime_listener = listener
    return listener


def stop_realtime_listener(session) -> None:
    """Tear down the session's listener if one exists. Idempotent."""
    listener = getattr(session, "realtime_listener", None)
    if listener is not None:
        listener.stop()
    session.realtime_listener = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/realtime/test_listener.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add linkedin/realtime/listener.py tests/realtime/test_listener.py
git commit -m "Add RealtimeListener with streamResourceContent capture"
```

---

## Task 9: `backfill_messages` — `--account` and `--skip-prereq-gate`

The startup catch-up (Task 10) needs to run `backfill_messages` for the daemon's account only, without the interactive prereq gate. Add both options first.

**Files:**
- Modify: `linkedin/management/commands/backfill_messages.py`
- Test: `tests/test_backfill_messages.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_backfill_messages.py`:

```python
"""Tests for the backfill_messages --account / --skip-prereq-gate options."""
from __future__ import annotations

from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError


@pytest.fixture
def both_accounts(monkeypatch):
    monkeypatch.setenv("LINKEDIN_USERNAME", "primary@x.com")
    monkeypatch.setenv("LINKEDIN_PASSWORD", "p")
    monkeypatch.setenv("BACKFILL_LINKEDIN_USERNAME", "backfill@x.com")
    monkeypatch.setenv("BACKFILL_LINKEDIN_PASSWORD", "p")


def test_account_flag_restricts_to_one_slot(db, both_accounts):
    """--account primary must iterate only the primary slot."""
    seen = []

    def fake_make_session(label, env_user, env_pass):
        seen.append(label)
        raise RuntimeError("stop before login")  # we only assert the slot set

    with patch("linkedin.management.commands.backfill_messages._make_session",
               side_effect=fake_make_session), \
         patch("linkedin.management.commands.backfill_messages._run_prereq_gate_for_accounts",
               return_value=True):
        call_command("backfill_messages", account="primary")

    assert seen == ["primary"]


def test_unknown_account_raises(db, both_accounts):
    with pytest.raises(CommandError):
        call_command("backfill_messages", account="nonsense")


def test_skip_prereq_gate_bypasses_the_gate(db, both_accounts):
    with patch("linkedin.management.commands.backfill_messages._run_prereq_gate_for_accounts") as gate, \
         patch("linkedin.management.commands.backfill_messages._make_session",
               side_effect=RuntimeError("stop")):
        call_command("backfill_messages", account="primary", skip_prereq_gate=True)
    gate.assert_not_called()


def test_default_run_still_calls_the_gate(db, both_accounts):
    with patch("linkedin.management.commands.backfill_messages._run_prereq_gate_for_accounts",
               return_value=True) as gate, \
         patch("linkedin.management.commands.backfill_messages._make_session",
               side_effect=RuntimeError("stop")):
        call_command("backfill_messages")
    gate.assert_called_once()
```

> `--account nonsense` is rejected by argparse `choices`; Django surfaces that as `CommandError`.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_backfill_messages.py -v`
Expected: FAIL — `call_command` rejects the unknown `account` / `skip_prereq_gate` kwargs (`TypeError`/`CommandError`).

- [ ] **Step 3: Write the implementation**

In `linkedin/management/commands/backfill_messages.py`, replace the `add_arguments` method (lines 151-164) with:

```python
    def add_arguments(self, parser):
        parser.add_argument(
            "--campaign", type=int, default=None,
            help="Restrict to a single Campaign by primary key. Default: all campaigns.",
        )
        parser.add_argument(
            "--limit", type=int, default=0,
            help="Cap how many Leads to process per pass (0 = all eligible).",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Log in (cheap once cookies are cached), derive sender, list eligible "
                 "Leads, but skip thread fetching.",
        )
        parser.add_argument(
            "--account", choices=[label for label, _u, _p in ACCOUNTS], default=None,
            help="Restrict the run to one configured account slot ('primary' or "
                 "'backfill'). Default: every env-configured account.",
        )
        parser.add_argument(
            "--skip-prereq-gate", action="store_true",
            help="Skip the interactive import-connections staleness gate. Used by "
                 "the daemon's startup catch-up, which runs non-interactively.",
        )
```

In `_handle_impl` (starts line 178), replace the body from the `campaign_id = opts["campaign"]` line through the `if not _run_prereq_gate_for_accounts(configured):` block (lines 179-201) with:

```python
        campaign_id = opts["campaign"]
        limit = opts["limit"]
        dry_run = opts["dry_run"]
        account = opts["account"]
        skip_prereq_gate = opts["skip_prereq_gate"]

        configured = [
            (label, env_user, env_pass)
            for (label, env_user, env_pass) in ACCOUNTS
            if os.getenv(env_user) and os.getenv(env_pass)
        ]
        if not configured:
            raise CommandError(
                "No LinkedIn accounts configured in .env. Set at least one of:\n"
                "  LINKEDIN_USERNAME + LINKEDIN_PASSWORD (primary account), or\n"
                "  BACKFILL_LINKEDIN_USERNAME + BACKFILL_LINKEDIN_PASSWORD (backfill account)."
            )

        # --account restricts the run to a single slot. The daemon's startup
        # catch-up uses this so it never spins up the unrelated backfill
        # account's login + pass.
        if account is not None:
            configured = [c for c in configured if c[0] == account]
            if not configured:
                raise CommandError(
                    f"--account {account!r} is not configured in .env "
                    f"(no username/password env pair for that slot)."
                )

        # Prereq staleness check — backfill_messages depends on
        # import-connections being fresh per operator. Skipped when the
        # caller asks (the startup catch-up runs non-interactively and has
        # already decided to proceed).
        if not skip_prereq_gate:
            if not _run_prereq_gate_for_accounts(configured):
                self.stdout.write(self.style.WARNING("Aborted by operator."))
                return
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_backfill_messages.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add linkedin/management/commands/backfill_messages.py tests/test_backfill_messages.py
git commit -m "Add --account and --skip-prereq-gate options to backfill_messages"
```

---

## Task 10: Startup catch-up

**Files:**
- Create: `linkedin/realtime/catchup.py`
- Test: `tests/realtime/test_catchup.py`

- [ ] **Step 1: Write the failing test**

Create `tests/realtime/test_catchup.py`:

```python
"""Tests for the daemon-startup listener catch-up gap logic."""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.utils import timezone

from linkedin.realtime import catchup


def test_missing_heartbeat_is_infinite_gap():
    with patch("linkedin.realtime.catchup.read_heartbeat", return_value=None):
        assert catchup.compute_gap_minutes("arian@x.com") == float("inf")


def test_recent_heartbeat_is_small_gap():
    recent = timezone.now() - timedelta(minutes=3)
    with patch("linkedin.realtime.catchup.read_heartbeat", return_value=recent):
        gap = catchup.compute_gap_minutes("arian@x.com")
    assert 2 < gap < 4


def test_gap_below_threshold_does_not_prompt_or_backfill():
    recent = timezone.now() - timedelta(minutes=5)
    with patch("linkedin.realtime.catchup.read_heartbeat", return_value=recent), \
         patch("linkedin.realtime.catchup.call_command") as mock_cmd, \
         patch("builtins.input") as mock_input:
        catchup.run_startup_catchup(username="arian@x.com", account_label="primary")
    mock_cmd.assert_not_called()
    mock_input.assert_not_called()


def test_large_gap_headless_logs_warning_no_backfill(caplog):
    old = timezone.now() - timedelta(hours=9)
    with patch("linkedin.realtime.catchup.read_heartbeat", return_value=old), \
         patch("linkedin.realtime.catchup.call_command") as mock_cmd:
        catchup.run_startup_catchup(
            username="arian@x.com", account_label="primary", interactive=False,
        )
    mock_cmd.assert_not_called()
    assert any("listener was off" in r.message.lower() for r in caplog.records)


def test_large_gap_interactive_yes_runs_backfill():
    old = timezone.now() - timedelta(hours=9)
    with patch("linkedin.realtime.catchup.read_heartbeat", return_value=old), \
         patch("linkedin.realtime.catchup.call_command") as mock_cmd, \
         patch("builtins.input", return_value="y"):
        catchup.run_startup_catchup(
            username="arian@x.com", account_label="primary", interactive=True,
        )
    mock_cmd.assert_called_once_with(
        "backfill_messages", account="primary", skip_prereq_gate=True,
    )


def test_large_gap_interactive_no_skips_backfill():
    old = timezone.now() - timedelta(hours=9)
    with patch("linkedin.realtime.catchup.read_heartbeat", return_value=old), \
         patch("linkedin.realtime.catchup.call_command") as mock_cmd, \
         patch("builtins.input", return_value="n"):
        catchup.run_startup_catchup(
            username="arian@x.com", account_label="primary", interactive=True,
        )
    mock_cmd.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/realtime/test_catchup.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'linkedin.realtime.catchup'`

- [ ] **Step 3: Write the implementation**

Create `linkedin/realtime/catchup.py`:

```python
"""Daemon-startup catch-up for the realtime listener.

The listener only runs during active hours and only while the process is
up. The heartbeat file records when it was last alive; the gap between
that and now is exactly the window inbound messages may have been missed
(off-hours + any downtime). On startup, if the gap exceeds a threshold:

  - Interactive (TTY): prompt to run backfill_messages first.
  - Headless (no TTY): log a WARNING; don't auto-run a long unattended
    LinkedIn session.

Below the threshold (a quick restart), do nothing.
"""
from __future__ import annotations

import logging
import sys

from django.core.management import call_command
from django.utils import timezone

from linkedin.conf import LISTENER_CATCHUP_GAP_MINUTES
from linkedin.realtime.heartbeat import read_heartbeat

logger = logging.getLogger(__name__)


def compute_gap_minutes(username: str, now=None) -> float:
    """Minutes since the listener heartbeat. inf if there is no heartbeat."""
    last = read_heartbeat(username)
    if last is None:
        return float("inf")
    now = now or timezone.now()
    return (now - last).total_seconds() / 60.0


def run_startup_catchup(
    *,
    username: str,
    account_label: str,
    interactive: bool | None = None,
) -> None:
    """Surface (and optionally backfill) the listener's missed window.

    `username` is the LinkedIn username of the daemon's account (keys the
    heartbeat file). `account_label` is the backfill_messages slot for that
    account ("primary"). `interactive` defaults to whether stdin is a TTY.
    """
    gap = compute_gap_minutes(username)
    if gap < LISTENER_CATCHUP_GAP_MINUTES:
        logger.info(
            "Realtime listener gap %.0f min (< %d min threshold) — no catch-up needed",
            gap if gap != float("inf") else -1, LISTENER_CATCHUP_GAP_MINUTES,
        )
        return

    hours = gap / 60.0
    gap_desc = "an unknown duration" if gap == float("inf") else f"~{hours:.1f}h"

    if interactive is None:
        interactive = sys.stdin.isatty()

    if not interactive:
        logger.warning(
            "Realtime listener was off for %s — inbound messages in that "
            "window were not detected. Run `manage.py backfill_messages "
            "--account %s` to catch up.",
            gap_desc, account_label,
        )
        return

    answer = input(
        f"Listener was off {gap_desc}. Run backfill_messages to catch up "
        f"first? [y/N] "
    ).strip().lower()
    if answer == "y":
        logger.info("Running backfill_messages catch-up for account=%s", account_label)
        call_command(
            "backfill_messages", account=account_label, skip_prereq_gate=True,
        )
    else:
        logger.info("Skipped backfill catch-up — continuing into the task loop")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/realtime/test_catchup.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add linkedin/realtime/catchup.py tests/realtime/test_catchup.py
git commit -m "Add daemon-startup listener catch-up"
```

---

## Task 11: Daemon + login wiring

This task connects the pieces into the daemon. The browser/CDP behaviour is verified manually in Task 12; there is no unit test for `run_daemon`'s loop. The changes are deliberately minimal.

**Files:**
- Modify: `linkedin/daemon.py`

- [ ] **Step 1: Add the realtime imports**

In `linkedin/daemon.py`, extend the `linkedin.conf` import block (lines 16-27) to include the listener flag — change the import list to add `ENABLE_REALTIME_LISTENER`:

```python
from linkedin.conf import (
    ACTIVE_END_HOUR,
    ACTIVE_START_HOUR,
    ACTIVE_TIMEZONE,
    CAMPAIGN_CONFIG,
    ENABLE_AUTO_DISCOVERY,
    ENABLE_FOLLOW_UP,
    ENABLE_FREEMIUM_CAMPAIGN,
    ENABLE_REALTIME_LISTENER,
    ENABLE_SWEEP_CONNECTIONS,
    ENABLE_ACTIVE_HOURS,
    REST_DAYS,
)
```

- [ ] **Step 2: Run the startup catch-up before healing**

In `run_daemon`, immediately before the `# Startup healing` comment + `heal_tasks(session)` call (line 403-404), add:

```python
    # Realtime listener startup catch-up — surface (and optionally backfill)
    # the window the listener was off (off-hours + any downtime). Runs
    # before the task loop; reads the heartbeat file written by the
    # listener's pump. No-op when the listener is disabled.
    if ENABLE_REALTIME_LISTENER:
        from linkedin.realtime.catchup import run_startup_catchup
        run_startup_catchup(
            username=session.linkedin_profile.linkedin_username,
            account_label="primary",
        )
```

- [ ] **Step 3: Open the listener and pump the idle waits**

In `run_daemon`, the `while True:` loop currently starts at line 430. Replace the loop body from `while True:` through the queue-idle `time.sleep(wait)` block (lines 430-454) with:

```python
    while True:
        # Close stale DB connections at the top of every loop iteration.
        # Neon's idle timeout can kill the SSL socket during any sleep.
        connections.close_all()

        pause = seconds_until_active()
        if pause > 0:
            # Off-hours: close the listener tab so the account doesn't hold
            # a live LinkedIn realtime connection overnight (a mild bot
            # signal). The next startup catch-up reconciles the gap.
            from linkedin.realtime.listener import stop_realtime_listener
            stop_realtime_listener(session)
            h, m = int(pause // 3600), int(pause % 3600 // 60)
            logger.info("Outside active hours — sleeping %dh%02dm", h, m)
            connections.close_all()
            time.sleep(pause)
            continue

        # Active hours: ensure the listener tab is up (creates it on first
        # iteration, recovers it after a browser relaunch). Degrades to
        # polling if it can't start.
        from linkedin.realtime.listener import ensure_realtime_listener
        listener = ensure_realtime_listener(session, operator=our_operator)

        task = Task.objects.claim_next(operator=our_operator)
        if task is None:
            wait = Task.objects.seconds_to_next(operator=our_operator)
            if wait is None:
                logger.info("Queue empty — nothing to do")
                return
            if wait > 0:
                h, m = int(wait // 3600), int(wait % 3600 // 60)
                logger.info("Next task in %dh%02dm — sleeping", h, m)
                connections.close_all()
                # Chunked Playwright-pumping wait so CDP callbacks fire
                # promptly during the idle window. Falls back to a plain
                # sleep if the listener isn't available.
                if listener is not None and listener.is_alive:
                    listener.pump(wait)
                else:
                    time.sleep(wait)
            continue
```

> The off-hours `time.sleep(pause)` stays a plain sleep — only the listener tab is closed around it (per spec Lifecycle). The queue-idle `time.sleep(wait)` becomes `listener.pump(wait)`.

- [ ] **Step 4: Verify the daemon module imports cleanly**

Run: `.venv/bin/python -c "import django, os; os.environ.setdefault('DJANGO_SETTINGS_MODULE','linkedin.django_settings'); django.setup(); import linkedin.daemon"`
Expected: no output, exit 0 (no `ImportError` / `SyntaxError`).

- [ ] **Step 5: Run the full daemon-resilience + task suite to confirm no regression**

Run: `.venv/bin/pytest tests/test_daemon_resilience.py tests/test_heal.py tests/tasks/ -v`
Expected: PASS (all pre-existing tests still green — the loop edits are additive and gated on `ENABLE_REALTIME_LISTENER`, default off).

- [ ] **Step 6: Commit**

```bash
git add linkedin/daemon.py
git commit -m "Wire realtime listener into the daemon loop and startup"
```

---

## Task 12: Manual integration test + docs

The listener's browser/CDP path is not unit-testable. This task is a manual end-to-end verification plus the required doc sync.

**Files:**
- Modify: `CLAUDE.md`
- Modify: `ARCHITECTURE.md`

- [ ] **Step 1: Manual integration test**

With `ENABLE_REALTIME_LISTENER=true` and a valid `SLACK_WEBHOOK_URL` in `.env`, run the daemon: `make run` (or `.venv/bin/python manage.py`). Verify, from the logs and Slack:

1. Startup log shows the catch-up line (gap computed; prompt on a TTY).
2. After the browser is ready, log shows `Realtime listener tab opened — observing /realtime/connect`.
3. A second browser tab is visibly on `/messaging/`.
4. From another LinkedIn account, send a DM to a lead that exists in the CRM at `state >= CONNECTED`. Within ~30s: a `Realtime inbound message persisted` log line, a `crm.Message` row (check Django Admin), and an `:envelope:` Slack notification.
5. Send a DM to someone NOT in the CRM → a `no matching Lead, skipped` WARNING, no crash.
6. Confirm `data/listener-heartbeat-<...>.json` exists and its `last_alive` advances.
7. Stop the daemon, wait >30 min (or temporarily lower `LISTENER_CATCHUP_GAP_MINUTES`), restart → catch-up prompt offers `backfill_messages`.

Record the outcome of each numbered check in the commit message or a scratch note. If the parser misfires on a real event, re-run `scripts/capture_realtime_events.py`, add the offending event as a fixture under `tests/fixtures/realtime/`, and extend `tests/realtime/test_parser.py`.

- [ ] **Step 2: Update CLAUDE.md**

In `CLAUDE.md`, under the **Architecture (quick reference)** section, add a bullet after the **Message store** bullet:

```markdown
- **Realtime listener**: `linkedin/realtime/` — near-realtime inbound LinkedIn DM detection. Gated by `ENABLE_REALTIME_LISTENER` (`conf.py`, default off). The daemon opens a second browser tab on `/messaging/` in its existing context and observes LinkedIn's own realtime stream (`/realtime/connect`, a streaming `text/event-stream` fetch) via CDP `Network.streamResourceContent` + `Network.dataReceived` — no injected script, no second connection. `sse.py` frames the base64 stream chunks into decoded SSE events; `parser.py` turns each into a `ParsedRealtimeMessage` or `None`; `handler.py` resolves the `Lead` (by conversation URN, then sender URN), persists via `linkedin/db/messages.py` (idempotent), and Slack-notifies inbound messages (`notify_message_received`). The daemon's queue-idle `time.sleep` is replaced by `RealtimeListener.pump()` — a chunked Playwright-pumping wait that also refreshes `data/listener-heartbeat-<account>.json`. On startup, `catchup.py` reads that heartbeat and, if the gap exceeds `LISTENER_CATCHUP_GAP_MINUTES`, prompts (TTY) or warns (headless) to run `backfill_messages --account primary`. The listener tab is closed during off-hours. Realtime is an enhancement — any failure degrades to the existing polling.
```

Also, in the `backfill_messages` command-block comment in `CLAUDE.md`, note the new flag — add to the usage line:

```markdown
# --account primary|backfill restricts the run to one configured slot;
# --skip-prereq-gate skips the interactive staleness gate (used by the
# daemon's realtime-listener startup catch-up).
```

- [ ] **Step 3: Update ARCHITECTURE.md**

In `ARCHITECTURE.md`, add a section documenting the `linkedin/realtime/` package: the modules and their responsibilities (sse, parser, heartbeat, lead_lookup, handler, listener, catchup), the CDP `streamResourceContent` capture approach (and why `eventSourceMessageReceived` does not work — LinkedIn's `/realtime/connect` is a streaming fetch, not a native EventSource), the lifecycle (open after browser ready, close off-hours, recover on browser relaunch), and the startup catch-up. Match the depth and voice of the existing module sections.

- [ ] **Step 4: Run the full test suite**

Run: `.venv/bin/pytest tests/realtime/ tests/test_conf.py tests/test_slack_notify.py tests/test_backfill_messages.py -v`
Expected: PASS (all realtime + touched-module tests green).

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md ARCHITECTURE.md
git commit -m "Document realtime inbound message listener"
```

---

## Self-review

**Spec coverage:**
- Detect inbound DMs near-realtime via CDP → Tasks 2, 3, 8, 11. ✓
- Persist to `crm.Message` + Slack notify → Tasks 6, 7. ✓
- Event parser as the unit-tested core → Task 3. ✓
- Event handler with try/except, unresolved-sender skip, idempotency → Task 7. ✓
- Listener tab in existing context + CDP subscribe → Task 8. ✓
- Daemon-loop change: chunked pump replacing queue-idle sleep → Task 11. ✓
- Heartbeat file in `data/`, per-account → Task 4, written by `pump()` Task 8. ✓
- Startup catch-up: gap, TTY prompt vs headless warning, threshold → Task 10. ✓
- `backfill_messages --account` (account scoping) + `--skip-prereq-gate` → Task 9. ✓
- Off-hours listener tab close/reopen → Task 11 Step 3. ✓
- Crash recovery re-establishes the listener → `ensure_realtime_listener` (Task 8) called every active iteration (Task 11). ✓
- `ENABLE_REALTIME_LISTENER` feature flag, default off → Task 1. ✓
- Error handling: degrade to polling on listener failure → `ensure_realtime_listener` swallows start failures (Task 8). ✓
- Open research item (payload shape) → Task 2 capture spike grounds Task 3. ✓
- Testing: parser/handler/catch-up/heartbeat units + manual browser test → Tasks 3, 4, 7, 10, 12. ✓

**Deviation from spec (intentional, documented in Task 7):** The spec's parser drops outbound echoes entirely. This plan instead has the parser return *every* message event and lets the handler persist it, deriving direction from the persisted row and Slack-notifying only inbound. Rationale: persisting outbound echoes is harmless and idempotent with `backfill_messages`, keeps DB threads complete, and removes the parser's dependency on knowing the daemon's own member URN (`ensure_self_profile` returns `None` after first run, so that URN isn't reliably available). Net behaviour for the user-facing goal — Slack notifications for inbound messages — is identical.

**Type consistency:** `ParsedRealtimeMessage` fields (`entity_urn`, `conversation_urn`, `sender_name`, `sender_member_urn`, `text`, `timestamp`) are referenced consistently in Tasks 3, 7, 8. `resolve_lead_for_realtime(conversation_urn=, sender_member_urn=)` keyword args match between Tasks 5 and 7. `handle_realtime_event(raw, *, operator=)` matches between Tasks 7 and 8. `RealtimeListener(session, *, operator=)`, `.start()`, `.pump()`, `.stop()`, `.is_alive`, `ensure_realtime_listener`, `stop_realtime_listener` match between Tasks 8 and 11. `run_startup_catchup(username=, account_label=, interactive=)` matches between Tasks 10 and 11. `backfill_messages` `account` / `skip_prereq_gate` opt keys match between Tasks 9 and 10.

**Placeholder scan:** No TBD/TODO. The one judgement step is Task 3 Step 4 (confirm `_*_KEYS` against the captured fixture) and Task 12 Step 1 (correct paths if a live event misfires) — both are inherent to a reverse-engineered wire format, bounded to editing key-name tuples, and verified by fixture-derived tests, not open-ended.
