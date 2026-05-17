# Phone Enrichment — Design

**Date:** 2026-05-17
**Status:** Approved — ready for implementation planning

## Goal

When the realtime listener detects a newly-replied lead, enrich that lead's
mobile phone number via third-party APIs and surface it to Slack. Enrichment
runs as a multi-provider failover waterfall, built so new providers can be
added without touching the orchestrator.

## Decisions locked in

| Decision | Choice | Rationale |
|---|---|---|
| Where enrichment runs | Dedicated worker thread inside the daemon process | Decoupled from outbound work; no event-loop conflict (HTTP only); pauses if the daemon is down (accepted). |
| Provider escalation | Escalate to the next provider **only on API failure** | BetterContact is itself a 20+ provider waterfall; a clean "no result" from it means standalone tools will also likely fail. Failover chain, not a coverage chain. |
| Provider order | BetterContact → LeadMagic → Prospeo | BetterContact = best waterfall coverage. LeadMagic = cheaper, synchronous, LinkedIn-URL native. Prospeo = last resort. |
| Re-enrichment | Never re-enrich a lead with a confirmed result | One attempt per lead. `phone_enriched_at` set = done forever. |
| Slack delivery | Listener notification unchanged; enrichment posts its **own separate** message | Listener reply alert stays instant and independent. Enrichment message is purely additive. |
| Provider abstraction | Protocol + explicit ordered list (Approach A) | Open-closed: new provider = new file implementing the protocol + one line in the chain list. Matches codebase style. |

## Architecture & data flow

```
inbound reply
  │
  ▼
listener handler._handle (INBOUND branch)
  ├─ notify_message_received()        ← unchanged, fires immediately
  └─ if ENABLE_PHONE_ENRICHMENT and lead.phone_enriched_at is None
        and not lead.disqualified:
        enqueue Task(enrich_phone)     ← new; one INSERT, non-blocking
                                          │
        ┌─────────────────────────────────┘
        ▼
EnrichmentWorker thread (daemon process, separate loop)
  └─ claim enrich_phone task → run_waterfall() → write Lead.phone/.phone_enriched_at
        → notify_phone_enriched()      ← separate Slack webhook message
        → mark task completed
```

The worker is a `threading.Thread` the daemon spawns alongside the listener
supervisor. It runs its own claim/run loop against the `Task` table,
independent of the outbound task loop.

## Components

### Schema (one migration)

Add to `crm.Lead`:
- `phone` — `CharField(max_length=32, blank=True, default="")`
- `phone_enriched_at` — `DateTimeField(null=True, blank=True)`

`phone_enriched_at` set = "attempted with a confirmed result, never re-enrich".

### Task type

New `Task.TaskType.ENRICH_PHONE`. Payload:
`{lead_id, operator, bettercontact_request_id}`. The `bettercontact_request_id`
is written back after a successful BetterContact submit so a daemon restart
resumes polling the same request instead of re-submitting (and re-billing).

### `linkedin/enrichment/` package (Approach A)

- `base.py` — `PhoneProvider` protocol (`name`, `enrich(lead) -> EnrichmentResult`);
  `EnrichmentResult` dataclass (`phone: str | None`,
  `status: FOUND | NOT_FOUND | API_FAILURE`, `provider: str`, `raw: dict`).
- `http.py` — small `urllib`-based POST/GET-JSON helper with timeout. No new
  dependency — matches `linkedin/notifications/slack.py`.
- `providers/bettercontact.py` — `BetterContactProvider`. Async: POST submit →
  poll `GET /async/{request_id}` until `terminated` or `ENRICHMENT_MAX_DURATION_SECONDS`.
- `providers/leadmagic.py` — `LeadMagicProvider`. Synchronous
  `POST /v1/people/mobile-finder`.
- `providers/prospeo.py` — `ProspeoProvider`. Synchronous `POST /mobile-finder`.
- `waterfall.py` — `PROVIDER_CHAIN = [BetterContact(), LeadMagic(), Prospeo()]`
  and `run_waterfall(lead, task) -> EnrichmentResult`: iterate the chain;
  `FOUND` or `NOT_FOUND` → return immediately; `API_FAILURE` → try next
  provider; all failed → return the last `API_FAILURE` result.
- `worker.py` — `EnrichmentWorker` thread: claim/run/complete loop, owns its
  DB-connection lifecycle (Django connections are thread-local — `close_all()`
  per iteration), reclaims stale `RUNNING` `enrich_phone` tasks on startup
  (crash recovery), `start()`/`stop()`.

### `linkedin/tasks/enrich_phone.py`

`handle_enrich_phone(task)` — note: **no `session` argument**; this is not a
daemon-loop handler, it runs in the enrichment thread. Load the lead, skip if
already enriched or disqualified, run the waterfall, write `Lead.phone` /
`phone_enriched_at`, post the Slack message.

### `daemon.py` wiring

- Spawn `EnrichmentWorker` when `ENABLE_PHONE_ENRICHMENT`; `worker.stop()` on
  daemon exit.
- `Task.claim_next` and `Task.seconds_to_next` **exclude `ENRICH_PHONE`** so the
  outbound loop never claims an enrichment task and never sleeps waiting on one.
- The "queue empty → return" daemon-exit path is guarded so the daemon does not
  exit while the enrichment worker has outstanding `enrich_phone` tasks.

### Slack

New `notify_phone_enriched(lead, result)` in `linkedin/notifications/slack.py`
— a webhook Block Kit message: lead name, company, profile link, phone (or
"no number found"), and which provider produced the hit. No-op when
`SLACK_WEBHOOK_URL` is unset, consistent with the other notify functions.

### Config

`conf.py`:
- `ENABLE_PHONE_ENRICHMENT` — `bool`, default `False` (project convention for
  new features).
- `ENRICHMENT_MAX_DURATION_SECONDS` — default `600`.
- `ENRICHMENT_HTTP_TIMEOUT_SECONDS` — default `5`.
- `BETTERCONTACT_POLL_INTERVAL_SECONDS` — default `15`.

`.env`: `BETTERCONTACT_API_KEY`, `LEADMAGIC_API_KEY`, `PROSPEO_API_KEY`.

New `EnrichmentError` exception in `linkedin/exceptions.py`.

## Error handling

Per the project rule (crash on unexpected errors; `try/except` only for
expected, recoverable cases):

- Provider API failures (network error, 5xx, timeout, auth, rate-limit) are
  **expected** — caught and converted to `EnrichmentResult(status=API_FAILURE)`,
  which drives failover. `EnrichmentError` is raised for malformed provider
  responses.
- The worker loop wraps each task in `try/except` (same pattern as
  `linkedin/realtime/handler.py`) — a bad task is marked `FAILED` and
  `notify_error`-reported; it never unwinds the worker thread.
- BetterContact failing to reach `terminated` within
  `ENRICHMENT_MAX_DURATION_SECONDS` is treated as `API_FAILURE` → failover.

### Outcome → persistence rules

| Waterfall outcome | `Lead.phone` | `phone_enriched_at` | Task status |
|---|---|---|---|
| A provider returns `FOUND` | written | stamped | `completed` |
| A provider returns clean `NOT_FOUND` | empty | stamped | `completed` |
| **Every** provider returns `API_FAILURE` | empty | **not stamped** | `failed` |

The all-errored case deliberately does not stamp `phone_enriched_at`, so the
lead's next inbound reply naturally re-attempts enrichment. "Never re-enrich"
applies to a confirmed empty result, not to a run where every provider errored.

## Testing

pytest under `tests/`, mirroring the existing layout:

- Each provider with mocked `urllib`: `FOUND` / `NOT_FOUND` / `API_FAILURE`
  response shapes, including BetterContact submit→poll→terminate and the
  poll-timeout path.
- `run_waterfall` escalation logic: `FOUND` stops; `NOT_FOUND` stops without
  escalating; `API_FAILURE` escalates; all-failed returns the final result.
- `handle_enrich_phone`: writes `Lead.phone`, stamps `phone_enriched_at`, skips
  already-enriched and disqualified leads, applies the outcome→persistence
  rules above.
- `EnrichmentWorker`: claim/run/complete; stale-`RUNNING`-task reclaim.
- Listener handler: enqueues an `enrich_phone` task, gated by
  `ENABLE_PHONE_ENRICHMENT` and `lead.phone_enriched_at`.

## Documentation

Update `CLAUDE.md` (new `enrich_phone` task type, `linkedin/enrichment/`
module, new config keys) and `ARCHITECTURE.md` per the project's docs-sync
rule.

## Out of scope

- Email enrichment (the providers also find emails — not part of this feature).
- Threaded Slack replies / Slack Web API migration (rejected: keeps the
  webhook-only integration).
- Surviving full daemon downtime (rejected: worker pauses with the daemon).
- A standalone OS-managed enrichment service.
