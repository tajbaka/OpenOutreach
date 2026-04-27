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

# Attio CRM sync (mirrors Deal state to the Sales list)
.venv/bin/python manage.py sync_attio --campaign 1   # one campaign
.venv/bin/python manage.py sync_attio                # all campaigns
.venv/bin/python manage.py sync_attio --dry-run      # show plan, no writes

# Slack notifications for accepted invites (separate from sync_attio)
.venv/bin/python manage.py check_connections
```

## Architecture (quick reference)

For detailed module docs, see `ARCHITECTURE.md`.

- **Entry**: `manage.py` — no args runs daemon (onboarding → browser → task queue loop); with args delegates to Django CLI. Auto-migrates + CRM bootstrap on startup.
- **State machine**: `enums.py:ProfileState` — QUALIFIED → READY_TO_CONNECT → PENDING → CONNECTED → COMPLETED / FAILED. Deal.state is a CharField with ProfileState choices (no Stage model). `ClosingReason` (COMPLETED/FAILED/DISQUALIFIED) on Deal.closing_reason. `Lead.disqualified=True` = permanent exclusion. LLM rejections = FAILED Deals with DISQUALIFIED closing reason (campaign-scoped).
- **Task queue**: `Task` model (persistent). Types: `connect`, `follow_up`, `sweep_connections`. Handlers in `linkedin/tasks/`, signature: `handle_*(task, session, qualifiers)`. `sweep_connections` replaces the legacy per-profile `check_pending` — one visit to `mynetwork/invite-connect/connections/` every `CONNECTION_SWEEP_INTERVAL_HOURS` cross-references accepted invitations in bulk.
- **ML pipeline**: GPR (sklearn) + BALD active learning + LLM qualification. Per-campaign models stored in `Campaign.model_blob` (DB).
- **Config**: `.env` (LLM_API_KEY, AI_MODEL, SLACK_WEBHOOK_URL, ATTIO_API_KEY, ATTIO_SALES_LIST_ID), `conf.py:CAMPAIGN_CONFIG` (timing/ML defaults), `conf.py` browser constants (`BROWSER_*`, `HUMAN_TYPE_*`, `VOYAGER_REQUEST_TIMEOUT_MS`), `conf.py` schedule constants (`ENABLE_ACTIVE_HOURS` flag, active hours/timezone/rest days), `conf.py` onboarding defaults (`DEFAULT_*_LIMIT`), Campaign/LinkedInProfile models (Django Admin).
- **Database**: Postgres on Neon when `DATABASE_URL` is set in `.env`, falls back to local SQLite for offline dev. `linkedin/django_settings.py` switches based on env. Daemon machine + dev box MUST share `DATABASE_URL` to avoid split-brain (lesson from 2026-04-26 cutover). Deps: `dj-database-url`, `psycopg[binary]>=3.1`.
- **Attio sync**: standalone `manage.py sync_attio` (REST, not MCP). Iterates Deals at `state >= PENDING`, **groups by company_name**, and mirrors to the Sales list (parent: companies). One Company + one Sales list entry per company; one Person per individual lead, all linked to the company. Entry's Stage = `aggregate_company_stage` of all leads' stages (Won wins outright; otherwise furthest-along active stage; all-Lost → Lost). Per-lead stage map: PENDING/CONNECTED-no-reply→Prospecting, CONNECTED+last_reply_at→Qualification, COMPLETED→Won, FAILED→Lost. Meeting is human-driven (`should_patch_stage` prevents downgrades). Attio IDs persisted on `Lead.attio_{person,company,entry}_id` for idempotency — peer-lookup within a company group reuses Company/entry IDs. Decoupled from the daemon — failures don't affect outreach. Module: `linkedin/notifications/attio.py`.
- **Django apps**: `linkedin` (main — Campaign with users M2M), `crm` (Lead with embedding/Deal), `chat` (ChatMessage).
- **Docker**: Playwright base image, VNC on port 5900, `BUILD_ENV` arg selects requirements.
- **CI/CD**: `.github/workflows/tests.yml` (pytest), `deploy.yml` (build + push to ghcr.io).
