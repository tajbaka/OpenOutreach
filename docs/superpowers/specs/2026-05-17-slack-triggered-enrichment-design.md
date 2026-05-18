# Slack-Triggered Phone Enrichment — Design

**Date:** 2026-05-17
**Status:** Approved — ready for implementation planning

## Goal

Let the operator trigger phone enrichment for a lead **on demand from Slack**
instead of it running automatically on every inbound reply. The inbound-reply
notification gains a provider-select menu; the operator picks which enrichment
to run (full waterfall or one specific provider) and only then is a credit
spent. Auto-enrichment stays in the codebase, gated off by default.

## Background

Phone enrichment (`linkedin/enrichment/`, the `enrich_phone` task, the
`EnrichmentWorker`) currently runs automatically: the realtime listener
enqueues an `enrich_phone` Task on every inbound reply. That spends provider
credits on every reply whether or not the operator wants the number. This
feature makes enrichment operator-initiated from Slack.

## Decisions locked in

| Decision | Choice | Rationale |
|---|---|---|
| Trigger | A Slack interactive select menu on the reply notification | Operator decides per lead; no credit spent until they ask. |
| Where the endpoint runs | Vercel serverless function in the OpenOutreach repo (`api/slack_enrich.py`) | A Slack interaction handler is a textbook serverless function — scales to zero, ~free. Vercel + Neon is a native pairing. It lives in the repo that owns the `Task` table it writes to. |
| Endpoint ↔ daemon coupling | Shared `Task` table only | The function is just another Task producer, exactly like the realtime listener. The daemon needs no awareness of Slack. |
| Auto-enrichment | Kept in code, gated off — not removed | Already built and tested; one flag disables it and can flip it back on. |
| Worker lifecycle | `EnrichmentWorker` always runs | The select menu is always present, so enrichment must always be processable; the worker is a cheap idle DB poll. |
| Provider selection | Select menu: waterfall (default) or one specific provider | Lets the operator trade coverage vs cost per lead (BetterContact ~$0.49/hit vs LeadMagic ~$0.035/hit). |
| Single-provider = no failover | Accepted | "BetterContact only" returning `API_FAILURE` fails the task; it does **not** fall through to the others. That is what "only" means — the waterfall option exists for failover. |

## Architecture & data flow

```
inbound reply
  │
  ▼
listener handler → notify_message_received()
  posts the reply notification + a "📞 Get phone number" select menu
  (options: waterfall / bettercontact / leadmagic / prospeo)
  │
  ▼  operator picks an option
Slack POSTs the interaction → Vercel function  api/slack_enrich.py
  ├─ verify X-Slack-Signature (HMAC-SHA256, SLACK_SIGNING_SECRET)
  ├─ parse the selected option → (lead_id, provider)
  ├─ skip if a pending/running enrich_phone Task already exists for the lead
  ├─ INSERT enrich_phone Task into Neon  {lead_id, bettercontact_request_id:"", provider}
  └─ respond 200, update the message → "⏳ Fetching via <provider>…"
  │
  ▼  (daemon's EnrichmentWorker already polls the Task table)
handle_enrich_phone → run_waterfall(lead, task, chain=<from provider>)
  → write Lead.phone / phone_enriched_at → notify_phone_enriched() posts the result
```

The Vercel function and the daemon never talk directly — the `Task` table is
the entire contract between them.

## Components

### Config — flag change

Remove `ENABLE_PHONE_ENRICHMENT`. It currently gates *both* the listener
auto-enqueue *and* the worker spawn — one flag conflating two concerns.
Replace it with:

- `ENABLE_AUTO_PHONE_ENRICHMENT` — `bool`, default `False`. Gates **only** the
  realtime listener's auto-enqueue of `enrich_phone` on every inbound reply.

The `EnrichmentWorker` is **no longer gated** — `run_daemon` always spawns it,
because the select menu is always available so enrichment must always be
processable. The daemon's queue-empty exit guard likewise drops the
`ENABLE_PHONE_ENRICHMENT` condition and always checks for outstanding
`enrich_phone` tasks before exiting.

Operator's target state: `ENABLE_AUTO_PHONE_ENRICHMENT=false` — auto off, the
Slack menu always available, the worker always running.

### `notify_message_received` — the select menu

`linkedin/notifications/slack.py:notify_message_received` gains an `actions`
block containing one `static_select` element:

- `placeholder`: "📞 Get phone number"
- `action_id`: `"enrich_phone_select"`
- options — each option's `value` encodes `"<lead_id>:<provider>"`:
  - "📞 All providers (waterfall)" → `<lead_id>:waterfall`
  - "BetterContact only" → `<lead_id>:bettercontact`
  - "LeadMagic only (cheapest)" → `<lead_id>:leadmagic`
  - "Prospeo only" → `<lead_id>:prospeo`

Posted through the existing incoming webhook — no change to the send path. The
block is always included (no feature flag for it).

### Vercel function — `api/slack_enrich.py`

A Python serverless function. The filename uses an underscore (not a hyphen)
so the logic stays importable for tests; the Slack Request URL is
`https://<project>.vercel.app/api/slack_enrich`.

Responsibilities:
1. **Verify the Slack signature** — `X-Slack-Signature` +
   `X-Slack-Request-Timestamp`, HMAC-SHA256 with `SLACK_SIGNING_SECRET`.
   Reject (`401`) on a mismatch or a timestamp older than 5 minutes.
2. **Parse the interaction** — Slack POSTs `application/x-www-form-urlencoded`
   with a `payload` field (URL-encoded JSON). Extract
   `actions[0].selected_option.value` and split into `lead_id` and `provider`.
3. **Dedup** — query Neon for a `pending`/`running` `enrich_phone` Task for
   that `lead_id`; if one exists, respond "already queued" without inserting.
4. **Enqueue** — `INSERT` a `Task` row into Neon: `task_type='enrich_phone'`,
   `status='pending'`, `scheduled_at=now()`, `created_at=now()`,
   `payload={'lead_id': <id>, 'bettercontact_request_id': '', 'provider': <provider>}`.
   Raw parameterized SQL via `psycopg` — the function does not import Django.
5. **Respond** within Slack's 3-second window — `200` with a message update
   swapping the menu for "⏳ Fetching via <provider>…".

Env vars (Vercel project): `DATABASE_URL` (OpenOutreach's Neon connection
string), `SLACK_SIGNING_SECRET`.

Repo additions: `api/slack_enrich.py`, `vercel.json` (no build step; Python
function), and a function-scoped `requirements.txt` (`psycopg[binary]`).

The function's logic is factored into pure, importable units —
`verify_signature(...)`, `parse_interaction(...)`, `enqueue_task(...)` — so it
is unit-testable without deploying.

### `handle_enrich_phone` — provider routing

`linkedin/tasks/enrich_phone.py:handle_enrich_phone` reads
`task.payload.get("provider", "waterfall")`:
- `"waterfall"` (or absent) → `run_waterfall(lead, task)` — the full
  `PROVIDER_CHAIN`, unchanged.
- a specific provider name → `run_waterfall(lead, task, chain=[<provider>])`.

`linkedin/enrichment/waterfall.py` gains
`PROVIDERS_BY_NAME = {p.name: p for p in PROVIDER_CHAIN}` for the lookup. An
unrecognized provider name logs a warning and falls back to the full
waterfall (defensive — the value originates from our own select menu).

### Listener auto-enqueue

`linkedin/realtime/handler.py:_maybe_enqueue_enrichment` is re-gated on
`ENABLE_AUTO_PHONE_ENRICHMENT` and writes `provider: "waterfall"` into the
Task payload for consistency with the Slack-triggered path.

### Slack app setup (one-time, manual)

On the existing Slack app that owns the incoming webhook: enable
**Interactivity & Shortcuts**, set the **Request URL** to the deployed Vercel
function URL, and copy the app's **Signing Secret** into the Vercel project's
environment.

## Error handling

- Invalid / missing / stale Slack signature → `401`, no DB write.
- Malformed interaction payload → `400`.
- Neon unreachable or the INSERT fails → `500`; Slack surfaces an error to the
  operator, who can re-click.
- **Duplicate clicks** — the function's SELECT-then-INSERT dedup is
  best-effort (a TOCTOU window exists across concurrent function
  invocations). A duplicate Task is harmless regardless: the single-threaded
  `EnrichmentWorker` runs tasks in series, so the second sees
  `phone_enriched_at` already set (on a `FOUND`/`NOT_FOUND` first run) and
  skips; an `API_FAILURE` re-attempt costs nothing (provider misses and
  failures are not billed).
- A single-provider selection returning `API_FAILURE` → the task is marked
  `failed` with no failover, by design.

## Testing

pytest under `tests/`:
- **Vercel function** (`tests/test_slack_enrich.py`, importing
  `api/slack_enrich.py`): signature verification (valid, bad signature, stale
  timestamp, missing headers); interaction parsing (lead id + provider
  extraction); dedup; the INSERT (mocked DB connection).
- `notify_message_received`: the select block is present with the four
  options and correctly encoded `value`s.
- `handle_enrich_phone`: `provider="waterfall"` / absent → full chain;
  `provider="leadmagic"` → single-provider chain; an unknown provider →
  waterfall fallback.
- Flag behaviour: the listener auto-enqueues only when
  `ENABLE_AUTO_PHONE_ENRICHMENT` is true; the `EnrichmentWorker` is spawned
  unconditionally.

## Documentation

Update `CLAUDE.md` and `ARCHITECTURE.md`: the flag rename
(`ENABLE_PHONE_ENRICHMENT` → `ENABLE_AUTO_PHONE_ENRICHMENT`), the always-on
worker, the Slack select menu, the `api/slack_enrich.py` function, and the
`provider` field on the `enrich_phone` task payload.

## Out of scope

- Showing all three providers' results side by side (settled — failover
  stays; see `2026-05-17-phone-enrichment-design.md`).
- Email enrichment (the providers return it; it is not persisted).
- Slack slash commands (select menu only).
- A feature flag for the select menu itself (always rendered).
