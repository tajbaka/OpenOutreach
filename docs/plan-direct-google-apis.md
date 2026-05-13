# Plan: Direct Google APIs (no MCP, no DB cache for Gmail/Calendar/Drive)

## Why

Current architecture caches Gmail/Calendar/Gemini data in DB (`crm.Message source=gmail`, `crm.Meeting`) via the data-sync workflow. Two things motivate revisiting:

1. **Only one consumer.** Just the followup workflow reads this data. The "multiple consumers benefit from cache" argument is theoretical until we add a second consumer (analytics, dashboard, second drafting flow).
2. **MCP account-switching is awkward.** One Google account per Claude session; multi-operator support needs self-hosted MCPs or Cloudflare-hosted Workers (~4-8 hr setup) — meaningful infra for one workflow.

API quotas are not a constraint (Gmail = 1B units/day, we'd use ~600 per run).

## What changes

| What | Action |
|---|---|
| MCP-driven Gmail / Calendar / Drive ingest | **Remove.** Replaced by Python google-api-python-client calls inside followup. |
| `crm.Meeting` model + migration | **Remove.** No DB persistence of meetings. |
| `linkedin/notifications/calendar_events.py` | **Remove.** |
| `crm.Message source=gmail` rows | **Keep existing data**, stop adding new ones. Eventually deletable, but harmless. |
| `data-sync-workflow.md` | **Slim.** Keep only People-tab AI Notes synthesis + Outreach status flips. No DB writes. |
| `followup-generation-workflow.md` Phase 1 | **Add inline Google API fetch** — pull Gmail threads + Calendar events + Drive Gemini notes fresh per Met-cohort lead. ~20s added to followup runtime. |
| `WorkflowRun` model + Phase 0.5 staleness check | **Keep.** Still useful for tracking import_connections / backfill_messages cadence per operator. Drop the `data-sync` entry from staleness check (less meaningful when followup is self-sufficient). |
| Account assertion preflight (data-sync Phase -1) | **Move to followup.** Followup's Phase 0 asserts the operator whose Google account is being queried matches the run scope. |

## What gets added

- `linkedin/notifications/google_apis.py` — single module with:
  - `GoogleApis(operator)` class: loads OAuth token from `data/google-tokens-<operator>.json`
  - `gmail_search_threads(email)`, `calendar_list_events(time_range)`, `drive_search_gemini_notes(query)`, `drive_read_file(file_id)`
  - Uses `google-auth` + `google-api-python-client` + `google-auth-oauthlib`
- `data/google-tokens-chuka.json` + `data/google-tokens-arian.json` — OAuth refresh tokens. Generated via one-time `manage.py google_oauth --operator chuka` setup command.
- `manage.py google_oauth` — bootstrap command: opens browser, runs OAuth consent flow, saves refresh token to disk.
- Google Cloud project + OAuth client ID setup (one-time, by you):
  - Create project on console.cloud.google.com
  - Enable Gmail API, Calendar API, Drive API
  - Create OAuth 2.0 Client ID (Desktop app)
  - Download `credentials.json`, save at repo root or `data/google-oauth-client.json`

## Migration order (4 PRs again)

### PR A — google_apis module + OAuth bootstrap
1. Add `google-api-python-client`, `google-auth-oauthlib` to `requirements/base.txt`.
2. Write `linkedin/notifications/google_apis.py` with the `GoogleApis(operator)` class and four methods.
3. Write `manage.py google_oauth --operator <name>` bootstrap command.
4. Run OAuth flow once for Chuka, once for Arian. Verify both token files work via a smoke test (`GoogleApis("Chuka").gmail_search_threads("test@example.com")`).

### PR B — Followup consumes google_apis directly
1. Update `followup-generation-workflow.md` Phase 0 — add operator assertion (similar to data-sync's old preflight but inside followup now).
2. Update Phase 1 `_build_row` for Met cohort:
   - Call `google_apis.calendar_list_events(...)` and find matching events for `lead.email`
   - For each match, call `google_apis.drive_search_gemini_notes(...)` and `drive_read_file(...)` to pull raw notes
   - Surface these into `row["latest_meeting_*"]` fields directly from API response, not from DB
3. Optionally Phase 0.5 staleness check drops `data-sync` from its checklist.
4. Keep `WorkflowRun` writes for followup, import_connections, backfill_messages.

### PR C — Slim down data-sync workflow
1. Remove all DB-write phases (no more `persist_calendar_events`, no `persist_gemini_notes`).
2. Keep only: pull data → synthesize AI Notes prose → write to People tab (Outreach status + AI Notes column).
3. Drop `data-sync` from `WorkflowRun.name` enum *if* we decide it's now too lightweight to track (operator can just observe the People tab last-synced timestamp).
4. Rename `data-sync-workflow.md` back to something like `sheets-enrichment-workflow.md` — its scope is now strictly the People-tab side, not DB ingest.

### PR D — Cleanup
1. Drop `crm.Meeting` model (migration to delete table).
2. Drop `linkedin/notifications/calendar_events.py`.
3. Drop `Message.Source.CALENDAR` enum value (no rows of this type exist anyway).
4. Update CLAUDE.md, ARCHITECTURE.md, human-workflows.md to reflect the simpler architecture.

## Estimated effort

- PR A: 4-6 hours (one-time OAuth setup + module + tests)
- PR B: 2-3 hours (modify Phase 1 to call APIs instead of DB)
- PR C: 1-2 hours (slim down docs, no real code change to data-sync since it's a workflow doc)
- PR D: 1 hour (delete files, migration, doc sweep)

Total: ~10-12 hours. About 2x the cost of what we already shipped (PR 1-4 today), but a fundamentally simpler runtime architecture.

## When this is worth doing

- You're running followup more than weekly and the manual data-sync run before each is annoying.
- You want multiple operators working in parallel (Cloudflare/self-hosted MCP would be the alternative; direct API is simpler).
- You'd cron data-sync's AI-Notes synthesis (still possible with this — it'd be a Python script using google_apis directly).

## When this is NOT worth doing

- Data-sync is a weekly task and the human-in-the-loop is fine.
- You're about to add a second DB consumer (the cache then pays off).
- You're already comfortable with the MCP flow.

## Risks

- **OAuth refresh token expiry**: Google refresh tokens for "Desktop" OAuth client type can expire if unused for 6 months. Mitigation: data-sync running weekly keeps them fresh.
- **API behavior drift**: Google's APIs evolve (deprecation notices). MCP servers often absorb these; direct integration means you maintain them. Low frequency though.
- **First-time setup friction**: OAuth consent flows can be confusing. Worth keeping a 5-line setup README in `data/google-tokens-README.md`.

## Decision deferred

You said "fk it for now" — so this plan sits here unimplemented. Revisit if:
- You add a non-followup consumer for Gmail/Calendar data
- You need multi-operator data-sync runs more than ~monthly
- You want to fully decouple followup from MCP infrastructure
