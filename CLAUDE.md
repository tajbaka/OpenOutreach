# CLAUDE.md

## Rules

- **Python env**: Always use `.venv/bin/python` (not system `python3`).
- **Commits**: No `Co-Authored-By` lines. Single-line messages (no body).
- **Dependencies**: Managed in `requirements/*.txt` (used by local dev and Docker).
- **Docs sync**: When modifying code, update CLAUDE.md and ARCHITECTURE.md to reflect changes.
- **No memory**: Never use the auto-memory system (no MEMORY.md, no memory files). All persistent context belongs in CLAUDE.md or ARCHITECTURE.md.
- **Error handling**: App should crash on unexpected errors. `try/except` only for expected, recoverable errors. Custom exceptions in `exceptions.py`.
- **No backward compat**: CRM models are owned by this project — no need for backward compatibility shims, legacy migration code, or re-export modules. Simplify freely.

## Project Overview

OpenOutreach — self-hosted LinkedIn automation for B2B lead generation. Playwright + stealth for browser automation, LinkedIn Voyager API for profile data, Django + Django Admin for CRM (models owned by this project).

## Commands

```bash
# Docker
make build / make up / make stop / make attach / make up-view

# Local dev
make setup    # install deps + browsers + migrate + bootstrap CRM
make run      # run daemon
make admin    # Django Admin at localhost:8000/admin/

# Testing
make test / make docker-test
pytest tests/api/test_voyager.py   # single file
pytest -k test_name                # single test

# Google Sheets CRM sync (mirrors Deal state to the People tab — single tab,
# one row per Lead, keyed by LinkedIn URL)
.venv/bin/python manage.py sync_sheets --campaign 1   # one campaign
.venv/bin/python manage.py sync_sheets                # all campaigns
.venv/bin/python manage.py sync_sheets --dry-run      # show plan, no writes

# Resync crm.Message from LinkedIn DM threads. Standalone runner — auto-runs one pass per
# env-configured LinkedIn account. Each pass logs in via StandaloneLinkedInSession (cookies
# cached per account at data/<label>_cookies.json so subsequent runs skip re-auth), asks
# LinkedIn who it is, then filters Leads to threads with that sender.
# Configure in .env:
#   LINKEDIN_USERNAME + LINKEDIN_PASSWORD                    (primary account)
#   BACKFILL_LINKEDIN_USERNAME + BACKFILL_LINKEDIN_PASSWORD  (backfill account)
# Set both pairs to sync both accounts; either pair alone is fine if you only have one.
# Run periodically (cron) since the daemon stops watching threads after the initial accept.
.venv/bin/python manage.py backfill_messages [--campaign 1] [--limit 50] [--dry-run]

# Backfill from CSV using a separate LinkedIn account (does not touch the daemon)
.venv/bin/python manage.py import_connections \
  --csv leads/linkedin-batch4-messages.csv \
  --since-days 90 \
  [--dry-run]
# Account creds resolved from BACKFILL_LINKEDIN_USERNAME/PASSWORD in .env
# (no --handle flag — the env vars decide which account logs in).
```

`import_connections` reads a CSV with either of these header sets:
- `LinkedIn URL, First Name, Message` (manual outreach log; `Message` becomes the seeded outbound `crm.Message`)
- `Profile URL, First Name, Last Name, Company` (standard LinkedIn-Connections export; no outbound message column)

The parser auto-detects which format and treats `Message` / `Last Name` / `Company` as optional bonuses. It logs into LinkedIn using `BACKFILL_LINKEDIN_USERNAME`/`BACKFILL_LINKEDIN_PASSWORD` from `.env`, scrapes the Connections page back N days, and creates Lead+Deal rows at `state=CONNECTED` for each row also present in the scraped connections. The SKIP path (lead already at CONNECTED+) backfills `Deal.connected_at` from the scrape's `connected_on` date if it was null. Skips URLs that already have a Deal in any non-backfill campaign so the daemon's outreach state is never disturbed. Outbound messages from the CSV are persisted to `crm.Message` with `external_id="csv:<path>:<url>"`; inbound messages flow through `get_conversation`'s persist hook. `sync_sheets` mirrors these to Google Sheets on its next hourly run — this command never touches Sheets directly.

## Architecture (quick reference)

For detailed module docs, see `ARCHITECTURE.md`.

- **Entry**: `manage.py` — no args runs daemon (onboarding → browser → task queue loop); with args delegates to Django CLI. Auto-migrates + CRM bootstrap on startup.
- **State machine**: `enums.py:ProfileState` — QUALIFIED → READY_TO_CONNECT → PENDING → CONNECTED → COMPLETED / FAILED. Deal.state is a CharField with ProfileState choices (no Stage model). `ClosingReason` (COMPLETED/FAILED/DISQUALIFIED) on Deal.closing_reason. `Lead.disqualified=True` = permanent exclusion. LLM rejections = FAILED Deals with DISQUALIFIED closing reason (campaign-scoped). `Deal.connected_at` is stamped once when `set_profile_state` flips into CONNECTED — stable timestamp the daemon's rigid follow-up handler (`linkedin/tasks/follow_up.py`) uses for connected-no-reply cohort tracking.
- **Task queue**: `Task` model (persistent). Types: `connect`, `follow_up`, `sweep_connections`. Handlers in `linkedin/tasks/`, signature: `handle_*(task, session, qualifiers)`. `sweep_connections` replaces the legacy per-profile `check_pending` — one visit to `mynetwork/invite-connect/connections/` every `CONNECTION_SWEEP_INTERVAL_HOURS` cross-references accepted invitations in bulk. Independently gated by `ENABLE_SWEEP_CONNECTIONS`; the post-accept follow-up DM is gated separately by `ENABLE_FOLLOW_UP`, so you can detect accepts (and mirror to Sheets) without auto-DMing.
- **ML pipeline**: GPR (sklearn) + BALD active learning + LLM qualification. Per-campaign models stored in `Campaign.model_blob` (DB).
- **Config**: `.env` (LLM_API_KEY, AI_MODEL, SLACK_WEBHOOK_URL, GOOGLE_SHEETS_ID, GOOGLE_SHEETS_CREDENTIALS_PATH, optional GOOGLE_SHEETS_TAB_NAME, OUR_COMPANY_NAME, OUR_WEBSITE_URL), `conf.py:CAMPAIGN_CONFIG` (timing/ML defaults), `conf.py` browser constants (`BROWSER_*`, `HUMAN_TYPE_*`, `VOYAGER_REQUEST_TIMEOUT_MS`), `conf.py` schedule constants (`ENABLE_ACTIVE_HOURS` flag, active hours/timezone/rest days), `conf.py` onboarding defaults (`DEFAULT_*_LIMIT`), Campaign/LinkedInProfile models (Django Admin).
- **Database**: Postgres on Neon when `DATABASE_URL` is set in `.env`, falls back to local SQLite for offline dev. `linkedin/django_settings.py` switches based on env. Daemon machine + dev box MUST share `DATABASE_URL` to avoid split-brain (lesson from 2026-04-26 cutover). Deps: `dj-database-url`, `psycopg[binary]>=3.1`.
- **Sheets sync**: standalone `manage.py sync_sheets` (gspread + service-account auth). Iterates Deals at `state >= PENDING` and **disqualified=False**, groups by `company_name`, and writes one row per Lead into a single People tab keyed by LinkedIn URL. 15 columns: Name, First name, Last name, Company, Title, LinkedIn URL, Email addresses, Outreach status, Stage, Priority, Primary location, Notes, AI Notes, Created at, Last synced. The Stage column is the company-level aggregate (`aggregate_company_stage`: Won wins; furthest-along active stage; all-Lost → Lost) denormalized onto each row. Per-lead stage derivation: PENDING/CONNECTED-no-reply→Prospecting, CONNECTED+last_reply_at→Qualification, COMPLETED→Won, FAILED→Lost. Stage hierarchy: Prospecting → Qualification → Meeting → Closing → Won (Lost terminates). Meeting and Closing are human-driven; `should_patch_stage` prevents downgrades. **Outreach status rank**: Invite Sent → Connected → Waiting → Replied → Wants Meeting → Meeting Booked → Had Meeting → Manual followup → Prospecting to close → Won → Don't send (Lost is terminal-negative, separately overridable). `should_patch_outreach_status` blocks demotion so manual sheet edits are never clobbered. **Priority** defaults to "Low" when empty so the column never has blanks. The Lead model has no per-row sheet pointer — `LinkedIn URL` is the natural key, and `SheetIndex.load()` slurps the whole tab in one API call per sync run. Decoupled from the daemon — failures don't affect outreach. Module: `linkedin/notifications/sheets.py`.
- **Synthesis pass (Phase D)**: `sync_sheets`'s per-Lead loop runs `linkedin.notifications.synthesis.synthesize_for_deal(deal, current_outreach_status=...)` between reading the existing sheet row and writing the new one. **D1**: regex-extracts an email from inbound `crm.Message` rows and writes it to `Lead.email`; sync_sheets folds the union of Lead.email + existing sheet emails into the row payload. **D2**: runs a cheap LLM (`AI_MODEL`) over the thread; returns a `SynthResult(wants_meeting_now, note_block)` that sync_sheets folds into the row's Outreach status + Notes columns. Gated by `Deal.wants_meeting_detected_at` (lock-in), `Deal.last_synthesized_at` vs latest `Message.sent_at` (skip when no new signal), and current sheet status rank ≥ Wants Meeting (skip when human already advanced past it). Synthesis failures are logged and never block the sync.
- **Message store**: `crm.Message` (FK to Lead, source enum {linkedin/gmail/calendar}, direction {inbound/outbound}, idempotent on `(source, external_id)`). Populated as a side effect of `linkedin.actions.conversations.get_conversation()` — every existing caller (sweep_connections, follow_up, agent) auto-persists threads via `linkedin.db.messages.persist_thread`. `import_connections` also seeds outbound messages directly from CSVs. Gmail threads land via `linkedin.notifications.gmail_threads.persist_gmail_threads` — the data-sync workflow (`docs/data-sync-workflow.md` Phase 0) hands MCP-fetched thread payloads to it; direction is inferred against a `self_emails` set the caller resolves at run start (primary mailbox + Send-As aliases from Gmail Profile API). The same module's `classify_ball_on_court(lead)` reads the merged LinkedIn+Gmail timeline so an email reply correctly flips a LinkedIn-silent lead to "ball on us".
- **Rigid ICP outbound templates**: `linkedin/icp_messages.json` holds per-ICP rigid follow-up messages (one per ICP × channel). Used by the daemon's `handle_follow_up` task to DM connected-no-reply leads programmatically — high-volume blast with no LLM personalization, only mechanical `{first_name}` / `{company_name}` / `{our_company_name}` / `{our_website_url}` / `{my_name}` substitutions. Loader / filler: `linkedin/icp_outbound.py`. Companion to the Sheets `Followup Templates` tab which carries an AI-filled `{Add personal message}` span for warm cohorts (Met / ball-on-us) generated by the Claude-run followup workflow. ROLE→ICP routing reuses `FU_ROLE_TO_ICP`.
- **Django apps**: `linkedin` (main — Campaign with users M2M), `crm` (Lead with embedding/Deal/Message), `chat` (ChatMessage).
- **Docker**: Playwright base image, VNC on port 5900, `BUILD_ENV` arg selects requirements.
- **CI/CD**: `.github/workflows/tests.yml` (pytest), `deploy.yml` (build + push to ghcr.io).
