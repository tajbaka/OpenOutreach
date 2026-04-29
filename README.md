![OpenOutreach Logo](docs/logo.png)

> Self-hosted LinkedIn outreach automation with first-class Attio CRM sync. Send connection requests, capture replies, mirror state to Attio, and use companion Claude workflows for follow-up drafting and post-meeting enrichment.

<div align="center">

[![License: GPLv3](https://img.shields.io/badge/License-GPLv3-blue.svg?style=flat-square)](https://www.gnu.org/licenses/gpl-3.0)

<br/>

# Demo:

<img src="docs/demo.gif" alt="Demo Animation" width="100%"/>

</div>

---

## What this is

A long-running daemon that runs LinkedIn outreach inside a stealth Playwright browser, plus a Postgres-backed CRM that mirrors deal state to Attio's Sales list. Built on top of the upstream `eracle/OpenOutreach` ML pipeline (Bayesian active learning for lead qualification), with the surface area extended for real B2B sales workflows: multi-account outreach, message-thread persistence, hourly Attio sync with don't-clobber stage logic, LLM-driven email extraction and meeting-intent detection, and Claude-driven runbooks for human-in-the-loop follow-up drafting.

**Core loop:**

1. **Daemon** connects to qualified leads (Voyager API + Playwright)
2. **Sweep** runs every 6h, detects accepted invites in bulk, captures the first DM reply
3. **`crm.Message`** persists every LinkedIn DM thread (idempotent on `external_id`)
4. **`sync_attio`** (cron'd hourly) mirrors Deals → Attio Sales list, with stage and outreach-status patching that won't downgrade manual changes
5. **Synthesis pass** (inside `sync_attio`) extracts email addresses from inbound messages and runs a cheap LLM to flag "wants meeting" intent
6. **Backfill** (`backfill_messages` on cron) keeps `crm.Message` fresh after the daemon stops watching threads
7. **Companion Claude workflows** (interactive, MCP-driven) generate follow-up drafts and enrich Attio with cross-source meeting context

The Bayesian ML qualifier is still there for autonomous lead discovery, but most teams running this will already have a lead list — the bulk of value is now in the Attio sync, message store, and human workflows.

---

## What you need

| # | What | Example |
|---|------|---------|
| 1 | LinkedIn account(s) | Primary outreach account; optional separate "backfill" account for CSV imports |
| 2 | LLM API key | Used for qualification + synthesis (cheap models work for synthesis, e.g., `gemini-2.5-flash`) |
| 3 | Postgres | Neon recommended; SQLite fallback works for dev |
| 4 | (Optional) Attio API key + Sales list ID | Required if you want CRM sync |
| 5 | (Optional) Slack webhook | For accepted-invite notifications |

---

## Quick start (Docker)

Pre-built images on GitHub Container Registry:

```bash
docker run --pull always -it -p 5900:5900 -v openoutreach_db:/app ghcr.io/eracle/openoutreach:latest
```

Connect a VNC client to `localhost:5900` to watch the browser. The interactive onboarding walks you through credentials and campaign setup on first run.

For Compose / build-from-source see [`docs/docker.md`](docs/docker.md).

---

## Local installation (development)

### Prerequisites

- Git
- Python 3.12+

### Setup

```bash
git clone https://github.com/tajbaka/OpenOutreach.git
cd OpenOutreach

# Install deps + Playwright browsers + migrations + CRM bootstrap
make setup

# Run the daemon (interactive onboarding on first run)
make run

# Browse the CRM (Django Admin)
python manage.py createsuperuser
make admin
# → http://localhost:8000/admin/
```

---

## Architecture (quick reference)

For module-level detail see [`CLAUDE.md`](CLAUDE.md) (kept current alongside code changes). For the live operational picture (what's running on your box right now given your `.env` flags) see [`docs/system-flow.txt`](docs/system-flow.txt).

**Entry point:** `manage.py` — no args runs the daemon. With args, delegates to Django CLI. Auto-migrates and bootstraps CRM on startup.

**State machine** (`enums.py:ProfileState`):

```
QUALIFIED → READY_TO_CONNECT → PENDING → CONNECTED → COMPLETED / FAILED
```

`Deal.state` is a CharField with these choices; `Deal.closing_reason` (COMPLETED / FAILED / DISQUALIFIED) closes out the lifecycle. `Lead.disqualified=True` is a permanent exclusion.

**Task queue** (`linkedin/models.py:Task`):

| Task type | What it does |
|---|---|
| **`connect`** | Sends invite + initial note; persists outbound to `crm.Message`. Gated by daily/weekly limits per profile. |
| **`sweep_connections`** | Visits the connections page every 6h (configurable), bulk-detects accepts, transitions PENDING → CONNECTED, captures first reply, posts Slack. Replaces the legacy per-profile `check_pending`. |
| **`follow_up`** | Runs the multi-turn LLM agent on connected leads. Gated by `ENABLE_FOLLOW_UP` — when off, queued tasks are cancelled at startup. |
| **`check_pending`** | Legacy task type, retained for migration compatibility. New deployments should use `sweep_connections`. |

**Storage:**
- **Postgres** (Neon recommended) when `DATABASE_URL` is set; SQLite fallback for offline dev. Daemon and dev box must share the same `DATABASE_URL` to avoid split-brain.
- **`crm.Message`** is the canonical DM history store (FK to Lead, source enum {linkedin, gmail, calendar}, direction {inbound, outbound}, idempotent on `(source, external_id)`).
- Per-campaign GP models live in `Campaign.model_blob` (binary BLOB, not files).

**Attio sync** (`linkedin/notifications/attio.py`):
- Standalone command: `manage.py sync_attio` (REST, not MCP).
- Iterates Deals at `state >= PENDING`, groups by `company_name`, mirrors to the Sales list as one Company + one Sales entry + one Person per Lead, all linked.
- Stage hierarchy: `Prospecting → Qualification → Meeting → Closing → Won` (Lost terminates).
- Outreach status hierarchy: `Invite Sent → Connected → Replied → Wants Meeting → Meeting Booked → Had Meeting → Prospecting to close → Won`.
- `should_patch_stage` and `should_patch_outreach_status` block downgrades, so manual Attio edits are never clobbered.
- Decoupled from the daemon — Attio failures don't affect outreach.

**Synthesis pass** (`linkedin/notifications/synthesis.py`, runs inside `sync_attio`):
- **D1 email extract:** regex over inbound `crm.Message` rows, appends to `Lead.email` and the Attio Person's `email_addresses`.
- **D2 wants-meeting LLM:** cheap LLM (configured via `AI_MODEL`) reads the thread; if meeting intent detected, patches Outreach status to "Wants Meeting" and POSTs an auto-detected note to the Person.
- Gated by `Deal.wants_meeting_detected_at` (lock-in) and `Deal.last_synthesized_at` vs latest message timestamp (skip when no new signal).

---

## Companion Claude workflows

Two interactive runbooks driven by Claude that sit on top of the data the automation produces. See [`docs/human-workflows.md`](docs/human-workflows.md) for the full picture.

| Workflow | Purpose |
|---|---|
| [`docs/followup-generation-workflow.md`](docs/followup-generation-workflow.md) | Generate per-prospect follow-up drafts from `crm.Message` + Gmail + Calendar + Drive. Output goes to `followups/YYYY-MM-DD/*.txt` for you to paste. Ball-on-court classifier supports daily runs. |
| [`docs/attio-meeting-sync-workflow.md`](docs/attio-meeting-sync-workflow.md) | Enrich Attio People with cross-source meeting context (calendar + Gmail + Drive Gemini notes), update Outreach status and Entry stage, compose AI Notes. Preview-first; you approve before any Attio write. |

These don't run on cron. You run them in conversation with Claude when you need them.

---

## Common commands

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
.venv/bin/python manage.py sync_attio --campaign 1
.venv/bin/python manage.py sync_attio
.venv/bin/python manage.py sync_attio --dry-run

# Resync crm.Message from LinkedIn DM threads (run on cron)
.venv/bin/python manage.py backfill_messages [--campaign 1] [--limit 50] [--dry-run]

# Bulk-import existing connections from CSV via a separate "backfill" account
.venv/bin/python manage.py import_connections \
  --csv leads/linkedin-batch4-messages.csv \
  --handle backfill-account@example.com \
  --since-days 90 \
  [--dry-run]
```

---

## Configuration

Configured via `.env` and the Campaign / LinkedInProfile models in Django Admin. See [`docs/configuration.md`](docs/configuration.md) for the full reference.

**Key feature flags:**

| Flag | Default | Purpose |
|---|---|---|
| `ENABLE_SWEEP_CONNECTIONS` | `true` | Bulk accept-detection task |
| `ENABLE_FOLLOW_UP` | `true` | Auto-DM after accept (set `false` if you want to write follow-ups by hand) |
| `ENABLE_ACTIVE_HOURS` | `false` | Restrict daemon to a daily window |
| `ENABLE_AUTO_DISCOVERY` | `false` | Autonomous lead-search via the ML pipeline |
| `CONNECTION_SWEEP_INTERVAL_HOURS` | `2` | How often the sweep task fires |
| `AI_MODEL` | `gpt-4o` | Used for both qualification and synthesis (cheap models work fine for synthesis) |
| `DATABASE_URL` | (unset → SQLite) | Postgres connection string |
| `ATTIO_API_KEY` + `ATTIO_SALES_LIST_ID` | (unset → no sync) | Required for Attio mirroring |
| `SLACK_WEBHOOK_URL` | (unset → no Slack) | Notifications when sweep detects accepts |

---

## Project structure

```
├── docs/
│   ├── configuration.md                # Configuration reference
│   ├── system-flow.txt                 # Operational state of your deployment
│   ├── human-workflows.md              # Overview of the two Claude runbooks
│   ├── followup-generation-workflow.md # Drafts: replied / connected-no-reply / met cohorts
│   ├── attio-meeting-sync-workflow.md  # Attio enrichment from calendar + Gmail + Drive
│   ├── docker.md                       # Docker setup
│   ├── templating.md                   # Follow-up message templating
│   ├── template-variables.md           # Available template variables
│   └── testing.md                      # Test strategy
├── linkedin/
│   ├── actions/                        # Browser actions (connect, message, status, search)
│   ├── agents/                         # ReAct follow-up agent (multi-turn DM)
│   ├── api/                            # Voyager API client + parser
│   ├── browser/                        # Session, login, navigation
│   ├── conf.py                         # .env loading + defaults
│   ├── daemon.py                       # Task queue worker loop
│   ├── db/                             # CRM CRUD (leads, deals, messages, enrichment)
│   ├── django_settings.py              # Django settings (Postgres or SQLite)
│   ├── management/commands/            # backfill_messages, sync_attio, import_connections, ...
│   ├── ml/                             # Bayesian qualifier (GPR), embeddings
│   ├── models.py                       # Campaign, LinkedInProfile, Task, etc.
│   ├── notifications/                  # attio.py, slack.py, synthesis.py
│   ├── onboarding.py                   # First-run interactive setup
│   ├── pipeline/                       # Candidate sourcing + qualification
│   ├── setup/                          # GDPR, self-profile, freemium campaign
│   └── tasks/                          # connect, sweep_connections, follow_up
├── crm/                                # Django app: Lead, Deal, Message
├── chat/                               # Django app: ChatMessage
├── manage.py                           # Entry point (no args = daemon, else Django CLI)
├── local.yml                           # Docker Compose
└── Makefile                            # setup / run / admin / test shortcuts
```

---

## Documentation

- [Module-level architecture (CLAUDE.md)](CLAUDE.md)
- [Configuration](docs/configuration.md)
- [System flow (operational)](docs/system-flow.txt)
- [Human-in-the-loop workflows](docs/human-workflows.md)
- [Follow-up generation runbook](docs/followup-generation-workflow.md)
- [Attio meeting sync runbook](docs/attio-meeting-sync-workflow.md)
- [Docker installation](docs/docker.md)
- [Follow-up templating](docs/templating.md)
- [Template variables](docs/template-variables.md)
- [Testing](docs/testing.md)

---

## License

[GNU GPLv3](https://www.gnu.org/licenses/gpl-3.0). See [`LICENCE.md`](LICENCE.md).

---

## Legal notice

**Not affiliated with LinkedIn.** Built on top of the upstream [`eracle/OpenOutreach`](https://github.com/eracle/OpenOutreach) project (GPLv3).

By using this software you accept the [Legal Notice](LEGAL_NOTICE.md). It covers LinkedIn ToS risks, automated browser behavior, and liability disclaimers.

**Use at your own risk. No liability assumed.**
