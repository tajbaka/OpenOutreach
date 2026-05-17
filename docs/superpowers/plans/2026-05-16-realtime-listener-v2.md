# Realtime Listener v2 (CDP-shared browser) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Re-architect the realtime inbound-message listener to run as a separate OS process that shares the daemon's browser over CDP — eliminating the sync-Playwright greenlet corruption that broke the in-process v1 design, with zero added bot-detection surface.

**Architecture:** The daemon launches Chromium as a *persistent context* with a fixed `--remote-debugging-port`. A child process (`manage.py listen_realtime`) `connect_over_cdp`s to that same browser, shares the one context (one device, one cookie jar), opens its own `/messaging/` tab, and streams LinkedIn's realtime feed via CDP `streamResourceContent`. Separate processes → separate Playwright/asyncio loops → the greenlet corruption is structurally impossible. The daemon spawns and supervises the child (restart on death, kill off-hours).

**Tech Stack:** Python 3.13, Django, Playwright sync API (`launch_persistent_context`, `connect_over_cdp`, CDP sessions), `subprocess`, pytest.

**Source spec:** `docs/superpowers/specs/2026-05-16-realtime-listener-v2-design.md`

---

## Context for the implementer

This plan modifies an in-progress feature branch (`realtime-message-listener`). The v1 in-process listener already shipped these modules, **which v2 reuses unchanged**: `linkedin/realtime/sse.py`, `parser.py`, `handler.py`, `lead_lookup.py`, `heartbeat.py`, `catchup.py`. Do not modify them.

v2 **replaces** `linkedin/realtime/listener.py` (was an in-process tab; becomes the child-process listener) and **reverts** the v1 daemon-loop wiring in `linkedin/daemon.py`.

**No live testing.** Do not run the daemon (`make run` / `manage.py` with no args), do not run `manage.py listen_realtime`, do not launch a browser against LinkedIn. Unit tests (`pytest`) only — they mock the browser and `subprocess`. Live integration testing is explicitly deferred to the operator.

**Project rules (from CLAUDE.md):** `.venv/bin/python` / `.venv/bin/pytest` (add `--reuse-db` if a Postgres `DuplicateDatabase` test-DB error appears); single-line commit messages, no `Co-Authored-By`; branch is `realtime-message-listener` — do not branch, do not touch `main`. Error handling: crash on unexpected errors; `try/except` only for expected, recoverable ones.

## File structure

Modified:
- `linkedin/conf.py` — add `LISTENER_CDP_PORT`.
- `linkedin/browser/cookie_store.py` — add `profile_dir_for(username)`.
- `linkedin/browser/login.py` — `launch_browser` → `launch_persistent_context`; `start_browser_session` reworked (profile dir, first-run cookie seeding, corrupt-profile fallback).
- `linkedin/browser/session.py` — `AccountSession.ensure_browser` simplified; `_maybe_refresh_cookies` removed; `close()` handles a `None` browser.
- `linkedin/realtime/listener.py` — fully rewritten as the listener-process module.
- `linkedin/daemon.py` — revert v1 listener wiring; add the supervisor.
- `CLAUDE.md`, `ARCHITECTURE.md` — doc sync.

Created:
- `linkedin/realtime/supervisor.py` — `ListenerSupervisor` (subprocess spawn / liveness / capped restart / off-hours kill).
- `linkedin/management/commands/listen_realtime.py` — the child-process entrypoint.

Test files:
- `tests/test_conf.py` — extend (CDP port).
- `tests/realtime/test_cookie_paths.py` — new (`profile_dir_for`).
- `tests/realtime/test_listener.py` — **replace** (old in-process tests are obsolete).
- `tests/realtime/test_supervisor.py` — new.

---

## Task 1: Config — `LISTENER_CDP_PORT`

**Files:**
- Modify: `linkedin/conf.py` (in the realtime-listener constant block, after `LISTENER_PUMP_SLICE_SECONDS`)
- Test: `tests/test_conf.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_conf.py`, inside the existing `TestRealtimeListenerConfig` class:

```python
    def test_cdp_port_has_default(self, monkeypatch):
        import importlib
        import linkedin.conf as conf
        monkeypatch.delenv("LISTENER_CDP_PORT", raising=False)
        importlib.reload(conf)
        assert conf.LISTENER_CDP_PORT == 9222
        importlib.reload(conf)

    def test_cdp_port_env_override(self, monkeypatch):
        import importlib
        import linkedin.conf as conf
        monkeypatch.setenv("LISTENER_CDP_PORT", "9444")
        importlib.reload(conf)
        assert conf.LISTENER_CDP_PORT == 9444
        importlib.reload(conf)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest "tests/test_conf.py::TestRealtimeListenerConfig::test_cdp_port_has_default" -v`
Expected: FAIL with `AttributeError: module 'linkedin.conf' has no attribute 'LISTENER_CDP_PORT'`

- [ ] **Step 3: Write the implementation**

In `linkedin/conf.py`, immediately after the `LISTENER_PUMP_SLICE_SECONDS` line, add:

```python
# Fixed CDP port the daemon exposes on its Chromium (`--remote-debugging-port`)
# and the realtime listener child process connects to (`connect_over_cdp`).
# Localhost-only. The daemon only opens the port when ENABLE_REALTIME_LISTENER
# is on; the listener connects to it.
LISTENER_CDP_PORT = int(os.getenv("LISTENER_CDP_PORT") or 9222)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_conf.py::TestRealtimeListenerConfig -v`
Expected: PASS (all tests in the class)

- [ ] **Step 5: Commit**

```bash
git add linkedin/conf.py tests/test_conf.py
git commit -m "Add LISTENER_CDP_PORT config constant"
```

---

## Task 2: Cookie store — `profile_dir_for`

The daemon moves to a persistent-context profile directory. Add a path helper alongside the existing `cookie_path_for`.

**Files:**
- Modify: `linkedin/browser/cookie_store.py`
- Test: `tests/realtime/test_cookie_paths.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/realtime/test_cookie_paths.py`:

```python
"""Tests for cookie_store path helpers."""
from __future__ import annotations

import pytest

from linkedin.browser import cookie_store


def test_profile_dir_is_per_username(tmp_path, monkeypatch):
    monkeypatch.setattr(cookie_store, "ROOT_DIR", tmp_path)
    p1 = cookie_store.profile_dir_for("arian@tryfedrampgpt.com")
    p2 = cookie_store.profile_dir_for("chukyjack@gmail.com")
    assert p1 != p2
    assert p1.name == "profile-arian-tryfedrampgpt-com"
    assert p1.parent == tmp_path / "data"


def test_profile_dir_empty_username_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(cookie_store, "ROOT_DIR", tmp_path)
    with pytest.raises(ValueError):
        cookie_store.profile_dir_for("")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/realtime/test_cookie_paths.py -v`
Expected: FAIL with `AttributeError: module 'linkedin.browser.cookie_store' has no attribute 'profile_dir_for'`

- [ ] **Step 3: Write the implementation**

In `linkedin/browser/cookie_store.py`, add this function immediately after `cookie_path_for`:

```python
def profile_dir_for(username: str) -> Path:
    """Return the persistent-context profile directory for a LinkedIn username.

    Convention mirrors `cookie_path_for`: `data/profile-<safe_username>/`.
    Used by the daemon's `launch_persistent_context` (the listener child
    process shares this context over CDP).
    """
    safe = _SAFE_NAME_RE.sub("-", (username or "").lower()).strip("-")
    if not safe:
        raise ValueError("cannot derive profile dir from empty username")
    return ROOT_DIR / "data" / f"profile-{safe}"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/realtime/test_cookie_paths.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add linkedin/browser/cookie_store.py tests/realtime/test_cookie_paths.py
git commit -m "Add profile_dir_for helper for persistent-context profiles"
```

---

## Task 3: Persistent-context migration (`login.py` + `session.py`)

The daemon's browser moves from `launch()` + `new_context(storage_state=…)` to `launch_persistent_context(profile_dir, …)`. This makes the daemon's context the browser's *default* context — the only kind a `connect_over_cdp` client can see (verified by spike). A persistent context also self-persists cookies to disk, so the manual JSON save/load and the `li_at` expiry-peek go away.

This task is browser code — **not unit-testable without a live browser**. Verification is an import check + the daemon regression suite. Live verification is deferred to the operator.

**Files:**
- Modify: `linkedin/browser/login.py` (`launch_browser`, `start_browser_session`)
- Modify: `linkedin/browser/session.py` (`AccountSession.ensure_browser`, `_maybe_refresh_cookies`, `close`)

- [ ] **Step 1: Rewrite `launch_browser` in `linkedin/browser/login.py`**

Replace the entire `launch_browser` function with:

```python
def launch_browser(profile_dir, *, cdp_port=None, seed_cookies=None):
    """Launch Chromium as a persistent context rooted at `profile_dir`.

    A persistent context is the browser's *default* context — required so
    the realtime listener child process can see it via `connect_over_cdp`
    (a plain `new_context()` context is invisible to CDP-connecting peers).
    Cookies/localStorage persist in `profile_dir` automatically; there is no
    JSON storage_state to load or save.

    `cdp_port` (when set) exposes `--remote-debugging-port` so the listener
    can attach. `seed_cookies` (a Playwright cookies list) is added once on
    a first run to migrate an account off the legacy JSON cookie jar without
    forcing a re-login.

    Returns `(page, context, browser, playwright)`. NOTE: `browser` is
    `None` for a persistent context — closing the context closes the
    browser. Callers must tolerate a `None` browser.
    """
    playwright = sync_playwright().start()
    args = []
    if cdp_port:
        args.append(f"--remote-debugging-port={cdp_port}")
    context = playwright.chromium.launch_persistent_context(
        str(profile_dir),
        headless=False,
        slow_mo=BROWSER_SLOW_MO,
        args=args,
    )
    context.set_default_timeout(BROWSER_DEFAULT_TIMEOUT_MS)
    Stealth().apply_stealth_sync(context)
    if seed_cookies:
        try:
            context.add_cookies(seed_cookies)
            logger.info("Seeded %d cookies into new persistent profile", len(seed_cookies))
        except Exception as e:
            logger.warning("Cookie seeding failed (will fall back to login): %s", e)
    page = context.pages[0] if context.pages else context.new_page()
    return page, context, context.browser, playwright
```

- [ ] **Step 2: Rewrite `start_browser_session` in `linkedin/browser/login.py`**

Replace the entire `start_browser_session` function with:

```python
def start_browser_session(session: "AccountSession", handle: str):
    """Bring the session online with a persistent-context Chromium.

    Profile lives at `data/profile-<safe_username>/`. On the first run
    (profile dir absent) the legacy `data/cookies-<safe_username>.json` jar,
    if present, seeds the new context so the cutover skips a re-login.
    After launch, `/feed/` is checked; a bounce to login/checkpoint means
    the session is stale → interactive `playwright_login` (the persistent
    context records the result itself — no save step).
    """
    from linkedin.browser.cookie_store import cookie_path_for, load_cookies, profile_dir_for
    from linkedin.conf import ENABLE_REALTIME_LISTENER, LISTENER_CDP_PORT

    logger.debug("Configuring persistent-context browser for @%s", handle)

    linkedin_username = session.linkedin_profile.linkedin_username
    profile_dir = profile_dir_for(linkedin_username)
    cdp_port = LISTENER_CDP_PORT if ENABLE_REALTIME_LISTENER else None

    seed_cookies = None
    if not profile_dir.exists():
        legacy = load_cookies(cookie_path_for(linkedin_username))
        if legacy and legacy.get("cookies"):
            seed_cookies = legacy["cookies"]
            logger.info("First persistent-context run for @%s — seeding from legacy cookie jar", handle)

    try:
        session.page, session.context, session.browser, session.playwright = launch_browser(
            profile_dir, cdp_port=cdp_port, seed_cookies=seed_cookies,
        )
    except Exception:
        logger.warning("Persistent profile for @%s failed to launch — wiping and retrying fresh", handle)
        import shutil
        shutil.rmtree(profile_dir, ignore_errors=True)
        session.page, session.context, session.browser, session.playwright = launch_browser(
            profile_dir, cdp_port=cdp_port, seed_cookies=None,
        )

    if not _cookies_still_valid(session):
        logger.warning("Session for @%s not valid (landed on %s) — authenticating",
                        handle, urlparse(session.page.url).path)
        playwright_login(session)
        logger.info(colored("Login successful — persistent profile at %s", "green", attrs=["bold"]), profile_dir)

    session.page.wait_for_load_state("load")
    logger.info(colored("Browser ready", "green", attrs=["bold"]))
```

> The old `start_browser_session` referenced `clear_cookies` / `save_cookies` — those imports are no longer used by this function. Leave the `from linkedin.browser.cookie_store import (...)` module-level import block as-is if other code in `login.py` uses it; otherwise it is harmless. Do not delete `cookie_store.py`'s JSON helpers — `StandaloneLinkedInSession` still uses them.

- [ ] **Step 3: Simplify `AccountSession` in `linkedin/browser/session.py`**

Replace the `ensure_browser` method and **delete** the `_maybe_refresh_cookies` method. The new `ensure_browser`:

```python
    def ensure_browser(self):
        """Launch or recover the persistent-context browser. Call before using .page.

        A persistent context self-maintains its session on disk, so there is
        no cookie-refresh step — a live page needs nothing; a closed/absent
        page triggers a relaunch (which re-opens the same profile dir).
        """
        from linkedin.browser.login import start_browser_session

        if not self.page or self.page.is_closed():
            logger.debug("Launching/recovering persistent-context browser for %s", self.handle)
            start_browser_session(session=self, handle=self.handle)
```

Delete the entire `_maybe_refresh_cookies` method (and the `_AUTH_COOKIE_NAME` module constant if nothing else references it — check with grep; if `cookie_store.py` or others use it, leave it).

In `close()`, the existing `if self.browser:` guard already tolerates a `None` browser (persistent context returns `browser=None`). Verify `close()` reads:

```python
    def close(self):
        if self.context:
            try:
                self.context.close()
                if self.browser:
                    self.browser.close()
                if self.playwright:
                    self.playwright.stop()
                logger.info("Browser closed gracefully (%s)", self.handle)
            except Exception as e:
                logger.debug("Error closing browser: %s", e)
            finally:
                self.page = self.context = self.browser = self.playwright = None
        logger.info("Account session closed → %s", self.handle)
```

If `close()` already looks like this, leave it. (Closing the persistent context closes the browser; `self.browser` is `None` so the `if self.browser` branch is skipped — correct.)

- [ ] **Step 4: Verify imports + run the daemon regression suite**

Run: `.venv/bin/python -c "import django, os; os.environ.setdefault('DJANGO_SETTINGS_MODULE','linkedin.django_settings'); django.setup(); import linkedin.browser.login, linkedin.browser.session"`
Expected: exit 0, no `ImportError` / `SyntaxError` / `NameError`.

Run: `.venv/bin/pytest tests/test_daemon_resilience.py tests/test_onboarding.py -q --reuse-db`
Expected: no *new* failures vs. the branch baseline. (Pre-existing failures unrelated to the browser launch are acceptable — compare against a baseline run if unsure. The daemon-resilience tests mock the browser, so they should be unaffected.)

- [ ] **Step 5: Commit**

```bash
git add linkedin/browser/login.py linkedin/browser/session.py
git commit -m "Migrate daemon browser to a persistent context (CDP-shareable)"
```

---

## Task 4: Rewrite `listener.py` as the listener-process module

The v1 `listener.py` (`RealtimeListener` in-process tab + `ensure_realtime_listener`/`stop_realtime_listener`) is fully replaced. The new module is the logic the `listen_realtime` child process runs: connect to the daemon's browser over CDP, share its context, stream, reconnect on drop.

The CDP/browser parts are not unit-testable; the reconnect-loop control flow (`run_listener`) **is** — Task tests it with `_run_one_connection` mocked.

**Files:**
- Modify (full rewrite): `linkedin/realtime/listener.py`
- Replace: `tests/realtime/test_listener.py`

- [ ] **Step 1: Write the failing test**

Replace the entire contents of `tests/realtime/test_listener.py` with:

```python
"""Tests for the realtime listener process — the reconnect control loop.

The CDP/browser path (_run_one_connection) needs a live browser and is
covered by the deferred manual integration test, not here.
"""
from __future__ import annotations

from unittest.mock import patch

from linkedin.realtime import listener


def test_run_listener_gives_up_after_max_quick_failures(monkeypatch):
    """Quick consecutive connect failures exhaust the cap → exit code 1."""
    monkeypatch.setattr(listener, "_RECONNECT_DELAY_SECONDS", 0)
    calls = {"n": 0}

    def always_fail(**kwargs):
        calls["n"] += 1
        raise RuntimeError("no browser on CDP port")

    with patch.object(listener, "_run_one_connection", side_effect=always_fail), \
         patch.object(listener.time, "sleep"):
        code = listener.run_listener(operator="Arian", username="a@x.com", cdp_port=9222)

    assert code == 1
    assert calls["n"] == listener._MAX_CONSECUTIVE_FAILURES


def test_run_listener_resets_failures_after_a_real_connection(monkeypatch):
    """A connection that lasted a while (then dropped) resets the failure
    count — a long-lived listener that reconnects forever never exits."""
    monkeypatch.setattr(listener, "_RECONNECT_DELAY_SECONDS", 0)
    monkeypatch.setattr(listener, "_MAX_CONSECUTIVE_FAILURES", 3)
    state = {"n": 0}
    times = iter([0.0, 999.0, 999.0,   # call 1: lasted 999s → reset
                  1000.0, 1001.0,      # call 2: lasted 1s → failure 1
                  1002.0, 1003.0,      # call 3: 1s → failure 2
                  1004.0, 1005.0])     # call 4: 1s → failure 3 → give up

    def conn(**kwargs):
        state["n"] += 1
        raise RuntimeError("dropped")

    monkeypatch.setattr(listener.time, "monotonic", lambda: next(times))
    with patch.object(listener, "_run_one_connection", side_effect=conn), \
         patch.object(listener.time, "sleep"):
        code = listener.run_listener(operator="Arian", username="a@x.com", cdp_port=9222)

    assert code == 1
    # call 1 reset the counter, so it took 1 (reset) + 3 (fail) = 4 attempts
    assert state["n"] == 4
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/realtime/test_listener.py -v`
Expected: FAIL — the new `run_listener` / `_run_one_connection` / `_MAX_CONSECUTIVE_FAILURES` symbols don't exist yet (the old `listener.py` has `RealtimeListener` instead).

- [ ] **Step 3: Write the implementation**

Replace the entire contents of `linkedin/realtime/listener.py` with:

```python
"""Realtime listener — runs as a child process spawned by the daemon.

Connects to the daemon's already-running Chromium over CDP (the daemon
launches a persistent context with a fixed --remote-debugging-port),
shares that one context (= one device, one cookie jar), opens its own
/messaging/ tab, and streams LinkedIn's realtime feed via CDP
Network.streamResourceContent.

Because this runs in a SEPARATE process from the daemon, it has its own
Playwright/asyncio loop — the sync-API greenlet corruption that killed the
in-process design cannot occur here. This is the topology the capture
spike proved out.

The daemon's supervisor (linkedin/realtime/supervisor.py) spawns and
restarts this process; the entrypoint is `manage.py listen_realtime`.
"""
from __future__ import annotations

import base64
import logging
import time

from playwright.sync_api import sync_playwright

from linkedin.conf import LISTENER_CDP_PORT, LISTENER_PUMP_SLICE_SECONDS
from linkedin.realtime.handler import handle_realtime_event
from linkedin.realtime.heartbeat import write_heartbeat
from linkedin.realtime.sse import RealtimeSSEBuffer

logger = logging.getLogger(__name__)

MESSAGING_URL = "https://www.linkedin.com/messaging/"
_REALTIME_CONNECT_PATH = "/realtime/connect"
_RECONNECT_DELAY_SECONDS = 10
# After this many quick consecutive connect failures the daemon's browser
# is presumed genuinely gone; the process exits non-zero and the daemon's
# supervisor decides whether to respawn.
_MAX_CONSECUTIVE_FAILURES = 30
# A connection that survived at least this long counts as "worked, then
# dropped" — the failure counter resets so a long-lived listener that
# reconnects across daemon browser-relaunches never exhausts the cap.
_HEALTHY_CONNECTION_SECONDS = 60


def run_listener(*, operator: str, username: str, cdp_port: int | None = None) -> int:
    """Listener process main loop. Returns a process exit code (0 never —
    it loops until the cap is hit, then returns 1).

    Maintains a CDP connection to the daemon's browser; on any drop,
    reconnects after a short delay. Exits 1 only after
    `_MAX_CONSECUTIVE_FAILURES` quick failures in a row.
    """
    cdp_port = LISTENER_CDP_PORT if cdp_port is None else cdp_port
    failures = 0
    while failures < _MAX_CONSECUTIVE_FAILURES:
        started = time.monotonic()
        try:
            _run_one_connection(cdp_port=cdp_port, operator=operator, username=username)
        except Exception as e:
            lasted = time.monotonic() - started
            if lasted >= _HEALTHY_CONNECTION_SECONDS:
                failures = 0
                logger.warning("listener: connection dropped after %.0fs — reconnecting", lasted)
            else:
                failures += 1
                logger.warning(
                    "listener: connect attempt failed (%d/%d): %s",
                    failures, _MAX_CONSECUTIVE_FAILURES, e,
                )
        time.sleep(_RECONNECT_DELAY_SECONDS)
    logger.error("listener: gave up after %d failed reconnects — exiting", failures)
    return 1


def _run_one_connection(*, cdp_port: int, operator: str, username: str) -> None:
    """One CDP connection lifecycle: connect, wire the stream, pump until
    the connection drops (at which point a Playwright call raises and the
    exception propagates to `run_listener`'s reconnect loop).
    """
    buffer = RealtimeSSEBuffer()
    stream_request_ids: set = set()

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(f"http://localhost:{cdp_port}")
        if not browser.contexts:
            raise RuntimeError("no shared browser context available over CDP")
        context = browser.contexts[0]
        page = context.new_page()
        cdp = context.new_cdp_session(page)
        cdp.send("Network.enable")

        def _dispatch(data_b64: str) -> None:
            try:
                text = base64.b64decode(data_b64).decode("utf-8", errors="replace")
            except Exception as e:
                logger.warning("listener: undecodable stream chunk dropped: %s", e)
                return
            for event in buffer.feed(text):
                handle_realtime_event(event, operator=operator)

        def _on_request(params: dict) -> None:
            url = (params.get("request") or {}).get("url", "")
            rid = params.get("requestId")
            if rid and _REALTIME_CONNECT_PATH in url:
                stream_request_ids.add(rid)

        def _on_response(params: dict) -> None:
            rid = params.get("requestId")
            if rid not in stream_request_ids:
                return
            try:
                result = cdp.send("Network.streamResourceContent", {"requestId": rid})
            except Exception as e:
                logger.warning("listener: streamResourceContent failed: %s", e)
                return
            buffered = result.get("bufferedData")
            if buffered:
                _dispatch(buffered)

        def _on_data(params: dict) -> None:
            if params.get("requestId") not in stream_request_ids:
                return
            data_b64 = params.get("data")
            if data_b64:
                _dispatch(data_b64)

        cdp.on("Network.requestWillBeSent", _on_request)
        cdp.on("Network.responseReceived", _on_response)
        cdp.on("Network.dataReceived", _on_data)

        page.goto(MESSAGING_URL, wait_until="domcontentloaded")
        logger.info("listener: connected over CDP, observing %s", _REALTIME_CONNECT_PATH)

        # Pump loop — keeps the Playwright event loop turning so CDP
        # callbacks fire, and refreshes the heartbeat each slice. A dropped
        # connection makes wait_for_timeout raise → propagates out.
        slice_ms = LISTENER_PUMP_SLICE_SECONDS * 1000
        while True:
            page.wait_for_timeout(slice_ms)
            write_heartbeat(username)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/realtime/test_listener.py -v`
Expected: PASS (2 tests)

Also confirm the module imports: `.venv/bin/python -c "import django, os; os.environ.setdefault('DJANGO_SETTINGS_MODULE','linkedin.django_settings'); django.setup(); import linkedin.realtime.listener"` → exit 0.

- [ ] **Step 5: Commit**

```bash
git add linkedin/realtime/listener.py tests/realtime/test_listener.py
git commit -m "Rewrite realtime listener as a CDP-connecting child process"
```

---

## Task 5: `manage.py listen_realtime` command

The child-process entrypoint. Resolves the daemon's account, then runs `run_listener`.

**Files:**
- Create: `linkedin/management/commands/listen_realtime.py`

This is a thin entrypoint — verification is an import + `--help` check (no live run).

- [ ] **Step 1: Write the implementation**

Create `linkedin/management/commands/listen_realtime.py`:

```python
"""`manage.py listen_realtime` — the realtime listener child process.

Spawned and supervised by the daemon (see linkedin/realtime/supervisor.py).
Resolves the same LinkedIn account the daemon runs as, then connects to the
daemon's browser over CDP and streams inbound messages. Not meant to be run
by hand in normal operation, though it can be for debugging.
"""
from __future__ import annotations

import logging
import sys

from django.core.management.base import BaseCommand, CommandError

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Run the realtime inbound-message listener (child process of the daemon)."

    def handle(self, *args, **opts):
        from linkedin.conf import get_daemon_handle
        from linkedin.models import LinkedInProfile
        from linkedin.operators import resolve_operator
        from linkedin.realtime.listener import run_listener

        handle = get_daemon_handle()
        if not handle:
            raise CommandError(
                "No daemon LinkedIn account configured — set LINKEDIN_USERNAME in .env."
            )
        profile = (
            LinkedInProfile.objects.select_related("user")
            .filter(user__username=handle)
            .first()
        )
        if profile is None:
            raise CommandError(f"No LinkedInProfile for handle {handle!r}.")

        username = profile.linkedin_username
        operator = resolve_operator(username)
        logger.info("listen_realtime: starting for operator=%s (%s)", operator, username)
        code = run_listener(operator=operator, username=username)
        sys.exit(code)
```

- [ ] **Step 2: Verify the command registers**

Run: `.venv/bin/python manage.py listen_realtime --help`
Expected: prints the command's help text including "Run the realtime inbound-message listener", exit 0. (Do **not** run it without `--help` — that would start a live listener.)

- [ ] **Step 3: Commit**

```bash
git add linkedin/management/commands/listen_realtime.py
git commit -m "Add manage.py listen_realtime child-process entrypoint"
```

---

## Task 6: Listener supervisor

The daemon spawns + supervises the listener child process. `ListenerSupervisor` is plain process management — fully unit-testable with `subprocess` mocked.

**Files:**
- Create: `linkedin/realtime/supervisor.py`
- Test: `tests/realtime/test_supervisor.py`

- [ ] **Step 1: Write the failing test**

Create `tests/realtime/test_supervisor.py`:

```python
"""Tests for the realtime listener supervisor."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from linkedin.realtime.supervisor import ListenerSupervisor


def _fake_proc(alive=True):
    proc = MagicMock()
    proc.poll.return_value = None if alive else 1
    return proc


def test_ensure_running_spawns_when_no_process():
    sup = ListenerSupervisor()
    with patch("linkedin.realtime.supervisor.subprocess.Popen", return_value=_fake_proc()) as popen:
        sup.ensure_running()
    popen.assert_called_once()


def test_ensure_running_is_noop_when_process_alive():
    sup = ListenerSupervisor()
    with patch("linkedin.realtime.supervisor.subprocess.Popen", return_value=_fake_proc(alive=True)) as popen:
        sup.ensure_running()   # spawns
        sup.ensure_running()   # alive → no second spawn
    popen.assert_called_once()


def test_ensure_running_respawns_after_death():
    sup = ListenerSupervisor()
    dead, alive = _fake_proc(alive=False), _fake_proc(alive=True)
    with patch("linkedin.realtime.supervisor.subprocess.Popen", side_effect=[dead, alive]) as popen:
        sup.ensure_running()   # spawn #1 (dead)
        sup.ensure_running()   # detects death → spawn #2
    assert popen.call_count == 2


def test_ensure_running_gives_up_after_max_failures():
    sup = ListenerSupervisor()
    with patch("linkedin.realtime.supervisor.subprocess.Popen",
               side_effect=OSError("cannot spawn")) as popen:
        for _ in range(ListenerSupervisor.MAX_SPAWN_FAILURES + 5):
            sup.ensure_running()
    assert popen.call_count == ListenerSupervisor.MAX_SPAWN_FAILURES


def test_stop_terminates_a_running_process():
    sup = ListenerSupervisor()
    proc = _fake_proc(alive=True)
    with patch("linkedin.realtime.supervisor.subprocess.Popen", return_value=proc):
        sup.ensure_running()
    sup.stop()
    proc.terminate.assert_called_once()


def test_stop_is_noop_when_nothing_running():
    sup = ListenerSupervisor()
    sup.stop()  # must not raise


def test_stop_resets_failure_count_so_ensure_can_spawn_again():
    """After off-hours stop(), the next active period can spawn afresh even
    if earlier spawns had failed."""
    sup = ListenerSupervisor()
    with patch("linkedin.realtime.supervisor.subprocess.Popen",
               side_effect=OSError("boom")):
        for _ in range(ListenerSupervisor.MAX_SPAWN_FAILURES + 2):
            sup.ensure_running()
    sup.stop()
    with patch("linkedin.realtime.supervisor.subprocess.Popen", return_value=_fake_proc()) as popen:
        sup.ensure_running()
    popen.assert_called_once()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/realtime/test_supervisor.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'linkedin.realtime.supervisor'`

- [ ] **Step 3: Write the implementation**

Create `linkedin/realtime/supervisor.py`:

```python
"""Supervises the realtime listener child process from inside the daemon.

The daemon owns the browser; the listener (`manage.py listen_realtime`)
runs as a child process that shares it over CDP. The supervisor spawns the
child, restarts it if it dies, gives up after repeated spawn failures
(degrading to polling), and kills it when the daemon goes off-hours.

Process management only — no Playwright, no browser. Fully unit-testable.
"""
from __future__ import annotations

import logging
import subprocess
import sys

logger = logging.getLogger(__name__)


class ListenerSupervisor:
    """Owns the lifecycle of the listener child process."""

    # Consecutive spawn failures after which the supervisor stops trying for
    # the rest of the current active period (daemon degrades to polling).
    MAX_SPAWN_FAILURES = 5

    def __init__(self):
        self._proc: subprocess.Popen | None = None
        self._spawn_failures = 0

    def ensure_running(self) -> None:
        """Spawn the listener if it is not currently alive. Idempotent —
        call once per active-hours daemon loop iteration."""
        if self._proc is not None and self._proc.poll() is None:
            return  # alive
        if self._proc is not None:
            logger.warning("Realtime listener process exited (code=%s) — respawning",
                            self._proc.returncode)
            self._proc = None
        if self._spawn_failures >= self.MAX_SPAWN_FAILURES:
            return  # gave up for this active period
        self._spawn()

    def _spawn(self) -> None:
        try:
            self._proc = subprocess.Popen(
                [sys.executable, "manage.py", "listen_realtime"],
            )
            self._spawn_failures = 0
            logger.info("Realtime listener child process spawned (pid=%s)", self._proc.pid)
        except Exception as e:
            self._spawn_failures += 1
            logger.warning(
                "Failed to spawn realtime listener (%d/%d): %s",
                self._spawn_failures, self.MAX_SPAWN_FAILURES, e,
            )
            if self._spawn_failures >= self.MAX_SPAWN_FAILURES:
                logger.error(
                    "Realtime listener spawn gave up — daemon continues without "
                    "realtime (polling still covers inbound messages)."
                )

    def stop(self) -> None:
        """Terminate the listener child if running. Idempotent, never raises.

        Also clears the spawn-failure count so the next active period starts
        fresh (off-hours is a natural reset point)."""
        if self._proc is not None and self._proc.poll() is None:
            try:
                self._proc.terminate()
                logger.info("Realtime listener child process terminated")
            except Exception as e:
                logger.debug("Error terminating realtime listener: %s", e)
        self._proc = None
        self._spawn_failures = 0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/realtime/test_supervisor.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add linkedin/realtime/supervisor.py tests/realtime/test_supervisor.py
git commit -m "Add realtime listener subprocess supervisor"
```

---

## Task 7: Daemon rewiring — revert v1 wiring, add the supervisor

Replace the v1 in-process listener wiring (`ensure_realtime_listener` / `pump` / `stop_realtime_listener`) with the supervisor. The daemon's queue-idle wait reverts to plain `time.sleep`. The `run_startup_catchup` call and the `ENABLE_REALTIME_LISTENER` import stay.

**Files:**
- Modify: `linkedin/daemon.py`

Browser/process wiring — verification is an import check + the daemon regression suite.

- [ ] **Step 1: Replace the `while True:` loop body**

In `linkedin/daemon.py`'s `run_daemon`, the v1 loop currently calls `ensure_realtime_listener` / `stop_realtime_listener` / `listener.pump`. Replace the loop region — from `while True:` through the queue-idle `continue` — with:

```python
    # Realtime listener supervisor — owns the listener child process.
    from linkedin.realtime.supervisor import ListenerSupervisor
    listener_supervisor = ListenerSupervisor()

    while True:
        # Close stale DB connections at the top of every loop iteration.
        # Neon's idle timeout can kill the SSL socket during any sleep.
        connections.close_all()

        pause = seconds_until_active()
        if pause > 0:
            # Off-hours: kill the listener child so the account isn't
            # holding a live LinkedIn realtime connection overnight.
            listener_supervisor.stop()
            h, m = int(pause // 3600), int(pause % 3600 // 60)
            logger.info("Outside active hours — sleeping %dh%02dm", h, m)
            connections.close_all()
            time.sleep(pause)
            continue

        # Active hours: ensure the listener child process is running.
        # No-op when ENABLE_REALTIME_LISTENER is off (the child resolves the
        # flag itself and exits immediately if disabled — see below).
        if ENABLE_REALTIME_LISTENER:
            listener_supervisor.ensure_running()

        task = Task.objects.claim_next(operator=our_operator)
        if task is None:
            wait = Task.objects.seconds_to_next(operator=our_operator)
            if wait is None:
                logger.info("Queue empty — nothing to do")
                listener_supervisor.stop()
                return
            if wait > 0:
                h, m = int(wait // 3600), int(wait % 3600 // 60)
                logger.info("Next task in %dh%02dm — sleeping", h, m)
                connections.close_all()
                time.sleep(wait)
            continue
```

> Notes: the `listener_supervisor = ListenerSupervisor()` line goes just before `while True:`. The off-hours branch calls `stop()`; the active branch calls `ensure_running()` (gated on `ENABLE_REALTIME_LISTENER`). The queue-idle wait is a plain `time.sleep(wait)` again (v1's `listener.pump` is gone). The `Queue empty` early-return also calls `stop()` so the child doesn't outlive the daemon. Everything after this block (task dispatch, handler execution) is unchanged.

- [ ] **Step 2: Confirm the `run_startup_catchup` call and imports are intact**

The `ENABLE_REALTIME_LISTENER` import in the `from linkedin.conf import (...)` block stays. The `run_startup_catchup` block (before `heal_tasks`) stays unchanged. There must be **no remaining reference** to `ensure_realtime_listener`, `stop_realtime_listener`, or `RealtimeListener` anywhere in `daemon.py` — grep to confirm:

Run: `grep -n "ensure_realtime_listener\|stop_realtime_listener\|RealtimeListener\|\.pump(" linkedin/daemon.py`
Expected: no output (all v1 in-process wiring removed).

- [ ] **Step 3: Verify imports + daemon regression suite**

Run: `.venv/bin/python -c "import django, os; os.environ.setdefault('DJANGO_SETTINGS_MODULE','linkedin.django_settings'); django.setup(); import linkedin.daemon"`
Expected: exit 0.

Run: `.venv/bin/pytest tests/test_daemon_resilience.py tests/test_heal.py tests/tasks/ -q --reuse-db`
Expected: PASS (no new failures — the loop edits are gated on `ENABLE_REALTIME_LISTENER`, default off; the supervisor is only constructed, not exercised, when the flag is off).

- [ ] **Step 4: Commit**

```bash
git add linkedin/daemon.py
git commit -m "Wire daemon to supervise the listener child process"
```

---

## Task 8: Documentation

**Files:**
- Modify: `CLAUDE.md`
- Modify: `ARCHITECTURE.md`
- Modify: `docs/superpowers/specs/2026-05-16-realtime-message-listener-design.md` (mark superseded)

- [ ] **Step 1: Update the `CLAUDE.md` realtime bullet**

In `CLAUDE.md`, find the existing **Realtime listener** bullet under "Architecture (quick reference)" and replace it with:

```markdown
- **Realtime listener**: `linkedin/realtime/` — near-realtime inbound LinkedIn DM detection, gated by `ENABLE_REALTIME_LISTENER` (`conf.py`, default off). Runs as a **separate child process** (`manage.py listen_realtime`) that the daemon spawns and supervises (`linkedin/realtime/supervisor.py`). The daemon launches Chromium as a *persistent context* with a fixed `--remote-debugging-port` (`LISTENER_CDP_PORT`); the listener `connect_over_cdp`s to that same browser, shares its one context (one device, one cookie jar), opens a `/messaging/` tab, and streams LinkedIn's realtime feed via CDP `streamResourceContent` → `sse.py` (SSE framing) → `parser.py` → `handler.py` (resolve `Lead`, persist `crm.Message`, Slack-notify inbound). Separate processes = separate Playwright loops, which is why this works where the in-process v1 design did not. The daemon supervises the child (restart on death, kill off-hours); on a daemon browser relaunch the listener reconnects. Heartbeat at `data/listener-heartbeat-<account>.json`; startup catch-up (`catchup.py`) surfaces the off-hours gap. Realtime is an enhancement — any failure degrades to the existing polling.
```

- [ ] **Step 2: Update `ARCHITECTURE.md`**

In `ARCHITECTURE.md`, find the realtime-listener section and rewrite it to describe the v2 architecture: the daemon's persistent-context Chromium + fixed CDP port; the `listen_realtime` child process; `connect_over_cdp` sharing one context (one device — the bot-detection rationale); the `ListenerSupervisor` (spawn/restart/off-hours-kill); the reused pure modules (`sse`, `parser`, `handler`, `lead_lookup`, `heartbeat`, `catchup`); and why a separate process is required (sync-Playwright greenlet corruption when CDP event handlers and the daemon's task loop share one Playwright instance). Note the persistent-context migration: the daemon uses `data/profile-<account>/`; `StandaloneLinkedInSession` stays on the JSON cookie jar. Match the file's existing voice and depth.

- [ ] **Step 3: Mark the v1 spec superseded**

At the top of `docs/superpowers/specs/2026-05-16-realtime-message-listener-design.md`, immediately under the title, add:

```markdown
> **SUPERSEDED (2026-05-16):** the in-process CDP approach in this document
> does not work — see `2026-05-16-realtime-listener-v2-design.md` for the
> implemented design (separate process sharing the browser over CDP). This
> file is kept for history.
```

- [ ] **Step 4: Run the full realtime + touched-module suite**

Run: `.venv/bin/pytest tests/realtime/ tests/test_conf.py -q --reuse-db`
Expected: PASS (all realtime tests + conf tests green).

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md ARCHITECTURE.md docs/superpowers/specs/2026-05-16-realtime-message-listener-design.md
git commit -m "Document realtime listener v2 architecture"
```

---

## Self-review

**Spec coverage:**
- Persistent-context Chromium + fixed CDP port → Task 1 (port), Task 3 (persistent context). ✓
- Listener as a separate child process sharing the browser over CDP → Task 4 (`listener.py`), Task 5 (`listen_realtime` command). ✓
- Daemon spawns + supervises the child (restart on death, off-hours kill) → Task 6 (`supervisor.py`), Task 7 (daemon wiring). ✓
- Reverting v1's in-process daemon wiring (`pump`/`ensure`/`stop`) → Task 7. ✓
- Reused pure modules untouched (`sse`/`parser`/`handler`/`lead_lookup`/`heartbeat`/`catchup`) → stated in Context; no task modifies them. ✓
- Cookie model — one shared context, first-run seeding from the legacy JSON, profile dir per account → Task 2 (`profile_dir_for`), Task 3 (seeding + launch). ✓
- `StandaloneLinkedInSession` stays on JSON cookies → Task 3 Step 2 note; `cookie_store.py` JSON helpers retained. ✓
- Reconnect on daemon browser relaunch → Task 4 (`run_listener` reconnect loop). ✓
- Degradation to polling on listener failure → Task 6 (`MAX_SPAWN_FAILURES`), Task 4 (`_MAX_CONSECUTIVE_FAILURES`). ✓
- Startup catch-up stays in the daemon → Task 7 Step 2 (left intact). ✓
- Config: `LISTENER_CDP_PORT` → Task 1; existing flags untouched. ✓
- Docs → Task 8. ✓

**Placeholder scan:** No TBD/TODO. Browser-code tasks (3, 4 partially, 5) are explicitly marked not-unit-testable with import-check verification and deferred live testing — that is inherent to Playwright code, not a placeholder; the reconnect *control loop* (the testable logic) is fully TDD'd in Task 4.

**Type consistency:** `profile_dir_for(username)` (Task 2) is used in Task 3's `start_browser_session`. `launch_browser(profile_dir, *, cdp_port, seed_cookies)` (Task 3 Step 1) matches its call in `start_browser_session` (Task 3 Step 2). `run_listener(*, operator, username, cdp_port=None)` (Task 4) matches the call in `listen_realtime.py` (Task 5) and the tests (Task 4 Step 1). `ListenerSupervisor` with `ensure_running()` / `stop()` / `MAX_SPAWN_FAILURES` (Task 6) matches the daemon wiring (Task 7) and tests. `_run_one_connection(*, cdp_port, operator, username)` / `_MAX_CONSECUTIVE_FAILURES` / `_RECONNECT_DELAY_SECONDS` / `_HEALTHY_CONNECTION_SECONDS` are consistent between `listener.py` and its tests.

**Note on v1 leftovers:** Task 4 fully replaces `listener.py` (the v1 `RealtimeListener`/`ensure_realtime_listener`/`stop_realtime_listener` cease to exist); Task 7 removes their only caller (`daemon.py`). After Task 7, `grep` confirms no dangling references. The v1 `tests/realtime/test_listener.py` is replaced wholesale in Task 4.
