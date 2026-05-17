# Phone Enrichment — Design

**Date:** 2026-05-17
**Status:** Approved — amended 2026-05-17 after a five-agent verification pass;
ready for implementation planning.

> **Amendment note (2026-05-17):** Parallel verification against the live
> codebase and provider APIs found four factual errors and a set of gaps in
> the original draft. They are corrected inline below; the substantive
> changes are summarized in **Corrections** at the end of the document.

## Goal

When the realtime listener detects a newly-replied lead, enrich that lead's
mobile phone number via third-party APIs and surface it to Slack. Enrichment
runs as a multi-provider failover waterfall, built so new providers can be
added without touching the orchestrator.

## Decisions locked in

| Decision | Choice | Rationale |
|---|---|---|
| Where enrichment runs | Dedicated worker thread inside the daemon process | Decoupled from outbound work; no event-loop conflict (HTTP only); pauses if the daemon is down (accepted). **Not** gated on active hours — it is HTTP-only with no LinkedIn detection risk, so it runs whenever the daemon is up. |
| Provider escalation | Escalate to the next provider **only on API failure** | BetterContact is itself a 20+ provider waterfall; a clean "no result" from it means standalone tools will also likely fail. Failover chain, not a coverage chain. |
| Provider order | BetterContact → LeadMagic → Prospeo | BetterContact = best waterfall coverage, and its `NOT_FOUND` is authoritative (it already tried 20+ sources), which is what makes "`NOT_FOUND` ends the chain" safe. LeadMagic = cheaper, synchronous, LinkedIn-URL native. Prospeo = last resort. |
| BetterContact missing required fields | Treat as `API_FAILURE` → fail over to LeadMagic | BetterContact's submit needs first **and last** name + company (LinkedIn URL is only an optional hint). A `crm.Lead` is only guaranteed `first_name` + `linkedin_url`. When `last_name`/`company_name` are absent the provider returns `API_FAILURE` (not a crash, not `NOT_FOUND`) so failover still fires. In practice a lead that has *replied* was already profile-scraped, so this is a rare edge case. |
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
        and not lead.disqualified
        and no PENDING/RUNNING enrich_phone task already exists for this lead:
        enqueue Task(enrich_phone)     ← new; one INSERT, non-blocking
                                          │
        ┌─────────────────────────────────┘
        ▼
EnrichmentWorker thread (daemon process, separate loop)
  └─ claim enrich_phone task → run_waterfall() → write Lead.phone/.phone_enriched_at
        → notify_phone_enriched()      ← separate Slack webhook message
        → mark task completed
```

The worker is a single `threading.Thread` the daemon spawns alongside the
listener supervisor. It runs its own claim/run loop against the `Task` table,
independent of the outbound task loop.

**Concurrency note.** `Task.objects.claim_next` is a plain ordered read —
there is no `select_for_update` anywhere in this codebase, so "claim" is a
read, not a lock. Safe here **only because there is exactly one enrichment
worker thread**. The worker must never be scaled to multiple threads/processes
without first adding `select_for_update(skip_locked=True)`.

**Enqueue dedup.** The `phone_enriched_at is None` guard does *not* dedup
concurrent enqueues — when two inbound messages arrive from one lead before
the worker runs, `phone_enriched_at` is still `None` for both. The listener
must additionally skip enqueue when a `PENDING`/`RUNNING` `enrich_phone` task
already exists for that lead (DB-level check, mirroring `_enqueue_task` in
`linkedin/tasks/connect.py`). Without it the provider gets billed twice.

## Components

### Schema (two migrations)

**Migration 1 — `crm` app.** Add to `crm.Lead`:
- `phone` — `CharField(max_length=32, blank=True, default="")`
- `phone_enriched_at` — `DateTimeField(null=True, blank=True)`

`phone_enriched_at` set = "attempted with a confirmed result, never re-enrich".

**Migration 2 — `linkedin` app.** `Task.TaskType` is a `TextChoices` enum on a
model in the `linkedin` app; adding `ENRICH_PHONE` is a schema change to
`Task.task_type`'s `choices` and needs its own `AlterField` migration there
(mirrors `linkedin/migrations/0002_alter_task_task_type.py`).

### Task type

New `Task.TaskType.ENRICH_PHONE = "enrich_phone"`. Payload:
`{lead_id, bettercontact_request_id}`. `bettercontact_request_id` is written
back into the payload after a successful BetterContact submit so a daemon
restart resumes polling the same request instead of re-submitting (and
re-billing). `Task.scheduled_at` has **no default** — the enqueue INSERT must
supply it explicitly (`timezone.now()`).

The payload does **not** carry `operator`. In this codebase `operator` is a
LinkedIn-account routing key (the "Travis incident" guard in
`Task.claim_next`); phone enrichment is not account-scoped, so carrying an
operator would be misleading.

### `linkedin/enrichment/` package (Approach A)

- `base.py` — `PhoneProvider` protocol (`name: str`,
  `enrich(lead, task) -> EnrichmentResult`); `EnrichmentResult` dataclass
  (`status: EnrichmentStatus`, `provider: str`, `phone: str | None`,
  `raw: dict`); `EnrichmentStatus` enum (`FOUND`, `NOT_FOUND`, `API_FAILURE`).
- `http.py` — small `urllib`-based JSON POST/GET helper with timeout. No new
  dependency — matches `linkedin/notifications/slack.py`. Auth headers are
  **passed in per call**, not hardcoded: providers use different schemes
  (BetterContact = `api_key` query param; LeadMagic = `X-API-Key` header;
  Prospeo = `X-KEY` header). Transport failures (network error, non-2xx,
  timeout, non-JSON body) raise `HttpError`, which providers catch and convert
  to `API_FAILURE`.
- `providers/bettercontact.py` — `BetterContactProvider`. Async: `POST` submit
  → poll until `terminated` or `ENRICHMENT_MAX_DURATION_SECONDS`. Submit body
  carries `first_name`, `last_name`, `company` (+ `linkedin_url` as a hint) and
  must set `enrich_phone_number: true`. The submit response's request id is the
  `id` field. When the lead lacks `last_name` or `company_name`, the provider
  returns `API_FAILURE` without calling the API.
- `providers/leadmagic.py` — `LeadMagicProvider`. Synchronous
  `POST /mobile-finder` with the `X-API-Key` header and a `profile_url` body.
- `providers/prospeo.py` — `ProspeoProvider`. Synchronous
  `POST https://api.prospeo.io/enrich-person` with the `X-KEY` header and a
  `{"only_verified_mobile": true, "data": {"linkedin_url": ...}}` body; the
  mobile number is read from the `person.mobile.mobile` path of the response.
  (The old `POST /mobile-finder` endpoint named in the original draft was
  retired by Prospeo on 2026-03-01.)
- `waterfall.py` — `PROVIDER_CHAIN = [BetterContactProvider(),
  LeadMagicProvider(), ProspeoProvider()]` and
  `run_waterfall(lead, task) -> EnrichmentResult`: iterate the chain; `FOUND`
  or `NOT_FOUND` → return immediately; `API_FAILURE` → try next provider; all
  failed → return the last `API_FAILURE` result.
- `worker.py` — `EnrichmentWorker`: a single thread with a claim/run/complete
  loop. Owns its DB-connection lifecycle — Django connections are thread-local,
  so the loop calls `connections.close()` (thread-scoped) per iteration, **not**
  `connections.close_all()` (which is process-global and would close the
  daemon main thread's connection too). Reclaims stale `RUNNING` `enrich_phone`
  tasks on `start()`. `start()`/`stop()`.

**Crash safety.** There is no clean-shutdown path in the daemon (no signal
handler, no `atexit`, no `try/finally`). `worker.stop()` only runs on the
daemon's two clean `return` paths; a `SIGTERM` or hard crash abandons the
thread mid-task. The startup stale-`RUNNING`-reclaim is therefore the *real*
crash-recovery mechanism, not a nicety — a task left `RUNNING` by a killed
worker is reset to `PENDING` on the next daemon boot (and the persisted
`bettercontact_request_id` lets it resume rather than re-submit).

### `linkedin/tasks/enrich_phone.py`

`handle_enrich_phone(task)` — note: **no `session` argument**; this is not a
daemon-loop handler, it runs in the enrichment thread. Load the lead, skip if
already enriched or disqualified, run the waterfall, write `Lead.phone` /
`phone_enriched_at`, post the Slack message. Returns the `EnrichmentResult`
(or `None` for a skip) so the worker can set the task's final status.

### `daemon.py` wiring

- Spawn `EnrichmentWorker` when `ENABLE_PHONE_ENRICHMENT`; `worker.stop()` on
  the daemon's clean-exit paths.
- `Task.claim_next` and `Task.seconds_to_next` **exclude `ENRICH_PHONE`** so the
  outbound loop never claims an enrichment task and never sleeps waiting on one.
- `heal_tasks` resets **all** `RUNNING` tasks → `PENDING` unconditionally
  (`daemon.py:192`). That fires on every daemon boot and would yank an
  in-flight enrichment task away from the worker. The reset must therefore also
  exclude `ENRICH_PHONE` — the worker does its own stale-`RUNNING` reclaim at
  `start()` instead. (`heal_tasks` runs before the worker spawns, so excluding
  it there and reclaiming in the worker is the correct ordering.)
- The "queue empty → return" daemon-exit path is guarded so the daemon does not
  exit while the enrichment worker has outstanding `enrich_phone` tasks.

### Slack

New `notify_phone_enriched(*, lead, result)` in
`linkedin/notifications/slack.py` — keyword-only, matching the existing
`notify_*` signatures. A webhook Block Kit message: lead name, company,
profile link, phone (or "no number found"), and which provider produced the
hit. No-op when `SLACK_WEBHOOK_URL` is unset, consistent with the other notify
functions. Only `FOUND`/`NOT_FOUND` reach it — an all-providers-`API_FAILURE`
run posts nothing (the worker just marks the task `failed`).

### Config

`conf.py` — booleans use the project idiom
(`os.getenv(...).strip().lower() in {"1","true","yes","on"}`), not bare
`bool()`:
- `ENABLE_PHONE_ENRICHMENT` — `bool`, default `False` (project convention for
  new features).
- `ENRICHMENT_MAX_DURATION_SECONDS` — default `600`.
- `ENRICHMENT_HTTP_TIMEOUT_SECONDS` — default `5`.
- `BETTERCONTACT_POLL_INTERVAL_SECONDS` — default `15`.

API keys are read into `conf.py` constants (mirroring `LLM_API_KEY`), not read
ad-hoc from `os.getenv` inside provider modules:
`BETTERCONTACT_API_KEY`, `LEADMAGIC_API_KEY`, `PROSPEO_API_KEY` — each from the
matching `.env` var.

New `EnrichmentError` exception in `linkedin/exceptions.py`.

## Error handling

Per the project rule (crash on unexpected errors; `try/except` only for
expected, recoverable cases):

- Provider API failures (network error, 5xx, timeout, auth, rate-limit) are
  **expected** — caught and converted to `EnrichmentResult(status=API_FAILURE)`,
  which drives failover. `EnrichmentError` is raised for malformed provider
  responses (valid JSON, unexpected schema).
- A BetterContact lead missing `last_name`/`company_name` short-circuits to
  `API_FAILURE` before any HTTP call.
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

(`Task.Status` DB values are lowercase — `completed`, `failed`, `running`,
`pending`. Code always references `Task.Status.*`, never raw strings.)

The all-errored case deliberately does not stamp `phone_enriched_at`, so the
lead's next inbound reply naturally re-attempts enrichment. "Never re-enrich"
applies to a confirmed empty result, not to a run where every provider errored.

## Testing

pytest under `tests/`, mirroring the existing layout:

- Each provider with mocked `urllib`: `FOUND` / `NOT_FOUND` / `API_FAILURE`
  response shapes, including BetterContact submit→poll→terminate, the
  poll-timeout path, and the missing-`last_name` short-circuit.
- `run_waterfall` escalation logic: `FOUND` stops; `NOT_FOUND` stops without
  escalating; `API_FAILURE` escalates; all-failed returns the final result.
- `handle_enrich_phone`: writes `Lead.phone`, stamps `phone_enriched_at`, skips
  already-enriched and disqualified leads, applies the outcome→persistence
  rules above.
- `EnrichmentWorker`: claim/run/complete; stale-`RUNNING`-task reclaim.
- Listener handler: enqueues an `enrich_phone` task, gated by
  `ENABLE_PHONE_ENRICHMENT` and `lead.phone_enriched_at`, and **deduped**
  against an existing `PENDING`/`RUNNING` task.

**Slack tests.** `tests/conftest.py` has an autouse `_silence_slack` fixture
that blanks `SLACK_WEBHOOK_URL` everywhere. Tests that exercise a notify
function must assert at the `urllib.request.urlopen` layer (patch it) — they
cannot rely on the webhook being live.

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
- A multi-threaded / multi-process enrichment worker (would require
  `select_for_update(skip_locked=True)` — see the concurrency note above).

## Corrections (2026-05-17, post-verification)

Substantive changes from the original approved draft:

1. **Prospeo endpoint.** `POST /mobile-finder` was retired 2026-03-01. Replaced
   with `POST /enrich-person` (`only_verified_mobile` body flag, `X-KEY` auth,
   `person.mobile.mobile` response path).
2. **BetterContact field requirements.** Submit needs first + last name +
   company; `linkedin_url` is only an optional hint. A missing `last_name`/
   `company_name` now short-circuits to `API_FAILURE` (graceful failover). The
   submit response id field is `id` (not `request_id`), and the body must set
   `enrich_phone_number: true`.
3. **`heal_tasks` race.** The unconditional `RUNNING → PENDING` reset at
   `daemon.py:192` now also excludes `ENRICH_PHONE`; the worker reclaims its own
   stale tasks at `start()`.
4. **Listener enqueue race.** Added a DB-level dedup against an existing
   `PENDING`/`RUNNING` `enrich_phone` task — the `phone_enriched_at` guard alone
   does not prevent duplicate enqueues (and duplicate provider billing).
5. **Prospeo non-2xx semantics** (found via the 2026-05-17 live contract-check).
   Prospeo returns HTTP 400 / `error_code: NO_MATCH` for a clean "no number"
   result. `HttpError` now carries `status` + parsed `body`, and
   `ProspeoProvider` maps a 400/`NO_MATCH` to `NOT_FOUND` instead of letting
   it fall through as `API_FAILURE` (which would have wrongly left the task
   `failed` and the lead un-stamped, causing a needless re-enrichment).

6. **Smaller corrections.** `operator` dropped from the task payload
   (enrichment is not account-scoped); worker uses thread-scoped
   `connections.close()`; API keys live as `conf.py` constants; worker is not
   gated on active hours; `notify_phone_enriched` is keyword-only; two
   migrations not one; `scheduled_at` has no default; the single-threaded
   worker is load-bearing because `claim_next` is not atomic.

## Provider contract fixes (2026-05-17, second verification pass)

A second parallel verification pass — three agents fact-checking the *as-built*
provider code against each vendor's live API docs — caught four implementation
bugs that the mocked unit tests could not (the mocks encoded the same wrong
assumptions as the code). All fixed:

1. **Prospeo response shape.** `enrich-person` returns `person` at the **top
   level** — there is no `response` wrapper (that wrapper exists only on
   Prospeo's unrelated `account-information` endpoint). The code read
   `resp["response"]["person"]…`, so every successful enrichment silently
   degraded to `NOT_FOUND`. Fixed to `resp["person"]["mobile"]["mobile"]`.
2. **Prospeo masked numbers.** `only_verified_mobile` alone returns a *masked*
   number (`revealed: false`). The submit body now also sets
   `enrich_mobile: true`, which triggers the reveal.
3. **LeadMagic endpoint URL.** Correct path is
   `https://api.leadmagic.io/v1/people/mobile-finder` — the code was missing
   the `/v1/people` segment and would have 404'd in production.
4. **BetterContact auth.** The documented scheme is the `X-API-Key` **header**
   on both the submit and poll endpoints; the code passed `?api_key=` as a
   query param (documented only for the unrelated `/account` endpoint). Moved
   to the header.

5. **BetterContact Cloudflare block** (found via the 2026-05-17 live check).
   `app.bettercontact.rocks` sits behind Cloudflare, which 403-bans urllib's
   default `Python-urllib/x` User-Agent (Cloudflare error 1010) — every submit
   and poll would have failed in production while the mocked tests stayed
   green. `http.py` now sends `User-Agent: OpenOutreach/1.0` on all enrichment
   requests.

Live-verified end-to-end against `linkedin.com/in/chukwukaagu` on 2026-05-17:
all three providers reached `terminated`/HTTP 200 through the as-built code.
BetterContact's terminated-response phone field is confirmed to be
`contact_phone_number`. None of the three returned a number for that profile —
a genuine coverage miss, not a defect.
