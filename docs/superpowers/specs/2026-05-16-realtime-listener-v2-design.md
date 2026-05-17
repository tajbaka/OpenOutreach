# Realtime Inbound Message Listener — v2 Design (CDP-shared browser)

**Date:** 2026-05-16
**Branch:** `realtime-message-listener`
**Status:** Design — approved, pending implementation plan
**Supersedes:** the transport approach in `2026-05-16-realtime-message-listener-design.md`

## Why v2

The v1 design ran the listener as a second tab inside the daemon process and
observed LinkedIn's realtime stream via CDP. A live daemon run proved this
**fundamentally broken**: the listener's CDP event handlers (and the
`Network.streamResourceContent` call inside `_on_response`) re-enter
Playwright's **sync** API while the daemon's task loop is also driving sync
Playwright. Playwright's sync greenlet model does not support that mixing —
the result was `playwright._impl._errors.Error: ... Sync API inside the
asyncio loop`, closed pages, and every follow-up/connect/sweep failing. The
in-process design cannot work.

v1 also already corrected the *transport*: LinkedIn's realtime feed is a
streaming `fetch()` (`/realtime/connect`, `text/event-stream`), captured via
CDP `Network.streamResourceContent` + `Network.dataReceived` — **not** the
native-`EventSource` `eventSourceMessageReceived` event. v2 keeps that.

## Chosen approach: shared browser over CDP, separate process

A feasibility spike (two real OS processes, one Chromium) confirmed:

- The daemon launches Chromium as a **persistent context** with a CDP TCP
  port. A second process `connect_over_cdp`s to it, sees the **same context
  and cookies**, opens its own tab, and attaches a CDP session — while the
  first process keeps driving Playwright with **zero interference**.
- A plain `launch()` + `new_context()` context is **invisible** to a
  CDP-connecting process (it saw `cookies=[]`). A **persistent context** is
  the browser's default context and *is* visible. Persistent context is
  therefore mandatory.
- `connect_over_cdp(...).close()` only **disconnects** the client; it does
  not kill the browser. The listener process exiting never harms the daemon.

So v2: **one browser, one persistent context, one device fingerprint, one
cookie jar — driven by two separate OS processes.** The daemon owns the
browser; the listener is a child process that shares it over CDP. Because
the processes are separate, each has its own Playwright/asyncio loop and the
greenlet corruption is structurally impossible — the listener process is
exactly the (working) capture-spike topology.

### Why this is also detection-safe

LinkedIn sees one browser, one context → one device (`bcookie`), one TLS +
JS fingerprint, with a feed tab and a messaging tab open. Identical to any
normal multi-tab user. No second login, no second device, no copied token,
no fingerprint/device-id inconsistency. The realtime observation is the
page's own connection. Net new detection surface: ~zero.

## Architecture

One persistent-context Chromium. Two processes.

### The daemon process (`make run`)

1. Launches Chromium via `launch_persistent_context(profile_dir,
   args=["--remote-debugging-port=<LISTENER_CDP_PORT>"])` — a per-account
   on-disk profile dir, plus a **fixed** CDP port.
2. Logs in / validates session as today (feed-bounce check).
3. Once the browser is up, **spawns the listener as a child process** and
   **supervises** it: during active hours, ensures the child is alive
   (respawn on death, capped retries); on entering off-hours, kills the
   child; respawns when active hours resume.
4. Runs its task loop unchanged — idle wait reverts to plain `time.sleep`.

### The listener process (`manage.py listen_realtime`)

A standalone Django process with its own Playwright. It:

1. `connect_over_cdp("http://localhost:<LISTENER_CDP_PORT>")`, with
   retry/backoff (the daemon's browser may briefly be down — e.g. a crash
   relaunch).
2. Takes the shared persistent context (`browser.contexts[0]`), opens its
   own page on `/messaging/`, attaches a CDP session, enables `Network`.
3. Watches `Network.requestWillBeSent` for `/realtime/connect` →
   `Network.responseReceived` → `Network.streamResourceContent` →
   `Network.dataReceived` chunks → SSE buffer → parser → handler.
4. Loops `page.wait_for_timeout` in slices forever — keeps its Playwright
   event loop pumped and refreshes the heartbeat file each slice. This is
   the proven capture-spike topology (listener alone in a process).
5. Detects a dropped CDP connection (daemon relaunched its browser, or the
   listener tab died) and reconnects — same fixed port, so reconnect is a
   retry loop.

### Components

**Reused unchanged** (already built, reviewed, tested — process-agnostic):
`linkedin/realtime/sse.py`, `parser.py`, `handler.py`, `lead_lookup.py`,
`heartbeat.py`, `catchup.py`.

**Rewritten:** `linkedin/realtime/listener.py` — becomes the child-process
listener: connect-over-CDP, shared-context tab, CDP stream wiring (the
`streamResourceContent`/`dataReceived`/SSE/`_dispatch` logic carries over
from the spike-proven code), plus the connect/reconnect retry loop and the
forever pump-loop. No longer opens a tab inside the daemon.

**New:** `manage.py listen_realtime` management command — the listener
process entrypoint (`django.setup()` + run the listener).

**New:** a listener supervisor in `linkedin/daemon.py` —
`subprocess.Popen` spawn, process-liveness check, capped-retry restart,
off-hours kill. Replaces v1's `ensure_realtime_listener` / `pump` /
`stop_realtime_listener` daemon-loop wiring.

**Reverted:** v1's Task 11 daemon-loop changes — the `RealtimeListener.pump`
idle wait, the off-hours `stop_realtime_listener`, the `ensure_realtime_listener`
call. The daemon's queue-idle wait goes back to plain `time.sleep`.

## Cookie model

There is exactly **one** session: the daemon's persistent context, stored in
the profile dir on disk. The listener shares that live context over CDP — it
does not have, copy, or maintain any cookies of its own. When LinkedIn
rotates `li_at`, it rotates in the one shared context and both processes see
it immediately. The earlier "separate cookie file / heartbeat self-heal /
three layers" analysis applied to a separate-*browser* model; the shared-
context model dissolves it — staleness is whatever the daemon has, always
current.

The only session-related event the listener handles is the daemon
**relaunching** its browser (crash recovery): the CDP connection drops, and
the listener reconnects (retry loop on the fixed port).

## Persistent-context migration (the main blast radius)

The daemon switches from `launch()` + JSON `storage_state`
(`linkedin/browser/cookie_store.py`) to `launch_persistent_context` with an
on-disk profile dir. Scope and rules:

- **Daemon only.** `StandaloneLinkedInSession` (backfill / sales-nav /
  import_connections) keeps `launch()` + JSON cookies — it never needs CDP
  sharing. Two cookie mechanisms coexist; that is acceptable.
- **Profile dir:** `data/profile-<safe_username>/`, safe-name derived the
  same way `cookie_store.cookie_path_for` derives the JSON filename.
- **First-run seeding:** if the profile dir does not exist yet but an old
  `data/cookies-<safe_username>.json` does, launch the persistent context
  and `context.add_cookies(...)` from that JSON before validating — so the
  cutover does not force a re-login.
- **Affected code:** `linkedin/browser/login.py` (`launch_browser`,
  `start_browser_session`, `_cookies_still_valid`), `linkedin/browser/
  session.py` (`AccountSession.ensure_browser`, `_maybe_refresh_cookies`,
  `close`). The proactive `li_at` expiry-peek (`_maybe_refresh_cookies`) is
  dropped — a persistent context has no JSON to peek; the existing
  `/feed/`-bounce validation at startup is the staleness check, and the
  profile dir self-persists cookies (no manual save).
- Crash recovery (`ensure_browser` → relaunch) re-opens the same persistent
  context (same profile dir, same port).

## Configuration

- `LISTENER_CDP_PORT` (`conf.py`, env-overridable, default e.g. `9222`) —
  the fixed CDP port the daemon exposes and the listener connects to.
- `ENABLE_REALTIME_LISTENER` (existing, default off) — gates the daemon
  spawning the child process at all.
- `LISTENER_CATCHUP_GAP_MINUTES`, `LISTENER_PUMP_SLICE_SECONDS` (existing).
- The daemon's startup catch-up (`run_startup_catchup`) stays in the daemon,
  called once before the task loop — it reads the heartbeat the listener
  process wrote on its last run. Unchanged.

## Error handling

- Listener child fails to spawn / crashes repeatedly → daemon logs, retries
  spawn up to a cap, then continues **without** realtime (degrades to the
  existing polling — sweep / `backfill_messages`). Realtime is an
  enhancement, never a hard dependency.
- Listener loses its CDP connection → reconnect loop; if it cannot reconnect
  within a cap, the process exits non-zero and the daemon's supervisor
  respawns it.
- A bad realtime event → `handle_realtime_event` is already try/except
  wrapped (logs, Slack-error-notifies deduped, drops the event).
- Persistent-context launch failure → same degradation path as any browser
  launch failure today.

## Testing

- **Unit (carried over, already green):** `sse`, `parser`, `handler`,
  `lead_lookup`, `heartbeat`, `catchup`.
- **Unit (new):** the supervisor logic (spawn / liveness / capped restart /
  off-hours kill) with `subprocess` mocked; the listener's connect/reconnect
  retry logic with `connect_over_cdp` mocked.
- **Manual integration (deferred — operator-run):** the real two-process
  topology against LinkedIn — daemon launches the persistent context +
  port, spawns the listener, an inbound DM round-trips to a `crm.Message`
  row + Slack notification, and the daemon's own follow-up/connect/sweep
  tasks all still succeed (the v1 failure mode is gone).

## Out of scope

- Migrating `StandaloneLinkedInSession` to persistent context.
- Off-hours realtime coverage (still closed off-hours; the startup catch-up
  reconciles the gap, unchanged).
- Any change to what the daemon does with a detected message (persist +
  Slack-notify only; no auto-reply).
