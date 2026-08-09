# Sender-Aware LinkedIn Profile Discovery — Overall Implementation Plan

**Date:** 2026-07-29

**Branch:** `codex/discovery-mode-review`

**Status:** Implemented in this branch; disabled pending controlled sender/ICP
activation and live selector verification

## Goal

Build a standalone LinkedIn discovery workflow that runs after a sender's
weekday connection work is complete or freely on configured rest days.

The workflow does only four things:

1. Find people on an enabled LinkedIn discovery surface.
2. Compare the visible profile card with the logged-in sender's
   discovery-enabled ICPs.
3. Open profiles that plausibly match one enabled ICP.
4. Save their structured LinkedIn profile information into one dedicated
   discovery-leads table.

Each saved row must identify:

- the complete collected LinkedIn profile,
- the sender/account that stored it, and
- the potential ICP it appeared to match.

The workflow ends after saving the profile. It does not create CRM Leads,
Deals, connection requests, follow-up messages, or other outbound work.

## Confirmed Product Rules

- Discovery is its own scheduled workflow.
- It does not run in the middle of normal connection-request activity.
- On weekdays it becomes eligible when the sender is within the configured
  grace of the daily connection limit, cannot send more connections, or has no
  connectable work.
- On rest days/weekends it may run at any hour.
- Discovery uses each sender's existing `linkedin/icp_messages.json` block.
- Only ICPs explicitly enabled for discovery are considered.
- Initial validation is deliberately lightweight.
- The initial validation chooses one potential ICP; it is not final
  qualification.
- A plausible result is opened and fully collected from LinkedIn.
- Saved profiles go into a separate discovery table, not `crm.Lead`.
- Every stored profile records which sender stored it.
- Every stored profile records its potential ICP.
- Each sender has a hard daily limit on newly saved discovery leads.
- The workflow also has scan/page/time limits so it cannot browse forever when
  few profiles match.
- Discovery never sends or accepts anything on LinkedIn.

## Non-Goals

This plan does not include:

- Connection-request creation.
- Follow-up creation.
- Automatic invitation acceptance.
- GP/BALD qualification or embedding.
- A replacement for the existing CRM or People Sheet.
- Restoring the removed global `goto_page()` profile-link harvesting hook.

## High-Level Flow

```text
discovery gate eligible
        ↓
load sender's enabled discovery ICPs
        ↓
open one discovery source/page
        ↓
extract source-specific person cards
        ↓
skip self, malformed URLs, existing CRM Leads, and existing discovery rows
        ↓
lightweight card-to-ICP screen
        ↓
plausible match?
  no → continue scanning within the hard scan cap
 yes
        ↓
open LinkedIn profile
        ↓
fetch structured Voyager profile
        ↓
save one LinkedInDiscoveryLead row
        ↓
daily saved-lead limit reached?
 yes → stop for this sender/day
  no → process the next bounded unit
```

## Sender-Specific ICP Configuration

### Chosen location

Extend `linkedin/icp_messages.json`. Do not create a separate discovery JSON.

The file is already keyed by canonical sender and then by canonical ICP. This
lets discovery be enabled independently for each sender and ICP.

### Implemented shape

```json
{
  "Arian": {
    "CSPs": {
      "discovery": {
        "enabled": true,
        "profile": "Executives, security, compliance, and public-sector leaders at cloud software providers with possible FedRAMP relevance.",
        "search_queries": [
          "FedRAMP SaaS founder",
          "public sector cloud CTO"
        ]
      },
      "linkedin_connect_note": [
        "..."
      ],
      "linkedin_connect_followup": [
        "..."
      ]
    },
    "CMMC Buyers": {
      "discovery": {
        "enabled": false,
        "profile": ""
      },
      "linkedin_connect_note": [
        "..."
      ],
      "linkedin_connect_followup": [
        "..."
      ]
    }
  }
}
```

### Field semantics

- `enabled`: whether this sender wants discovery for the ICP.
- `profile`: a short description used for broad card-level matching.
- `search_queries`: optional explicit LinkedIn People-search queries for this
  sender/ICP.

The discovery block contains no Deal or sending state.

### Validation

- `discovery` is optional.
- A missing block means discovery is disabled for that sender/ICP.
- `enabled` must be a boolean.
- An enabled block must have a non-empty `profile`.
- `search_queries`, when present, must be a list of non-empty strings.
- Unknown discovery keys should fail validation.
- A sender with no enabled discovery ICPs produces a clean no-op.
- Configuration is loaded fresh for each discovery run.
- Existing message rendering must ignore discovery metadata.

### Sheets round trip

`save_icp_messages()` already merges edited message channels into the existing
ICP block, so JSON-only fields survive `sync_icp_messages --pull`.

The first version keeps discovery settings JSON-managed. Adding Discovery
columns to the sender ICP Message Sheets is outside this implementation.

## Dedicated Discovery Table

Add one model in the `linkedin` Django app:

### `LinkedInDiscoveryLead`

One row represents one fully collected LinkedIn profile.

Required fields:

- `public_identifier`: canonical LinkedIn public identifier.
- `linkedin_url`: canonical LinkedIn profile URL.
- `member_urn`: LinkedIn profile/member URN when available.
- `first_name`.
- `last_name`.
- `full_name`.
- `headline`.
- `company_name`.
- `location`.
- `profile_data`: complete parsed structured Voyager profile JSON.
- `stored_by_operator`: canonical sender handle such as `Arian` or `Chuka`.
- `stored_by_account_username`: the LinkedIn account username.
- `potential_icp`: one canonical ICP enabled for that sender.
- `created_at`.
- `updated_at`.

Optional operational fields:

- `last_seen_at`: useful if the profile is encountered again.
- `last_profiled_at`: when the stored LinkedIn profile was fetched.

### Identity and deduplication

- Canonical LinkedIn URL/public identifier is globally unique.
- The first sender that stores the profile owns the row's
  `stored_by_operator` value.
- If another sender encounters the same canonical profile later, discovery
  skips it and does not overwrite the original sender or potential ICP.
- Existing `crm.Lead` rows are skipped before profile collection.
- The logged-in account's self-profile is always skipped.
- Active suppression matches are always skipped.

The table stays limited to the collected profile, the sender that stored it,
the potential ICP, canonical identity, and collection timestamps.
Rejected/irrelevant cards exist only in the current run's in-memory/task
dedupe and are not accumulated.

## Per-Sender Daily Save Limit

### Required environment setting

Use `DISCOVERY_DAILY_LIMIT` as the shared per-sender save cap.

Implemented behavior:

- Default to a conservative positive value of `25`.
- A non-positive value is rejected as invalid configuration.
- Count only newly created `LinkedInDiscoveryLead` rows.
- Duplicate encounters and updates do not consume the limit.
- The day boundary uses `ACTIVE_TIMEZONE`.

### Limit calculation

Before starting a run and before every profile visit:

```text
saved_today =
  count LinkedInDiscoveryLead
  where stored_by_operator = current sender
  and created_at >= discovery local-day start

remaining_today = DISCOVERY_DAILY_LIMIT - saved_today
```

When `remaining_today <= 0`:

- do not open another discovery profile,
- complete the current discovery task cleanly,
- do not enqueue more discovery work for that sender until the next eligible
  day/window,
- log the daily-limit stop reason.

### Why a save limit is not enough

If no cards match an ICP, the saved-lead count never increases. The workflow
could otherwise scan indefinitely.

Every run also needs:

- maximum cards scanned,
- maximum search result pages,
- maximum profile visits,
- maximum run time,
- source/query exhaustion,
- a maximum consecutive-no-match threshold.

The first condition reached stops the run.

## Implemented Hard Limits

Add the following settings:

```dotenv
ENABLE_PROFILE_DISCOVERY=false
DISCOVERY_DAILY_LIMIT=25
DISCOVERY_CONNECT_LIMIT_GRACE=5

DISCOVERY_MAX_CARDS_PER_RUN=200
DISCOVERY_MAX_PAGES_PER_RUN=10
DISCOVERY_MAX_PROFILE_VISITS_PER_RUN=40
DISCOVERY_MAX_CONSECUTIVE_NO_MATCHES=75
DISCOVERY_MAX_RUN_MINUTES=120

DISCOVERY_PROFILE_DELAY_MIN_SECONDS=20
DISCOVERY_PROFILE_DELAY_MAX_SECONDS=45
```

`DISCOVERY_DAILY_LIMIT` is authoritative for how many new discovery leads each
sender may save per day.

Environment limits bound browsing work independently of how many records are
saved.

Validation requirements:

- discovery defaults off,
- the daily limit must be positive,
- the connection-limit grace must be nonnegative,
- scan/page/visit/no-match limits must be positive,
- delays must be positive,
- minimum delay cannot exceed maximum delay,
- timezone must be valid.

## Lightweight ICP Screening

### Purpose

Screening decides only:

> Does this visible card plausibly match one discovery-enabled ICP for this
> sender, making the profile worth opening?

### Inputs

- Visible card name.
- Visible headline/title.
- Visible company.
- Source-specific visible context.
- Enabled ICP names and `discovery.profile` descriptions for the current
  sender.

### Deterministic skips

Before semantic screening:

- invalid or non-profile LinkedIn URL,
- current sender's own profile,
- existing `crm.Lead`,
- existing `LinkedInDiscoveryLead`,
- active suppression match,
- missing identity that cannot be canonicalized.

### Structured result

```json
{
  "should_visit": true,
  "potential_icp": "CSPs"
}
```

Rules:

- `potential_icp` must be one of the current sender's enabled ICPs.
- Return exactly one best potential ICP.
- Prefer `should_visit=true` when the card is ambiguous but plausible.
- This result is not final qualification.
- A screening failure fails the bounded Task loudly and defers a fresh run to
  the next eligible window.

Use the existing configured LLM with a low-temperature structured output for
semantic matching. Deterministic skips happen before the LLM call.

## Discovery Sources

Each source must extract only its own person-card container. Never collect
every `/in/` link visible on the page.

### Phase 1 source: People search

- Use explicit `discovery.search_queries` for enabled sender/ICP blocks.
- Open one result page at a time.
- Extract canonical profile URL, name, headline/title, and company.
- Deduplicate before screening.
- Track query/page position in the discovery Task payload.
- Stop when the query list, page cap, card cap, daily save limit, or time
  window is exhausted.

### Later sources

After People search is stable, the same workflow may add:

- People You May Know,
- incoming invitations without accepting or ignoring them,
- explicitly identified profile-recommendation modules,
- relevant feed authors,
- inbox participants.

Each additional source requires its own configuration gate and fixture-backed
selectors. These sources still save into the same `LinkedInDiscoveryLead`
table.

## Profile Visit and Save

For a card that passes the lightweight screen:

1. Re-check the sender's remaining daily save capacity.
2. Re-check the dynamic discovery gate and run caps.
3. Navigate to the canonical LinkedIn profile.
4. Verify the browser reached the expected profile or an accepted canonical
   redirect.
5. Fetch structured profile data with
   `PlaywrightLinkedinAPI.get_profile()`.
6. Normalize the returned public identifier and URL.
7. Re-run existing CRM/discovery/suppression dedupe using the canonical
   identity returned by LinkedIn.
8. Create one `LinkedInDiscoveryLead` row with:
   - full profile data,
   - current sender/account, and
   - screened potential ICP.
9. Recompute the sender's saved-today count.
10. Stop if the daily limit is reached; otherwise schedule the next bounded
    discovery unit after a randomized delay.

Expected outcomes:

- Private/restricted/unavailable profiles are logged and skipped.
- A canonical duplicate discovered after profile fetch is skipped.
- A restricted profile or exhausted profile/API fetch is skipped; an
  unexpected failure fails loudly and defers a fresh run to the next eligible
  window.
- No outcome creates or changes a CRM Deal.

## Scheduling and Browser Serialization

### Execution model

Add a dedicated operator-scoped discovery task type to the existing
single-threaded daemon:

```text
Task.TaskType.DISCOVERY
```

This keeps the workflow separate while ensuring discovery cannot navigate the
browser concurrently with connection, follow-up, sweep, or manual-reply work.

### Task payload

```json
{
  "operator": "Arian",
  "source": "people_search",
  "query_index": 0,
  "page": 1,
  "pending_cards": []
}
```

The payload is only a resumable discovery cursor.

Requirements:

- Discovery is included in linked-account-scoped task types.
- `payload.operator` is required.
- The task is claimable only by the matching sender daemon.
- Manual replies remain higher priority.
- One discovery task execution performs one bounded browser unit and then
  completes/re-enqueues.

### Weekday gate

Discovery may run on a normal workday only when:

- `ENABLE_PROFILE_DISCOVERY=true`,
- at least one sender ICP has discovery enabled,
- the sender is within `DISCOVERY_CONNECT_LIMIT_GRACE` of the connection daily
  limit, cannot execute another connection, or has no connectable work,
- the sender has not reached the discovery daily save limit.

### Rest-day behavior

On configured rest days:

- normal connect/follow-up tasks remain blocked,
- discovery is claimable at any hour,
- manual replies and status summaries retain existing priority,
- the same daily save and browsing caps apply.

### Task seeding

The daemon should ensure at most one pending/running discovery task per sender.

It should seed a fresh cursor when the dynamic gate opens and:

- no active discovery task exists,
- the daily save limit has not been reached,
- at least one ICP is enabled,
- the sender has explicit discovery search queries.

It must not continuously recreate work after a daily stop condition. Once the
sender reaches the daily save limit or all queries are exhausted, no additional
discovery task is seeded until the next eligible local day.

Use a small per-sender/day completion marker in the Task payload/history or a
minimal scheduling marker if needed.

## Django Admin

### `LinkedInDiscoveryLead`

Provide a read-only operational list with:

- name,
- company,
- headline,
- stored-by sender,
- potential ICP,
- LinkedIn URL,
- created date.

Filters:

- stored-by sender,
- potential ICP,
- created date.

Search:

- name,
- company,
- headline,
- public identifier,
- LinkedIn URL.

This Admin view is only for inspecting collected data and exposes no sending
actions.

## File-Level Implementation Map

### Create

- `linkedin/discovery/__init__.py`
- `linkedin/discovery/config.py`
- `linkedin/discovery/screening.py`
- `linkedin/discovery/collector.py`
- `linkedin/discovery/sources/__init__.py`
- `linkedin/discovery/sources/base.py`
- `linkedin/discovery/sources/people_search.py`
- `linkedin/tasks/discovery.py`
- `linkedin/management/commands/start_discovery.py`
- `tests/discovery/`
- Django migration for:
  - `LinkedInDiscoveryLead`,
  - discovery Task choice.

### Modify

- `linkedin/models.py`
  - add `LinkedInDiscoveryLead`,
  - add Task type and operator scoping.
- `linkedin/daemon.py`
  - register the handler,
  - seed discovery tasks,
  - apply the weekday connection-completion gate and unrestricted rest days.
- `linkedin/conf.py`
  - discovery daily save, connection grace, and browsing limits.
- `linkedin/env_spec.py`
  - discovery environment registry.
- `.env.example`
  - disabled-by-default discovery settings.
- `linkedin/icp_outbound.py`
  - sender discovery-block loader and validation.
- `linkedin/admin.py`
  - sender limit and discovery-lead inspection.
- `linkedin/exceptions.py`
  - expected discovery configuration/surface exceptions if needed.
- `AGENTS.md`, `ARCHITECTURE.md`, `README.md`, and
  `docs/configuration.md`
  - required implementation and operator documentation.

## Implementation Phases

### Phase 1 — Sender ICP discovery configuration

- [x] Add tests for enabled, disabled, missing, and malformed discovery blocks.
- [x] Implement strict `load_discovery_targets(sender)`.
- [x] Verify existing message rendering ignores discovery metadata.
- [x] Verify Sheets pull preserves discovery metadata.
- [ ] Add one controlled sender/ICP discovery block.

Acceptance criteria:

- No enabled block means no discovery.
- Malformed configuration fails before browser activity.
- Existing outbound messaging behavior is unchanged.

### Phase 2 — Discovery table and daily limit

- [x] Add `LinkedInDiscoveryLead` model tests.
- [x] Add global canonical profile uniqueness.
- [x] Add the `DISCOVERY_DAILY_LIMIT` environment setting.
- [x] Implement sender-local-day saved count.
- [x] Add Admin visibility.
- [x] Add migrations.

Acceptance criteria:

- Each stored row contains full profile data, sender, and potential ICP.
- Duplicate profiles cannot be inserted.
- The first storing sender is never silently overwritten.
- A sender at its daily limit cannot save another discovery lead.

### Phase 3 — People-search card collection

- [x] Define the source/card interface.
- [ ] Capture current People-search fixtures.
- [x] Implement source-specific card parsing.
- [x] Implement query/page cursor handling.
- [x] Add card, page, and consecutive-no-match caps.
- [x] Add self/CRM/discovery/suppression skips.

Acceptance criteria:

- Only actual result cards are collected.
- Generic page `/in/` links are ignored.
- Scanning stops at every configured hard cap.

### Phase 4 — Lightweight ICP screening

- [x] Add the structured screening schema.
- [x] Add the sender-enabled ICP prompt.
- [x] Validate `potential_icp` against enabled buckets.
- [x] Prefer collection on plausible ambiguity.
- [x] Add explicit bounded-task failure/retry scheduling.

Acceptance criteria:

- Screening returns only visit/no-visit and one potential ICP.
- It does not create CRM or outbound state.

### Phase 5 — Profile collection and save

- [x] Reuse canonical profile navigation checks.
- [x] Reuse `PlaywrightLinkedinAPI.get_profile()`.
- [x] Persist complete parsed profile data.
- [x] Recheck canonical dedupe after enrichment.
- [x] Handle restricted profiles as expected skips.
- [x] Apply randomized delay between profile visits.
- [x] Stop immediately when the sender reaches its daily saved-lead limit.

Acceptance criteria:

- A plausible result becomes exactly one discovery-table row.
- The row identifies the storing sender and potential ICP.
- No CRM Lead, Deal, Message, or outbound task is created.

### Phase 6 — Scheduled discovery lane

- [x] Add operator-scoped `DISCOVERY` Task validation.
- [x] Add weekday discovery-window calculation.
- [x] Add rest-day discovery-window calculation.
- [x] Keep manual replies higher priority.
- [x] Process one bounded unit per Task execution.
- [x] Prevent repeated task seeding after daily limit/query exhaustion.
- [x] Add next-day reset tests.

Acceptance criteria:

- Discovery never overlaps another daemon browser action.
- It runs only after outbound hours or in configured rest-day windows.
- Every sender stops at their own daily saved-lead limit.
- Low-match runs still stop at scan/page/no-match/time caps.

### Phase 7 — Documentation and controlled rollout

- [x] Update project architecture and configuration documentation.
- [x] Add manual `start_discovery` instructions.
- [x] Run focused and full test suites. The focused suite passes; the full
  suite reaches three unrelated pre-existing onboarding/auto-discovery
  failures.
- [x] Run `git diff --check`.
- [ ] Perform a manual collection-only dry run.
- [ ] Enable one sender, one ICP, and a low daily limit.
- [ ] Inspect stored profiles before expanding queries/senders/sources.

## Test Strategy

### Unit tests

- Discovery JSON parsing and strict validation.
- Sheets round-trip preservation.
- Sender daily-limit counting across local-day boundaries.
- Duplicate rows do not consume daily limit.
- Global canonical profile dedupe.
- First-sender ownership preservation.
- ICP screen output validation.
- Schedule-window calculations.
- Task payload/operator scoping.
- Daily completion and next-day reset.
- Card/page/visit/no-match stop conditions.

### Fixture-backed browser tests

- People-search result cards.
- Missing headline/company.
- Duplicate profile links.
- Self-profile links.
- Non-card navigation/profile links.
- Canonical profile redirects.
- Restricted/private profiles.

### Integration tests

- A discovery Task scans, screens, visits, saves, and re-enqueues.
- The daily saved-lead limit stops additional profile visits.
- A low-match run stops at the card/no-match cap.
- Manual reply priority wins over discovery.
- Off-hours discovery claimability does not enable connect/follow-up Tasks.
- Discovery writes no CRM or outbound records.

### Manual LinkedIn verification

- Confirm selectors against the current live People-search UI.
- Confirm profile navigation uses the correct logged-in sender.
- Confirm structured profile data is complete enough for later use.
- Confirm the task stops at the daily sender limit.
- Confirm weekend/off-day windows behave as configured.
- Confirm no invitations or messages are sent.

## Observability

Log per sender/day:

- enabled discovery ICPs,
- query and page,
- cards scanned,
- deterministic skips,
- no-match count,
- profiles opened,
- discovery leads newly saved,
- daily saved/limit count,
- duplicate CRM/discovery matches,
- restricted/unavailable profiles,
- stop reason.

Required stop reasons:

- `daily_save_limit_reached`,
- `card_limit_reached`,
- `page_limit_reached`,
- `profile_visit_limit_reached`,
- `consecutive_no_match_limit_reached`,
- `weekday_connection_work_incomplete`,
- `day_ended`,
- `queries_exhausted`,
- `discovery_disabled`,
- `no_enabled_icps`,
- `authentication_lost`,
- `linkedin_limit_detected`.

Discovery metrics must not be added to outbound `ActionLog` counts.

## Safety and Failure Rules

- Discovery defaults off.
- `DISCOVERY_DAILY_LIMIT` is a positive per-sender saved-row cap.
- No silent sender fallback.
- No generic profile-link harvesting.
- No browser concurrency with outbound tasks.
- No CRM Lead or Deal creation.
- No connection or message action.
- No invitation acceptance.
- Daily saved-lead limit is checked before every profile visit and insert.
- Card/page/visit/no-match/time caps are always enforced.
- Authentication loss ends the current workflow.
- LinkedIn limit/restriction surfaces end the current workflow.
- Unexpected programming/database errors fail loudly.
- Expected restricted profiles and transient screening/profile failures are
  handled as explicit recoverable outcomes.

The repository's existing legal notice applies: automated LinkedIn access
violates LinkedIn's terms, and pacing/limits cannot eliminate account risk.

## Configuration Decisions

1. **Initial per-sender daily limit**
   - Implemented default: `25`.

2. **Initial sender and ICP**
   - Pending operator selection; no sender is enabled by this branch.

3. **Initial search queries**
   - Pending operator selection; explicit queries are required before
     activation.

4. **Weekday window**
   - Implemented default: 18:00–21:00 America/Toronto.

5. **Rest-day window**
   - Implemented default: 11:00–16:00 America/Toronto.

6. **Browsing caps**
   - Implemented defaults: 200 cards, 10 pages, 40 profile visits, and 75
     consecutive non-matches per run.

7. **First source**
   - Implemented: People search.
   - People You May Know can follow once the basic workflow is stable.

## Definition of Done

The feature is complete when:

- selected ICP blocks can enable discovery per sender;
- each sender has a configurable daily discovery-lead limit;
- discovery runs only after outbound hours or in configured rest-day windows;
- the workflow scans only a bounded number of cards/pages/profiles;
- plausible cards are opened and fetched as structured profiles;
- each new profile is stored once in `LinkedInDiscoveryLead`;
- every stored row identifies its storing sender and potential ICP;
- reaching the daily save limit stops further discovery for that sender/day;
- low-match runs stop at their independent browsing caps;
- discovery creates no CRM or outbound state;
- the workflow is tested, documented, observable, and disabled by default.
