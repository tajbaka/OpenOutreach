![OpenOutreach Logo](docs/logo.png)

> Self-hosted LinkedIn outreach automation with a Postgres-backed, Google
> Sheets-operated sales CRM. Capture conversations, maintain a durable People
> ledger, operate concise Active Accounts and Actions views, and draft
> human-reviewed followups.

<div align="center">

[![License: GPLv3](https://img.shields.io/badge/License-GPLv3-blue.svg?style=flat-square)](https://www.gnu.org/licenses/gpl-3.0)

<br/>

# Demo:

<img src="docs/demo.gif" alt="Demo Animation" width="100%"/>

</div>

---

## What this is

A long-running daemon that runs LinkedIn outreach inside a stealth Playwright
browser, plus a Postgres-backed canonical sales CRM operated through Google
Sheets. The CRM separates per-Lead outreach automation Deals from durable
account Opportunities, then publishes only meaningful Active Accounts and
current Actions.

**Core loop:**

1. **Daemon** connects to qualified leads (Voyager API + Playwright)
2. **Sweep** runs every 6h, detects accepted invites in bulk, captures the first DM reply
3. **`crm.Message`** persists every LinkedIn DM thread (idempotent on `external_id`)
4. **Backfill** (`backfill_messages` on its own schedule) keeps later LinkedIn
   replies fresh
5. **`sync_crm_v2_context`** refreshes Gmail/Gemini, validated email-first
   contacts, and Granola without publishing Sheets
6. **`refresh_crm_v2`** reconciles account evidence and atomically publishes
   `Active Accounts` plus one owner-filterable `Actions` queue
7. **`sync_sheets`** remains a narrow incremental People publisher, not a sales
   decision engine
8. **`generate_followups`** exports stable-ID current Actions and applies validated
   drafts without sending messages

The Bayesian ML qualifier is still there for autonomous lead discovery, but most teams running this will already have a lead list — the bulk of value is now in the Sheets sync, message store, and human workflows.

---

## What you need

| # | What | Example |
|---|------|---------|
| 1 | LinkedIn account(s) | Primary outreach account; optional separate "backfill" account for CSV imports |
| 2 | LLM API key | Used for qualification + synthesis (cheap models work for synthesis, e.g., `gemini-2.5-flash`) |
| 3 | Postgres | Required shared `DATABASE_URL`; no runtime SQLite fallback |
| 4 | CRM Sheet ID + service-account JSON | Required for Sheets commands; configure the separate Sales Motion Sheet ID as a read-only account input |
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
.venv/bin/python manage.py createsuperuser
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
| **`discovery`** | On weekdays after sender connection work finishes, or at any time on rest days, scans bounded `/mynetwork/grow/` and one-hop profile recommendations, scores cards against that sender's enabled non-CMMC ICP blocks in `icp_messages.json`, opens profiles at or above `DISCOVERY_VISIT_SCORE_THRESHOLD`, and saves them to `LinkedInDiscoveryLead` up to `DISCOVERY_DAILY_LIMIT`. Never creates CRM or outbound state. |
| **`check_pending`** | Legacy task type, retained for migration compatibility. New deployments should use `sweep_connections`. |

**Storage:**
- **Postgres** is required through `DATABASE_URL`. Every daemon, runner, and dev
  box must use the intended shared database; there is no runtime SQLite
  fallback.
- **`crm.Message`** is the canonical DM history store (FK to Lead, source enum {linkedin, gmail, calendar}, direction {inbound, outbound}, idempotent on `(source, external_id)`).
- **`LinkedInDiscoveryLead`** is the separate discovery collection table. Each globally deduplicated profile stores its structured profile JSON, first storing sender/account, and potential ICP.
- Per-campaign GP models live in `Campaign.model_blob` (binary BLOB, not files).

**Canonical CRM v2 refresh** (`manage.py refresh_crm_v2`):

- `People` is durable and growing: update in place, append once, never
  clear/reorder/prune, and preserve operator columns/formulas/formatting.
- `Active Accounts` is the broad relationship radar: one row per admitted
  account/opportunity with owner, Trello-stage projection (`Radar only` when
  uncurated), attention, evidence, next action, due date, and key contacts.
- `Actions` is one owner-filterable current-work queue. Genuine
  primary/authoritative work can appear as `Unassigned`; LinkedIn-only noise
  does not. There are no separate Sheet Pipeline, Recovery, or sender Followups
  surfaces.
- Granola is primary meeting context; stored Gemini notes are secondary.
- Human Sheet fields round-trip through a conservative three-way merge.
  `Active Accounts.Stage` is a system projection, not an editable Sheet field.
  Invalid or conflicting edits fail closed instead of being guessed.
- Admission prioritizes explicit human/Sales Motion state, real meetings, and
  human Gmail; LinkedIn qualifies only when the exchange is substantive and
  bidirectional. One-sided outbound remains in People.
- Don't send suppresses outreach to that exact contact without erasing account
  relevance.
- Omit `--apply` for an exact rollback-only DB plan with zero Sheet writes.
  First cutover requires a reviewed private preview; scheduled runs use
  `--apply --routine`. The command never sends Gmail or LinkedIn messages.

**Context refresh** (`manage.py sync_crm_v2_context`):

- Refreshes Gmail/Gmail-delivered Gemini, strictly validated email-first Leads,
  and Granola before publication.
- Defaults to no-write; `--apply` persists context but never sends messages.

**Narrow People publisher** (`manage.py sync_sheets`):

- Plans/publishes only the durable People ledger.
- Performs no LLM synthesis, opportunity-stage decisions, or followup
  eligibility.

**Curated Trello pipeline** (`manage.py sync_trello_pipeline`):

- Trello is the only human-operated high-level stage surface; only
  Opportunities with nonblank `pipeline_stage` receive cards.
- The system may promote only a blank stage to `Potential / Triage`, and only
  from authoritative account state or completed-meeting plus human-Gmail
  evidence. It never auto-advances after triage.
- Card identity is a durable DB mapping plus an exact Opportunity UUID footer;
  names are never identity.
- Dry-run is the default. `--apply` writes reviewed changes and the explicit
  `--bootstrap-lists` option creates missing canonical lists. The Trello Free
  design uses lists/card descriptions because Custom Fields are unavailable.

---

## Human-in-the-loop workflows

See [`docs/human-workflows.md`](docs/human-workflows.md) for the full picture.

| Workflow | Purpose |
|---|---|
| [`docs/followup-generation-workflow.md`](docs/followup-generation-workflow.md) | Export explicitly owned due Actions and apply schema-validated drafts by stable ID. |
| [`docs/data-sync-workflow.md`](docs/data-sync-workflow.md) | Ingest Calendar/Drive context not directly fetched by the scheduled CRM refresh. |

Drafting and connector ingestion remain human-reviewed. The canonical CRM
publisher may run on a schedule.

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

# Refresh stored context, then inspect the canonical CRM plan
.venv/bin/python manage.py sync_crm_v2_context --apply
.venv/bin/python manage.py refresh_crm_v2 \
  --manual-pin StackArmor \
  --owner-override Ramp=Arian \
  --owner-override StackArmor=Arian

# Post-cutover routine publication
.venv/bin/python manage.py refresh_crm_v2 --apply --routine \
  --manual-pin StackArmor \
  --owner-override Ramp=Arian \
  --owner-override StackArmor=Arian

# Narrow People publisher diagnostics
.venv/bin/python manage.py sync_sheets --dry-run

# Review/apply the curated Trello pipeline after CRM refresh
.venv/bin/python manage.py sync_trello_pipeline
.venv/bin/python manage.py sync_trello_pipeline --apply

# Resync crm.Message from LinkedIn DM threads (run on cron)
.venv/bin/python manage.py backfill_messages [--campaign 1] [--limit 50] [--dry-run]

# Inspect sender discovery configuration/capacity or enqueue its next bounded run
.venv/bin/python manage.py start_discovery --dry-run
.venv/bin/python manage.py start_discovery [--handle arian]
.venv/bin/python manage.py run_discovery_once --handle arian --max-tasks 3

# Bulk-import existing connections from CSV via a separate "backfill" account
.venv/bin/python manage.py import_connections \
  --csv leads/linkedin-batch4-messages.csv \
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
| `ENABLE_ACTIVE_HOURS` | `true` | Restrict daemon to a daily window |
| `ENABLE_AUTO_DISCOVERY` | `true` | Autonomous lead-search via the legacy ML pipeline |
| `ENABLE_PROFILE_DISCOVERY` | `false` | Separate bounded profile collection after outbound hours/rest days |
| `DISCOVERY_VISIT_SCORE_THRESHOLD` | `70` | Minimum discovery ICP-fit score before opening a recommended profile |
| `CONNECTION_SWEEP_INTERVAL_HOURS` | `2` | How often the sweep task fires |
| `AI_MODEL` | `gpt-4o` | Qualification/drafting model identifier |
| `DATABASE_URL` | required | Shared Postgres connection string; no runtime SQLite fallback |
| `GOOGLE_SHEETS_ID` + `GOOGLE_SHEETS_CREDENTIALS_PATH` | required by Sheets commands | Missing configuration makes `sync_sheets` and `refresh_crm_v2` fail closed |
| `SALES_MOTION_VERSIONS_GOOGLE_SHEETS_ID` | recommended CRM v2 input | Separate read-only workbook whose account tabs are authoritative admission evidence |
| `GRANOLA_API_KEY` | unset | Optional read-only primary meeting-note source |
| `TRELLO_API_KEY` + `TRELLO_API_TOKEN` + `TRELLO_BOARD_ID` | required by Trello sync | Dedicated curated pipeline board credentials/identity |
| `SLACK_WEBHOOK_URL` | (unset → no Slack) | Notifications when sweep detects accepts |

---

## Project structure

```
├── docs/
│   ├── configuration.md                # Configuration reference
│   ├── system-flow.txt                 # Operational state of your deployment
│   ├── human-workflows.md              # Human-operated CRM and outreach workflows
│   ├── crm-refresh-workflow.md          # Canonical CRM deployment/safety runbook
│   ├── followup-generation-workflow.md # Stable-ID Action drafting workflow
│   ├── data-sync-workflow.md            # Calendar/Drive context ingestion
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
│   ├── discovery/                      # ICP config, dynamic gating, search cards, screening, persistence
│   ├── django_settings.py              # Runtime Postgres settings; pytest alone uses in-memory SQLite
│   ├── management/commands/            # context/CRM/Trello sync commands, ...
│   ├── ml/                             # Bayesian qualifier (GPR), embeddings
│   ├── models.py                       # Campaign, LinkedInProfile, Task, etc.
│   ├── notifications/                  # sheets.py, slack.py, synthesis.py
│   ├── onboarding.py                   # First-run interactive setup
│   ├── pipeline/                       # Candidate sourcing + qualification
│   ├── setup/                          # GDPR, self-profile, freemium campaign
│   └── tasks/                          # connect, sweep, follow_up, discovery
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
- [CRM v2 evidence and pipeline contract](docs/crm-v2-contract.md)
- [Canonical CRM refresh](docs/crm-refresh-workflow.md)
- [System flow (operational)](docs/system-flow.txt)
- [Human-in-the-loop workflows](docs/human-workflows.md)
- [Follow-up generation runbook](docs/followup-generation-workflow.md)
- [Google/meeting context ingestion](docs/data-sync-workflow.md)
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
