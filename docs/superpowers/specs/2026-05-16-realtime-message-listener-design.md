# Realtime Inbound Message Listener — Design

> **SUPERSEDED (2026-05-16):** the in-process CDP approach in this document
> does not work — see `2026-05-16-realtime-listener-v2-design.md` for the
> implemented design (separate process sharing the browser over CDP). This
> file is kept for history.

**Date:** 2026-05-16
**Branch:** `realtime-message-listener`
**Status:** Design — pending implementation plan

## Problem

Inbound LinkedIn messages are only detected by polling: the daemon-internal
`sweep_connections` task (every ~2h, and only for *freshly accepted*
connections) and the operator-run `backfill_messages` management command (run
manually, or via a crontab the operator chooses to set up). There is no
near-realtime path. The daemon's main loop is single-threaded
and idles in a dead `time.sleep()` between tasks — deaf to anything during the
idle window.

Goal: detect an inbound message **seconds** after it arrives, while the daemon
is running, without a second process, a second browser, or a duplicated cookie
jar.

## Scope

In scope:
- Detect inbound LinkedIn DMs in near-realtime during the daemon's active hours.
- On detection: persist the message to `crm.Message`; send a Slack notification.
- Close the gap left by active-hours-only listening with a **daemon-startup
  catch-up**: surface how long the listener has been off and offer to run
  `backfill_messages` before the task loop starts.

Explicitly out of scope:
- No follow-up / reply-handling task is enqueued. The daemon only runs
  follow-ups for the *connected-no-reply* cohort and connection requests;
  received messages are recorded and surfaced to the human, not auto-actioned.
- No overnight (off-hours) realtime coverage by the listener itself — the
  off-hours gap is closed by the startup catch-up (above), not by the listener.
- No mid-run catch-up when active hours resume each morning — the same gap
  technically reopens then, but there is no operator at the terminal to prompt.
  Noted as a possible later enhancement; v1 handles startup only.
- Outbound task execution (`connect`, `follow_up`, `sweep_connections`) is
  unchanged.

## Chosen approach: CDP `eventSourceMessageReceived`

LinkedIn's own web client opens a Server-Sent Events connection to
`https://www.linkedin.com/realtime/connect` whenever the messaging UI is
loaded. The Chrome DevTools Protocol emits a `Network.eventSourceMessageReceived`
event for **every** message on that stream.

The listener loads `/messaging/` in a second tab and *observes* that CDP event
stream. We open nothing LinkedIn's own client wouldn't — no injected script, no
second connection. This is the most browser-native option and inherits the
exact TLS fingerprint, headers, cookies, and IP of the daemon's existing
session because it is the **same browser context**.

Alternatives considered and rejected:
- **Injected `EventSource`** — we inject our own JS `EventSource` +
  `page.expose_function()`. More code, a second realtime connection, less
  native. Kept only as a fallback if the CDP payloads turn out unusable.
- **DOM `MutationObserver`** on the conversation list — no protocol work, but
  fragile to LinkedIn UI changes. Rejected.

## Architecture

One process, two tabs, no threads.

### Components

1. **Listener tab** — a second `page` opened in the daemon's *existing*
   `AccountSession.context` (shared cookies/fingerprint), navigated to
   `/messaging/`. A CDP session is attached via
   `context.new_cdp_session(listener_page)`, the `Network` domain enabled, and
   a handler subscribed to `eventSourceMessageReceived`.

2. **Event parser** — pure function: takes a raw `eventSourceMessageReceived`
   payload and returns a structured result (sender identifier, message text,
   timestamp, conversation URN, inbound/outbound direction) or `None` for
   events that are not inbound messages (presence heartbeats, typing
   indicators, outbound echoes, read receipts). This is the only unit with
   meaningful test coverage.

3. **Event handler** — on a parsed *inbound* message: resolve the `Lead` by
   sender, persist to `crm.Message` (reusing the existing
   `linkedin/db/messages.py` persist path, idempotent on
   `(source, external_id)`), and send a Slack notification (a new
   `notify_message_received` alongside the existing `notify_connection_accepted`
   in `linkedin/notifications/slack.py`). Wrapped in try/except — a bad event
   logs + Slack-error-notifies but never unwinds the daemon. An unresolved
   sender (no matching Lead) is logged and skipped.

4. **Daemon-loop change** — minimal. The listener runs entirely inside CDP
   callbacks, which fire whenever Python is parked inside *any* Playwright call.
   The listener enqueues no tasks, so the daemon never needs to "wake early."
   The only change: replace the idle `time.sleep(wait)` (currently
   `daemon.py` ~line 452, the queue-idle wait) with a **chunked
   Playwright-pumping wait** — a helper that loops `wait_for_timeout` on the
   listener page in short slices (~30s) until `wait` elapses. This keeps the
   Playwright event loop pumped so CDP callbacks fire promptly. The off-hours
   `time.sleep(pause)` itself stays a plain sleep — but a listener-tab close
   (before the sleep) and reopen (after) are added around it; see Lifecycle.

   Bonus: because CDP callbacks also fire during `connect`/`follow_up` task
   execution (those are Playwright-busy too), the listener keeps working
   *during* tasks for free. The only latency gaps are the human-pacing
   `time.sleep()` calls inside task handlers — events buffer there and flush a
   few seconds later. Acceptable.

5. **Listener heartbeat** — while the listener is up, the chunked pumping wait
   writes a timestamp to a small JSON file in `data/` (e.g.
   `data/listener_heartbeat.json`), refreshed every ~30s slice. This is the
   single source of truth for "when was the listener last alive." The `data/`
   file matches the existing cookie-store pattern and stays decoupled from the
   DB. (Per-account if the daemon runs multiple accounts — keyed like the
   cookie files.)

6. **Startup catch-up** — before the daemon enters its task loop, it reads the
   heartbeat file and computes `gap = now − last_heartbeat`. The gap naturally
   covers both off-hours and any time the process was stopped — exactly the
   window the listener missed. If `gap` exceeds a threshold (~30 min):
   - **Interactive (TTY):** prompt — `Listener was off ~Xh. Run backfill_messages
     to catch up first? [y/N]`. On `y`, run `backfill_messages` inline via
     Django `call_command` and then continue into the task loop; on `n` (or
     timeout/default), continue straight into the loop.
   - **Headless (no TTY):** skip the prompt; log a WARNING stating the gap and
     that `backfill_messages` should be run to catch up. The daemon does not
     auto-run a long LinkedIn session unattended.
   Below the threshold (e.g. a quick restart), no prompt — just proceed.

   **Account scoping (required).** `backfill_messages` today iterates *every*
   env-configured account (`ACCOUNTS = [("primary", LINKEDIN_USERNAME…),
   ("backfill", BACKFILL_LINKEDIN_USERNAME…)]`) — there is no account selector.
   The daemon runs as the `LINKEDIN_USERNAME` / `"primary"` account, and the
   catch-up must only resync *that* account's threads; it must not spin up a
   separate login + pass for the unrelated `BACKFILL_LINKEDIN_USERNAME` account.
   So this work adds an `--account <label>` option to `backfill_messages`
   (restricting the run to one entry of `ACCOUNTS`), and the catch-up invokes
   `call_command("backfill_messages", account="primary")`. With no flag,
   `backfill_messages` keeps its current all-accounts behavior.

   `backfill_messages` also has its own interactive prereq-staleness gate
   (`_run_prereq_gate_for_accounts`, on `import_connections` freshness).
   Invoked from the catch-up that gate would double-prompt; the catch-up
   suppresses it (a `--skip-prereq-gate` flag, or running it non-interactively).

### Lifecycle

- **Startup** — before the task loop, the startup catch-up (component 6) runs:
  read heartbeat → compute gap → prompt / log as appropriate.
- **Open** — `start_browser_session` / `ensure_browser` opens and wires the
  listener tab (navigate `/messaging/`, attach CDP, subscribe) after the main
  tab is ready.
- **Crash recovery** — the existing `ensure_browser` recovery path
  re-establishes the listener tab when the browser is relaunched.
- **Off-hours** — active-hours-only. When the daemon enters its off-hours
  sleep, the listener tab is **closed** (not merely unpumped) so the account
  does not hold a live LinkedIn realtime connection overnight — that "present
  24/7, no daily rhythm" pattern is a mild bot signal we choose to avoid. The
  tab is reopened when active hours resume. The off-hours gap is reconciled by
  the next startup catch-up, not by the listener.
- **Connection drops** — LinkedIn's own page JS auto-reconnects the
  `EventSource`; if the tab itself dies, crash recovery reopens it.

## Data flow

```
LinkedIn realtime SSE  →  Chrome (listener tab)  →  CDP eventSourceMessageReceived
  →  event parser  →  [inbound message?]  →  persist crm.Message  →  Slack notify
```

## Error handling

- CDP callback exceptions: caught, logged, Slack-error-notified (reusing the
  existing deduped error-notification path) — never unwind the daemon loop.
- Unresolvable sender (no Lead match): logged at WARNING, skipped.
- Persist is idempotent on `(source, external_id)` — a message also picked up
  later by `backfill_messages` will not double-insert.
- Listener-tab open/wire failure during `ensure_browser`: logged; the daemon
  continues without realtime (degrades to the existing polling). Realtime is an
  enhancement, never a hard dependency.

## Testing

- **Unit** — the event parser, against captured sample
  `eventSourceMessageReceived` payloads: inbound message → correct structured
  result; presence/typing/read-receipt/outbound-echo events → `None`.
- **Unit** — the event handler with persist + Slack mocked: parsed inbound →
  one `crm.Message` row + one Slack call; duplicate event → idempotent (one
  row); unresolved sender → skipped, no crash.
- **Unit** — startup catch-up gap logic: missing heartbeat file → treated as a
  large gap; gap below threshold → no prompt; gap above threshold → prompt
  (interactive) / WARNING log (headless). Heartbeat read/write is a pure
  file-IO unit, mockable.
- **Manual** — browser/CDP integration (listener tab opens, CDP subscribes,
  a real test message round-trips). Not unit-testable.

## Open research item (for the implementation plan)

The exact shape of the LinkedIn realtime event payload — the event envelope
and where sender / text / conversation-URN / timestamp live, and how to
distinguish an inbound message from presence/typing/read-receipt/outbound-echo
events. This must be reverse-engineered (capture live `eventSourceMessageReceived`
payloads) before the parser can be written. Approach B (injected `EventSource`)
is the fallback if the CDP payloads turn out unusable.

## Config

A feature flag — `ENABLE_REALTIME_LISTENER` in `conf.py`, default off — so the
listener can be disabled without code changes if it misbehaves, mirroring the
existing `ENABLE_*` gates (`ENABLE_SWEEP_CONNECTIONS`, `ENABLE_FOLLOW_UP`).
