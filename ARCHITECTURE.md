# Architecture

Detailed module documentation for OpenOutreach. See `CLAUDE.md` for rules and quick reference.

## Entry Flow

`manage.py` (Django bootstrap + auto-migrate + CRM setup):
- Suppresses Pydantic serialization warning from langchain-openai. Configures logging: DEBUG level, suppresses noisy third-party loggers.
- No args → runs daemon: startup checks → `ensure_onboarding()` → validate `LLM_API_KEY` → `get_or_create_session(handle)` → set default campaign → `session.ensure_browser()` → `ensure_self_profile()` → GDPR newsletter override (marker-guarded) → `ensure_newsletter_subscription()` → `run_daemon(session)`.
- With `runserver` arg → auto-migrates, then delegates to Django CLI.
- Other args → delegates directly to `execute_from_command_line`.

### Startup Integrity Checks

Before the daemon does any work, `manage.py`'s no-args branch runs two
checks, both before `_ensure_db()`:

- `linkedin/version_check.py` — `check_for_updates()` runs `git fetch` and compares local `HEAD` to the current branch's upstream `@{u}`. When behind, a TTY session is prompted to pull and a headless run auto-pulls. A successful `git pull --ff-only` exits 0 because the process must restart on the newly pulled code; a failed pull logs loudly, posts `notify_error`, and exits 1. Non-git deployments are a silent no-op.
- `linkedin/env_check.py` — `check_env_vars()` logs one grouped summary of missing environment variables. Advisory only; never aborts startup.
- `linkedin/env_spec.py` — declared `EnvVar` registry consumed by `check_env_vars()`. This is the single source of truth for project-owned env vars, and `.env.example` is kept in the same order so drift is easy to spot.

## Onboarding (`onboarding.py`)

`ensure_onboarding()` ensures Campaign, active LinkedInProfile, LLM config, and legal acceptance exist. Four checks:

1. **Campaign** — interactive prompts for campaign name, product docs, objective, booking link. Creates `Campaign` owned by the onboarding operator.
2. **LinkedInProfile** — prompts for LinkedIn email, password, newsletter, rate limits. Handle from email slug.
3. **LLM config** — prompts for `LLM_API_KEY`, `AI_MODEL`, `LLM_API_BASE` → writes to `.env`.
4. **Legal notice** — per-account acceptance stored as `LinkedInProfile.legal_accepted`.

## Profile State Machine

`enums.py:ProfileState` (TextChoices) values ARE CRM stage names: QUALIFIED, READY_TO_CONNECT, PENDING, CONNECTED, COMPLETED, FAILED. Pre-Deal states: url_only (no description), enriched (has description). `Lead.disqualified=True` = permanent account-level exclusion. LLM rejections = FAILED Deals with "Disqualified" closing reason (campaign-scoped).

`crm/models/deal.py:ClosingReason` (TextChoices): COMPLETED, FAILED, DISQUALIFIED. Used by `Deal.closing_reason`.

## Task Queue

Persistent queue backed by `Task` model. Worker loop in `daemon.py`: `seconds_until_active()` guard pauses outside active hours/rest days → pop oldest due task → set campaign on session → RUNNING → dispatch via `_HANDLERS` dict → COMPLETED/FAILED. Failures captured by `failure_diagnostics()` context manager. `heal_tasks()` reconciles on startup.

Task types (handlers in `linkedin/tasks/`, signature: `handle_*(task, session, qualifiers)`):

1. **`handle_connect`** — Unified via `ConnectStrategy` dataclass. Regular: `find_candidate()` from `pools.py`; freemium: `find_freemium_candidate()`. Unreachable detection after `MAX_CONNECT_ATTEMPTS` (3).
2. **`handle_sweep_connections`** — Account-wide. Scrapes `mynetwork/invite-connect/connections/` once per `CONNECTION_SWEEP_INTERVAL_HOURS`, cross-references PENDING Deals by `public_id`, transitions matches to CONNECTED and enqueues `follow_up`. Replaces the legacy per-profile `check_pending` flow. Plain accepted invites are not posted individually to Slack; accepted-and-replied leads still post, and completion posts a lean per-sender analytics snapshot to the ops Slack channel via `_post_sweep_summary` → `notify_sweep_summary` (sends today, newly accepted count, pending/connected/failed Deal counts) — best-effort, never blocks the sweep.
3. **`handle_follow_up`** — Per-profile. Sends rigid ICP LinkedIn follow-up sequence steps from `icp_messages.json`, gated by `ENABLE_FOLLOW_UP` and the follow-up rate limit. Payloads may carry `sequence_name`, `channel`, and `step_index`; missing values default to the current one-step `linkedin_connect_followup` / same channel / `0` behavior. Owner scoping compares outbound `Message.sender` values through `linkedin.operators.resolve_operator`, so new LinkedIn display variants must be added there. Stop checks are DB-local only: inbound LinkedIn/Gmail message, existing `crm.Meeting`, disqualified lead, suppression, or non-CONNECTED state. On send failure it re-enqueues the same step in 24h. On non-final success it records `ActionLog`, persists an outbound `crm.Message`, and enqueues the next step after that step's `delay_days`, normalized into configured active hours/rest days, while keeping the Deal `CONNECTED`; final success marks the Deal `COMPLETED`. Step-level dedup for a non-final already-sent step keeps the Deal `CONNECTED` and ensures the next step is queued; only a final-step dedup marks `COMPLETED`. Post-send retries only retry the state write so a dead DB connection cannot double-count the action or duplicate the next-step Task. The single-cell ICP Messages Sheets sync is legacy-only for follow-up copy: it rejects sequence-shaped follow-up channels on push and preserves existing sequences on pull, so multi-step copy is edited directly in JSON.
4. **`handle_manual_reply`** — Slack-to-LinkedIn reply lane. Slack modal submit inserts a `manual_reply` Task with `lead_id`, `operator`, `message`, Slack message coordinates, and original Slack blocks. The queued Slack status includes a cancel button backed by the Vercel endpoint; cancel deletes only a still-`pending` task, and reports if the daemon already started claiming/sending it. `Task.objects.claim_next()` atomically flips the selected task from `pending` to `running`, so a successfully cancelled reply cannot still be sent by a daemon that had only read the row. The daemon claims manual replies ahead of normal outbound work, scoped by `payload.operator`, and sends through the same logged-in Playwright page via `send_raw_message`. Manual replies use the direct-thread UI composer with human typing and deliberately disable the Voyager API fallback, so a UI send failure fails the task instead of sending instantly. Manual replies bypass active-hours sleeps when due; while off-hours and no reply is due, the daemon caps sleep to `MANUAL_REPLY_POLL_SECONDS` (default 60) so newly queued replies are picked up quickly without running normal automation. Manual replies do not consume connect/follow-up quotas, do not advance sequences, and do not change Deal state; the durable outreach side effect is the outbound `crm.Message` with a `manual-reply:` synthetic external id. Before sending, the handler checks that same `crm.Message` ledger for an existing same lead/operator/body manual reply and skips duplicates, covering the crash-after-send/before-task-complete window. Slack sent/failed acknowledgements are best-effort via `chat.update` on the original notification, falling back to the interaction `response_url`.

## Qualification ML Pipeline

GPR (sklearn, ConstantKernel * RBF) inside Pipeline(StandardScaler, GPR) with BALD active learning:

1. **Balance-driven selection** — n_negatives > n_positives → exploit (highest P); otherwise → explore (highest BALD).
2. **LLM decision** — All decisions via LLM (`qualify_lead.j2`). GP only for candidate selection and confidence gate.
3. **READY_TO_CONNECT gate** — P(f > 0.5) above `min_ready_to_connect_prob` (0.9) promotes QUALIFIED → READY_TO_CONNECT.

384-dim FastEmbed embeddings stored directly on Lead model, per-campaign GP models at ``Campaign.model_blob` (BinaryField)`. Cold start returns None until >=2 labels of both classes.

## Django Apps

Three apps in `INSTALLED_APPS`:

- **`linkedin`** — Main app: Campaign (owned by one User), LinkedInProfile, SearchKeyword, ActionLog, Task models. All automation logic.
- **`crm`** — Lead (with embedding) and Deal models (in `crm/models/lead.py` and `crm/models/deal.py`). Also defines `ClosingReason` enum.
- **`chat`** — `ChatMessage` model (GenericForeignKey to any object, content, owner, answer_to threading, topic).

## CRM Data Model

- **Campaign** (`linkedin/models.py`) — `name` (unique), `user` (FK to User), `product_docs`, `campaign_objective`, `booking_link`, `is_freemium`, `action_fraction`, `seed_public_ids` (JSONField).
- **LinkedInProfile** (`linkedin/models.py`) — 1:1 with User. Credentials, rate limits (`connect_daily_limit`, `connect_weekly_limit`, `follow_up_daily_limit`). Methods: `can_execute`/`record_action`/`mark_exhausted`. In-memory `_exhausted` dict for daily rate limit caching.
- **SearchKeyword** (`linkedin/models.py`) — FK to Campaign. `keyword`, `used`, `used_at`. Unique on `(campaign, keyword)`.
- **ActionLog** (`linkedin/models.py`) — FK to LinkedInProfile + Campaign. `action_type` (connect/follow_up), `created_at`. Composite index on `(linkedin_profile, action_type, created_at)`.
- **Lead** (`crm/models/lead.py`) — Per LinkedIn URL (`linkedin_url` = unique). `public_identifier` (derived from URL). `first_name`, `last_name`, `company_name`. `description` = parsed profile JSON. `embedding` = 384-dim float32 BinaryField (nullable). `disqualified` = permanent exclusion. `embedding_array` property for numpy access. `get_labeled_arrays(campaign)` classmethod returns (X, y) for GP warm start. Labels: non-FAILED state → 1, FAILED+DISQUALIFIED → 0, other FAILED → skipped.
- **Deal** (`crm/models/deal.py`) — Per campaign (campaign-scoped via FK). `state` = CharField (ProfileState choices). `closing_reason` = CharField (ClosingReason choices: COMPLETED/FAILED/DISQUALIFIED). `reason` = qualification/failure reason. `connect_attempts` = retry count. `backoff_hours` = check_pending backoff. `creation_date`, `update_date`.
- **Task** (`linkedin/models.py`) — `task_type` (connect/follow_up/sweep_connections; legacy `check_pending` retained for historical rows), `status` (pending/running/completed/failed), `scheduled_at`, `payload` (JSONField), `error`, `started_at`, `completed_at`. Composite index on `(status, scheduled_at)`.
- **ChatMessage** (`chat/models.py`) — GenericForeignKey to any object. `content`, `owner`, `answer_to` (self FK), `topic` (self FK), `recipients`, `to` (M2M to User).

## Key Modules

- **`daemon.py`** — Worker loop with active-hours guard (`ENABLE_ACTIVE_HOURS` flag, `seconds_until_active()`), `_build_qualifiers()`, `heal_tasks()`, freemium import, `_FreemiumRotator`.
- **`diagnostics.py`** — `failure_diagnostics()` context manager, `capture_failure()` saves page HTML/screenshot/traceback to `/tmp/openoutreach-diagnostics/`.
- **`tasks/connect.py`** — `handle_connect`, `ConnectStrategy`, `enqueue_connect`/`enqueue_follow_up`. Connect-note rendering uses `icp_outbound.safe_company_name()` so `"Unknown Company"` never leaks into outbound notes.
- **`tasks/sweep_connections.py`** — `handle_sweep_connections`, `enqueue_sweep_connections`. Replaces legacy `check_pending`.
- **`tasks/follow_up.py`** — `handle_follow_up`, rigid ICP LinkedIn DM send, sequence payload shim, rate limiting.
- **`tasks/manual_reply.py`** — `handle_manual_reply`, Slack-composed LinkedIn reply sends from the daemon's logged-in browser account.
- **`pipeline/qualify.py`** — `run_qualification()`, `fetch_qualification_candidates()`.
- **`pipeline/search.py`** — `run_search()`, keyword management.
- **`pipeline/search_keywords.py`** — `generate_search_keywords()` via LLM.
- **`pipeline/ready_pool.py`** — GP confidence gate, `promote_to_ready()`.
- **`pipeline/pools.py`** — Composable generators: `search_source` → `qualify_source` → `ready_source`.
- **`pipeline/freemium_pool.py`** — Seed priority + undiscovered pool, ranked by qualifier.
- **`ml/qualifier.py`** — `Qualifier` protocol, `BayesianQualifier`, `KitQualifier`, `qualify_with_llm()`.
- **`ml/embeddings.py`** — FastEmbed utilities, `embed_profile()`.
- **`ml/profile_text.py`** — `build_profile_text()`.
- **`ml/hub.py`** — HuggingFace kit loader (`fetch_kit()`).
- **`browser/session.py`** — `AccountSession`: handle, linkedin_profile, page, context, browser, playwright. `campaigns` property (via Campaign.user FK). `ensure_browser()` launches/recovers browser. Cookie expiry check via `_maybe_refresh_cookies()`.
- **`browser/registry.py`** — `AccountSessionRegistry`, `get_or_create_session()`.
- **`browser/login.py`** — `start_browser_session()` — browser launch + LinkedIn login.
- **`browser/nav.py`** — Navigation, auto-discovery, `goto_page()`.
- **`db/leads.py`** — Lead CRUD, `lead_to_profile_dict()`, `get_leads_for_qualification()`, `disqualify_lead()`.
- **`db/deals.py`** — Deal/state ops, `set_profile_state()`, `increment_connect_attempts()`, `create_freemium_deal()`.
- **`db/enrichment.py`** — Lazy enrichment/embedding (`ensure_profile_embedded()`).
- **`db/chat.py`** — `save_chat_message()`.
- **`db/urls.py`** — `url_to_public_id()`, `public_id_to_url()` — LinkedIn URL ↔ public identifier conversion.
- **`db/messages.py`** — `persist_thread()`: idempotent get_or_create per `(source, external_id)`; derives LinkedIn direction from a normalized sender match against the Lead name, stripping common honorifics like `Dr.` while explicit daemon operator senders still force outbound. LinkedIn invite-note echoes that arrive with the lead as sender are also forced outbound when they match stored `sent_note` text or a narrow legacy connect-note pattern. Falls back to `now()` on malformed timestamps. Called from `actions/conversations.py:get_conversation` as a best-effort side effect — never breaks the caller.
- **`conf.py`** — Config loading (dotenv), `CAMPAIGN_CONFIG`, path constants, `get_first_active_profile_handle()`.
- **`exceptions.py`** — `AuthenticationError`, `TerminalStateError`, `SkipProfile`, `ReachedConnectionLimit`, `SheetsError`.
- **`onboarding.py`** — Interactive setup.
- **`agents/follow_up.py`** — ReAct agent for follow-up conversations. Tools: `read_conversation`, `send_message`, `mark_completed`, `schedule_follow_up`.
- **`actions/`** — `connect.py` (`send_connection_request`), `status.py` (`get_connection_status`), `message.py` (`send_raw_message`), `profile.py` (profile extraction), `search.py` (LinkedIn search), `conversations.py` (`get_conversation`).
- **`notifications/sheets.py`** — gspread client for Google Sheets. Single `People` tab, 15 columns (Name, First name, Last name, Company, Title, LinkedIn URL, Email addresses, Outreach status, Stage, Priority, Primary location, Notes, AI Notes, Created at, Last synced). Rows keyed by `LinkedIn URL` — no per-row pointer on the Lead model. `SheetIndex.load()` slurps the whole tab in one API call, then `upsert_row()` schedules per-row appends/updates and `flush()` commits via `batch_update`. Don't-downgrade rules: `should_patch_outreach_status` and `should_patch_stage` block downgrades so manual sheet edits survive. Outreach status ranks: Invite Sent → Connected → Waiting → Replied → Wants Meeting → Meeting Booked → Had Meeting → Manual followup → Prospecting to close → Won → Don't send (with Lost as a separate terminal-negative). Stage hierarchy: Prospecting → Qualification → Meeting → Closing → Won (Lost terminates). Priority defaults to "Low" when empty so the column never has blanks. Auth: service-account JSON at `GOOGLE_SHEETS_CREDENTIALS_PATH`, scope `https://www.googleapis.com/auth/spreadsheets`. Service account email needs Editor access shared on the sheet.
- **`docs/followups-sort-buttons.gs`** — Google Apps Script (paste into the spreadsheet's Extensions → Apps Script). Adds a "Followups" menu with two within-section sort actions on any `<Operator> - Followups` tab: "Sort: Action needed" (both Sent toggles = No first, then PRIORITY desc) and "Sort: Days since (oldest first)". Reads formulas alongside values so HYPERLINK cells survive the sort; writes each section's data rows back as one range so divider merges stay intact. Column order depends on `FU_HEADERS` in `notifications/sheets.py`.
- **`notifications/synthesis.py`** — Synthesis pass invoked per-Deal from `sync_sheets`. `extract_email_from_messages` (D1) regex-extracts email from inbound `crm.Message` rows and mutates `Lead.email` directly (sync_sheets folds the union into the row payload). `detect_wants_meeting` (D2) calls a cheap LLM with structured Pydantic output. `synthesize_for_deal(deal, current_outreach_status="")` returns a `SynthResult(wants_meeting_now, note_block)` for the caller to fold into the Sheet row — no external system writes here. Three gates: skip-when-already-detected (`Deal.wants_meeting_detected_at`), skip-when-no-new-messages (`Deal.last_synthesized_at` vs latest `Message.sent_at`), skip-when-current-sheet-status-already-past-Wants-Meeting. All failures logged; never break the surrounding Stage/Status sync.
- **`api/client.py`** — `PlaywrightLinkedinAPI`: browser-context fetch (runs JS `fetch()` inside Playwright page for authentic headers). `get_profile()` with tenacity retry.
- **`api/voyager.py`** — `LinkedInProfile` dataclass (url, urn, full_name, headline, positions, educations, country_code, supported_locales, connection_distance/degree). `parse_linkedin_voyager_response()`.
- **`api/newsletter.py`** — `subscribe_to_newsletter()` via Brevo form, `ensure_newsletter_subscription()`.
- **`api/messaging/send.py`** — Send messages via Voyager messaging API.
- **`api/messaging/conversations.py`** — Fetch conversations/messages.
- **`api/messaging/utils.py`** — Shared helpers: `get_self_urn()`, `encode_urn()`, `check_response()`.
- **`setup/freemium.py`** — `import_freemium_campaign()`, `seed_profiles()`.
- **`setup/gdpr.py`** — `apply_gdpr_newsletter_override()`.
- **`setup/self_profile.py`** — `ensure_self_profile()`.
- **`setup/seeds.py`** — User-provided seed profiles: parse URLs, create Leads + QUALIFIED Deals.
- **`management/setup_crm.py`** — Idempotent CRM bootstrap (Site creation).
- **`admin.py`** — Django Admin: Campaign, LinkedInProfile, SearchKeyword, ActionLog, Task, ChatMessage.
- **`django_settings.py`** — Django settings (SQLite at `db.sqlite3`). Apps: crm, chat, linkedin.

## Realtime Inbound Message Listener (`linkedin/realtime/`)

Near-realtime detection of inbound LinkedIn DMs. Gated by `ENABLE_REALTIME_LISTENER` (`conf.py`, default `false`). Any failure degrades gracefully to the existing polling path — realtime is an enhancement, not a dependency.

### Architecture: Separate Child Process (v2)

The listener runs as a **separate child process** — `manage.py listen_realtime` — which the daemon spawns and supervises via `linkedin/realtime/supervisor.py` (`ListenerSupervisor`). This is the key architectural decision: the listener does NOT run in-process with the daemon.

**Why a separate process is required.** Playwright's sync API is built on a greenlet model: one event loop per process, and CDP event handlers and Playwright's task loop share it. An earlier in-process design (v1) attempted to drive CDP `Network.dataReceived` event callbacks while the daemon's task loop also drove sync Playwright — this corrupted Playwright's sync greenlet state and made the approach unworkable. Running the listener in its own process gives it a clean, independent Playwright/asyncio loop with no contention.

**Persistent browser context.** The daemon launches Chromium using `launch_persistent_context` (storing state under `data/profile-<account>/`) with a fixed `--remote-debugging-port` controlled by `LISTENER_CDP_PORT` (default 9222, localhost-only). This port is opened only when `ENABLE_REALTIME_LISTENER` is on. The listener calls `connect_over_cdp` to attach to this already-running browser and shares its one browser context — one device fingerprint, one cookie jar. From LinkedIn's perspective this looks like one browser with two tabs, not two browsers, which is the correct bot-detection posture.

**`StandaloneLinkedInSession`** (used by `backfill_messages` and sales-nav flows) stays on `launch()` + per-account JSON cookie files (`data/<label>_cookies.json`). It is not migrated to a persistent context; only the daemon uses `launch_persistent_context`.

### Modules

- **`supervisor.py`** — `ListenerSupervisor`: spawns `manage.py listen_realtime` as a subprocess, restarts it on unexpected death, gives up after 5 consecutive spawn failures, and runs/stops the child according to listener-specific hours (`LISTENER_ACTIVE_START_HOUR`, `LISTENER_ACTIVE_END_HOUR`, `LISTENER_REST_DAYS`) rather than outbound active hours.
- **`listener.py`** — `run_listener` / `_run_one_connection`: calls `connect_over_cdp` to attach to the daemon's browser, opens a `/messaging/` tab in the shared context, enables the CDP `Network` domain, calls `Network.streamResourceContent` to opt in to streaming, and receives `Network.dataReceived` events carrying base64-encoded SSE bytes. Reconnects automatically on a dropped CDP connection.
- **`sse.py`** — `RealtimeSSEBuffer`: accumulates base64-encoded CDP stream chunks, decodes them, and frames the raw bytes into complete SSE events (splitting on `\n\n`).
- **`parser.py`** — `parse_realtime_event(raw_event) → ParsedRealtimeMessage | None`: decodes the SSE `data:` payload as JSON, walks LinkedIn's realtime envelope, and extracts sender URN, conversation URN, message URN, body text, and `sent_at` timestamp. Returns `None` for non-message events (presence pings, typing indicators, etc.).
- **`handler.py`** — `handle_realtime_event(raw_event, account_label)`: orchestrates parse → lead lookup → `persist_thread` → `notify_message_received`. Inbound-only; outbound events are silently dropped. Reply Slack notifications include the full Slack-safe quoted message body, preserving line breaks and only truncating near Slack's 3000-character section limit, and carry the triggering LinkedIn `thread_external_id` so the manual-reply modal can show the right sender-specific thread when one Lead is shared across operators. Slack notification HTTP uses certifi's CA bundle so daemon hosts with stale system trust stores still post reliably. All exceptions are caught and logged so a bad event never crashes the listener.
- **`heartbeat.py`** — Writes and reads `data/listener-heartbeat-<account>.json` (timestamp + account label). Updated by the listener process; read by startup catch-up to compute how long the listener was offline.
- **`lead_lookup.py`** — `resolve_lead_for_realtime(conversation_urn, sender_urn) → Lead | None`: queries the DB first by conversation URN (matched against Deal metadata), then falls back to sender URN (matched against `Lead.linkedin_url`).
- **`catchup.py`** — `run_startup_catchup(account_label)`: reads the heartbeat file; if the gap since the last heartbeat exceeds `LISTENER_CATCHUP_GAP_MINUTES` (default 30), prompts the operator on TTY to run `backfill_messages --account primary --skip-prereq-gate`, or logs a warning when running headless.

### Why CDP `Network.streamResourceContent`, Not `eventSourceMessageReceived`

LinkedIn's `/realtime/connect` endpoint delivers a `text/event-stream` body over a regular `fetch()` call — it is not opened via the browser's native `EventSource` API. Playwright's `page.expect_event("websocket")` and CDP's `Network.eventSourceMessageReceived` only fire for native `EventSource` connections; they produce zero events here. The correct tap is `Network.streamResourceContent` (to opt in to streaming) followed by `Network.dataReceived` events, whose `data` field carries base64-encoded chunks of the raw SSE bytes. This was verified against a live LinkedIn session.

### Data Flow

```
CDP Network.dataReceived (base64 chunk)
  → RealtimeSSEBuffer.feed() → complete SSE event string
  → parse_realtime_event()   → ParsedRealtimeMessage | None
  → handle_realtime_event()
      → resolve_lead_for_realtime()  → Lead | None
      → persist_thread()             → crm.Message (idempotent)
      → notify_message_received()    → Slack (replies webhook)
```

### Lifecycle

- **Supervisor**: `ListenerSupervisor` runs inside the daemon. It spawns `listen_realtime`, watches for unexpected exits (restart), gives up after 5 consecutive failures, and runs/stops the child according to listener-specific hours. Defaults are 0–24 with no rest days, so inbound Slack notifications can continue while outbound automation sleeps.
- **Reconnect**: inside the listener process, `_run_one_connection` wraps a single CDP session; `run_listener` loops around it so a dropped CDP connection triggers a clean reconnect without a full process restart.
- **Startup catch-up**: the daemon calls `run_startup_catchup(account_label)` during startup. If the heartbeat gap exceeds `LISTENER_CATCHUP_GAP_MINUTES` (default 30 min), it either prompts the operator interactively (TTY) or emits a `WARNING` log (headless) recommending:

```bash
.venv/bin/python manage.py backfill_messages --account primary --skip-prereq-gate
```

`--skip-prereq-gate` bypasses the interactive staleness prompt inside `backfill_messages` so it can be called non-interactively.

## Phone Enrichment (`linkedin/enrichment/`)

Phone-number enrichment, **operator-triggered from Slack**. The
`EnrichmentWorker` always runs; auto-enqueue on every inbound reply is
opt-in via `ENABLE_AUTO_PHONE_ENRICHMENT` (`conf.py`, default off).

**Trigger.** Every inbound-reply Slack notification (`notify_message_received`)
carries a "Reply on LinkedIn" button, a "Lead context" button, plus a
"📞 Get phone number" `static_select` menu — waterfall (default) /
bettercontact / leadmagic / prospeo. The operator's pick/button/modal submit is
POSTed by Slack to a Vercel serverless function (`api/slack_enrich.py`), which
verifies the Slack request signature (`SLACK_SIGNING_SECRET`, HMAC-SHA256) and
routes each Slack intention through an explicit action-id-to-handler dispatch table.
Enrichment picks parse `(lead_id, provider)` and INSERT an `enrich_phone`
`Task`; reply modal submits INSERT a daemon-dispatched `manual_reply` `Task`.
The function uses raw `psycopg` (no Django import), and `SLACK_BOT_TOKEN` is
required for `views.open` / `views.update`; queued status updates prefer the
interaction `response_url` and fall back to `chat.update` when metadata is
available, while daemon sent/failed status uses `chat.update` with the task's
saved Slack blocks. The reply modal fetches the recent LinkedIn `crm.Message`
thread via raw SQL and renders a compact transcript above the reply textbox;
new reply/context buttons scope that preview by the triggering
`thread_external_id`, while legacy buttons fall back to the latest inbound
LinkedIn thread for that lead. The Lead context modal fetches deterministic
Lead/Deal/profile/thread context via raw SQL; its AI summary and Draft reply
buttons call the configured OpenAI-compatible endpoint (`LLM_API_KEY`,
`AI_MODEL`, optional `LLM_API_BASE`) and update the same modal. Generated
summary/draft text is carried in the modal's private metadata so each remains
visible as its own Lead context section while the other action runs. The queued Slack
status includes a cancel button whose payload points at the inserted task id;
cancelling deletes the task only if it is still pending. If the preview fetch
fails, the reply modal falls back to a plain textbox. The `Task` table is the entire contract between
the function and the daemon — they never talk directly. The function dedups
against an existing `PENDING`/`RUNNING` `enrich_phone` task for the same
`(lead, provider)` (best-effort — a duplicate is harmless); two *different*
providers can be queued for one lead at once. Separately, the realtime
listener's handler (`linkedin/realtime/handler.py`) can still auto-enqueue a
`waterfall` task on a persisted inbound reply when `ENABLE_AUTO_PHONE_ENRICHMENT`
is on, with the same per-`(lead, provider)` dedup. Either path writes
`payload={lead_id, bettercontact_request_id, provider}`.

**Worker.** `EnrichmentWorker` (`worker.py`) is a single background thread
`run_daemon` always spawns alongside the listener supervisor (no longer
flag-gated — the Slack menu is always available so enrichment must always be
processable). It claims
`enrich_phone` tasks via `Task.objects.next_enrichment()` — the outbound loop
excludes `ENRICH_PHONE` from `claim_next`/`seconds_to_next`, and `heal_tasks`
excludes it from the stale-`RUNNING` reset, so the two never race. The worker
reclaims its own stale `RUNNING` tasks at `start()` (the daemon has no clean
shutdown — this is the crash-recovery path). HTTP-only, so it is not gated on
active hours. Single-threaded is load-bearing: `next_enrichment` is a plain
read, not a locking claim.

**Waterfall.** `run_waterfall` (`waterfall.py`) iterates a provider chain.
`handle_enrich_phone` routes on the task payload's `provider` field: the
default `"waterfall"` runs the full `PROVIDER_CHAIN`; a specific provider name
(looked up in `PROVIDERS_BY_NAME`) runs that provider only, with **no
failover** — an unrecognized name logs a warning and falls back to the full
chain. The full chain is BetterContact → LeadMagic → Prospeo. `FOUND`/`NOT_FOUND` is terminal
(BetterContact's `NOT_FOUND` is authoritative — it is itself a 20+ provider
waterfall); `API_FAILURE` escalates. BetterContact is async (submit → poll,
resumable via the persisted `bettercontact_request_id`) and short-circuits to
`API_FAILURE` when the lead lacks the `last_name`/`company_name` its submit
needs. LeadMagic and Prospeo are synchronous and LinkedIn-URL native.
Providers implement the `PhoneProvider` protocol (`base.py`); transport
failures raise `HttpError` (→ `API_FAILURE`), malformed responses raise
`EnrichmentError`.

**Outcome (multi-number).** `handle_enrich_phone` (`linkedin/tasks/enrich_phone.py`)
lets a lead carry many numbers. `Lead.phones` is a JSON list of
`{number, provider, found_at}` — `FOUND` appends one entry (deduping a number
already present); `Lead.phone_numbers` is a bare-string convenience property.
`Lead.phone_providers_tried` records every provider that returned a definitive
result (`FOUND` or `NOT_FOUND`); `API_FAILURE` is not recorded, so it stays
retryable. Skip is per-provider — a single-provider task skips if that
provider is already in `phone_providers_tried`; a waterfall task skips only
once every provider is. `FOUND`/`NOT_FOUND` post a Slack message via
`notify_phone_enriched`; all-`API_FAILURE` posts nothing and marks the task
`failed`.

## Node Monitoring (`linkedin/monitoring/`)

Liveness + degraded-state monitoring with no third-party service. Each
daemon is a "node"; the design relies only on Neon and the ops Slack
webhook. Gated by `ENABLE_NODE_MONITOR` (default on).

**Peer liveness — "is the daemon process alive".** A dead daemon cannot
report its own death, so peers report it. The `NodeMonitor` background
thread (`node_monitor.py`, same start/stop pattern as `EnrichmentWorker`)
runs every `MONITOR_INTERVAL_SECONDS`:

1. **Heartbeat** — `write_heartbeat()` stamps this node's `DaemonHeartbeat`
   row (`linkedin` app; one row per sender, keyed by the resolved operator
   handle) with `last_alive = now()` and clears `down_alerted_at`.
2. **Peer scan** — `check_peers()` reads every *other* node's row; a peer
   whose `last_alive` is older than `PEER_STALE_MINUTES` is reported down
   to the ops Slack channel via `notify_degraded`.

The thread runs through the daemon's off-hours sleeps (separate thread),
so the heartbeat reflects "process alive", not "actively working".
`down_alerted_at` is an atomic claim+cooldown marker: the peer that wins
the `filter(...).update(down_alerted_at=now)` posts (so N peers don't all
alert for one outage), and the row is re-claimable only after
`DEGRADED_REALERT_HOURS`. `last_alive = NULL` means intentionally stopped —
the daemon calls `clear_heartbeat()` on a clean empty-queue exit so peers
don't false-alarm. **Coverage needs ≥2 daemons running**: a lone daemon
has no peer to watch it (an accepted limitation).

**Sender activity — "is the alive sender actually progressing".** The same
`NodeMonitor` tick also runs `check_expected_sender_activity()`. Expected
senders come from `EXPECTED_OUTBOUND_SENDERS` when set, otherwise from active
LinkedIn profiles that own active campaigns. After
`SENDER_ACTIVITY_GRACE_MINUTES` from the active-day start, an expected sender
with work should have outbound `ActionLog` rows. A fresh heartbeat plus stale
due outbound work and no recent `ActionLog` for
`SENDER_ACTIVITY_STALE_MINUTES` alerts as "outbound activity looks stuck".
Before declaring a sender stuck, the checker calls
`LinkedInProfile.can_execute()` for the due action types. If the sender is
blocked by the daily/weekly connect or follow-up limit, it alerts as "hit a
rate limit" and does not classify the outbound lane as stuck.
`DaemonHeartbeat.activity_alerted_at` is the atomic cooldown marker for this
class of alert. This separates healthy cap exhaustion from "monitor thread is
alive but the outbound lane is not making progress", which plain heartbeat
liveness cannot see.

**Degraded detection — "alive but not working".** Runs inside the daemon,
which is the only thing that can observe its own state (`degraded.py`):

- **`TaskFailureTracker`** — an in-process consecutive-failure counter
  wired into the daemon's task-dispatch loop (`record_success()` /
  `record_failure()` around each handler call). One instance per process,
  so it is sender-scoped by construction — no DB query, no `Task.operator`
  column. `TASK_FAILURE_STREAK_THRESHOLD` failures in a row → one alert.

Listener heartbeat files are still written for startup catch-up, but there is
no self-alerting "realtime listener looks stuck" check. All monitoring alerts
route to the ops Slack channel (`SLACK_WEBHOOK_URL`). Monitoring is an
enhancement — tick exceptions are logged and never crash the outreach daemon.

## Configuration

- **`.env`** (project root) — `LLM_API_KEY` (required), `AI_MODEL` (required), `LLM_API_BASE` (optional). For Docker, pass via `docker run -e`.
- **`conf.py` schedule** — `ACTIVE_START_HOUR` (9), `ACTIVE_END_HOUR` (17), `ACTIVE_TIMEZONE` ("UTC"), `REST_DAYS` ((5, 6) = Sat+Sun). Daemon sleeps outside this window.
- **`conf.py` realtime** — `ENABLE_REALTIME_LISTENER` (default `false`), `LISTENER_CDP_PORT` (default 9222, localhost-only), `LISTENER_CATCHUP_GAP_MINUTES` (30), `LISTENER_PUMP_SLICE_SECONDS` (30), `LISTENER_ACTIVE_START_HOUR` (0), `LISTENER_ACTIVE_END_HOUR` (24), `LISTENER_REST_DAYS` (empty).
- **`conf.py` node monitoring** — `ENABLE_NODE_MONITOR` (default `true`), `MONITOR_INTERVAL_SECONDS` (300), `PEER_STALE_MINUTES` (15), `DEGRADED_REALERT_HOURS` (6), `TASK_FAILURE_STREAK_THRESHOLD` (5), `EXPECTED_OUTBOUND_SENDERS` (empty → infer), `SENDER_ACTIVITY_GRACE_MINUTES` (60), `SENDER_ACTIVITY_STALE_MINUTES` (90).
- **`conf.py:CAMPAIGN_CONFIG`** — `min_ready_to_connect_prob` (0.9), `min_positive_pool_prob` (0.20), `connect_delay_seconds` (10), `connect_no_candidate_delay_seconds` (300), `check_pending_recheck_after_hours` (24), `check_pending_jitter_factor` (0.2), `qualification_n_mc_samples` (100), `enrich_min_interval` (1), `min_action_interval` (120), `embedding_model` ("BAAI/bge-small-en-v1.5").
- **Prompt templates** (at `linkedin/templates/prompts/`) — `qualify_lead.j2` (temp 0.7), `search_keywords.j2` (temp 0.9), `follow_up_agent.j2`.
- **`requirements/`** — `base.txt`, `local.txt`, `production.txt`, `crm.txt` (empty — DjangoCRM installed via `--no-deps`).

## Docker

Base image: `mcr.microsoft.com/playwright/python:v1.55.0-noble`. VNC on port 5900. `BUILD_ENV` arg selects requirements. Dockerfile at `compose/linkedin/Dockerfile`. Install: uv pip → DjangoCRM `--no-deps` → requirements → Playwright chromium.

## CI/CD

- `tests.yml` — pytest in Docker on push to `master` and PRs.
- `deploy.yml` — Tests → build + push to `ghcr.io/eracle/openoutreach`. Tags: `latest`, `sha-<commit>`, semver.

## Dependencies

`requirements/` files. DjangoCRM's `mysqlclient` excluded via `--no-deps`. `uv pip install` for fast installs.

Core: `playwright`, `playwright-stealth`, `Django`, `django-crm-admin`, `pandas`, `langchain`/`langchain-openai`, `jinja2`, `pydantic`, `jsonpath-ng`, `tendo`, `termcolor`, `tenacity`, `requests`
ML: `scikit-learn`, `numpy`, `fastembed`, `joblib`
