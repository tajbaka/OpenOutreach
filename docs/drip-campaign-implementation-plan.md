# Independent Multichannel Drip Campaigns — Implementation Plan

Status: proposed implementation plan

Date: 2026-08-29

Scope: new intent/theme-based LinkedIn + Gmail drip subsystem that coexists safely with the existing outbound connection and legacy follow-up flows

## 1. Decision summary

Build drip campaigns as a separate Django app and runtime subsystem. A drip campaign is a versioned narrative made of beats. Each beat expresses one communication intent and may contain an independently scheduled LinkedIn rendition, Gmail rendition, or both.

The two channel lanes advance independently:

- LinkedIn delivery requires positive operator-specific connection evidence and a final live first-degree check.
- Gmail delivery never depends on LinkedIn connection, LinkedIn delivery, or the LinkedIn daemon being healthy.
- A failure or unavailable prerequisite affects only that channel lane.
- Any qualifying inbound reply on either channel stops the entire enrollment and hands control to a human.
- No drip lifecycle state mutates `Deal.state`.
- Existing connection campaigns continue to own discovery, qualification, connection requests, invitation notes, acceptance sweeps, and their current legacy follow-ups unless an explicit drip enrollment takes automation ownership for that Lead.

The existing shared `Task` table remains transport plumbing, but drip receives new task types and keeps all authoritative state in drip-owned tables. The existing `FOLLOW_UP` and `GMAIL_FOLLOW_UP` handlers are not reused as drip sequencers.

## 2. Verified repository baseline

The repository was fetched and checked against `origin/main` on 2026-08-29. The reviewed reusable sales-copy, sales-motion, calendar-link, and related test work is committed and pushed as `cbbf2da` (`Add Boundera sales copy and motion tooling`), which is the exact mainline base for this plan.

Current implementation baseline:

- Branch: `codex/drip-campaigns`
- Worktree: clean sibling worktree dedicated to drip work
- Mainline base and merge base: `cbbf2da` (`origin/main` and local `main`)
- Rebased prerequisite 1: `f8a6619` (`Add canonical CRM refresh workflow`, replayed from `c9d7c2e`)
- Rebased prerequisite 2: `2733157` (`Replace CRM with account-first active views`, replayed from `77926da`)
- Drip scope on top of those prerequisites: this plan only; no runtime drip implementation yet

Only those two pre-mainline commits were retained as drip prerequisites:

- `f8a6619` provides canonical CRM state, strict `Message.operator` provenance, and the Gmail/message-persistence foundation needed for sender ownership, reply attribution, and deduplication.
- `2733157` provides email-first Lead identity, allowing valid Leads without LinkedIn URLs. This is required for email-only and not-yet-connected enrollments.

The old `1567e27` Trello/pipeline commit was deliberately not replayed: it is not a hard drip dependency, and the retained CRM migration chain is complete through `0020`. The old `2cd3e7b` and `f5fdec0` sales-motion commits were also not replayed because `cbbf2da` contains the reviewed mainline versions of the reusable work. This keeps the drip branch free of unrelated history while preserving its actual data-model prerequisites.

The separate `temp` worktree remains dirty and must not be used for implementation commits:

- staged `.codex-work/**` research and Gmail audit utilities;
- unstaged Boundera sales-skill and documentation changes;
- untracked calendar-link and ICP Messages skill work.

No uncommitted `temp` changes currently touch the principal runtime seams (`linkedin/models.py`, `linkedin/daemon.py`, `gmail/worker.py`, `gmail/handoff.py`, `linkedin/tasks/follow_up.py`, or `linkedin/tasks/sweep_connections.py`). Implementation must continue only in the clean `codex/drip-campaigns` sibling worktree. Do not stash, reset, switch, or commit the mixed `temp` worktree as part of drip work.

## 3. Existing behavior that must remain unchanged

When every drip feature flag is false and no active drip enrollment exists, the following must be behaviorally identical to the pre-drip baseline:

- connect candidate selection and qualification;
- LinkedIn connection-request sends and invitation notes;
- `Deal` transitions through `READY_TO_CONNECT`, `PENDING`, and `CONNECTED`;
- connection acceptance sweeping and Slack acceptance alerts;
- current `FOLLOW_UP` task scheduling, operator ownership, pacing, and sends;
- current `GMAIL_FOLLOW_UP` handoff and sends;
- manual replies, feed engagement, discovery, status summaries, enrichment, and task priority;
- existing LinkedIn and Gmail reply persistence;
- CRM publication and Actions policy.

No initial drip implementation should add a required call, transaction, or external dependency to the connect or sweep send path. Acceptance and invitation evidence will initially be observed by the drip coordinator from existing database state.

## 4. Product model: narrative beats and independent renditions

A campaign is not defined as two unrelated channel sequences. It is a sequence of narrative beats:

```text
Beat 1 — Name the problem
  LinkedIn rendition: eligible during one window, requires connection
  Gmail rendition: eligible on its own schedule

Beat 2 — Explain the point of view
  LinkedIn rendition: independent LinkedIn copy
  Gmail rendition: independent email copy

Beat 3 — Provide proof
  LinkedIn rendition: independent LinkedIn copy
  Gmail rendition: independent email copy

Any qualifying reply — stop both lanes and hand off
```

Rules:

1. Cross-channel delivery dependencies are prohibited in version 1.
2. A rendition may depend only on an earlier rendition in the same channel.
3. Copy should be semantically related across channels but independently understandable.
4. LinkedIn waiting, failure, expiry, or unavailability never blocks Gmail.
5. Gmail missing-email, bounce, auth failure, or send failure never blocks LinkedIn.
6. Expired LinkedIn beats are skipped. Late acceptance releases at most the currently relevant beat and never dumps a backlog.
7. Each campaign version specifies whether a previous same-channel rendition must be `sent` or merely terminal (`sent`, `skipped`, or `expired`) before a later rendition becomes eligible.
8. Version 1 supports the `all_available` delivery policy: both eligible renditions may send. `first_available` and fallback policies should be reserved for a later schema version rather than implemented prematurely.

## 5. Configuration contract

Create a separate namespace that neither reads nor rewrites legacy campaign copy:

```text
drip/
  campaigns/
    fedramp-core/
      v1/
        manifest.json
```

One manifest contains the narrative structure and paired sender/ICP renditions so publication is atomic:

```json
{
  "schema_version": 1,
  "campaign_key": "fedramp-core",
  "version": 1,
  "name": "FedRAMP core multichannel narrative",
  "beats": [
    {
      "key": "readiness_problem",
      "intent": "Explain the recurring burden of point-in-time readiness.",
      "channels": {
        "linkedin": {
          "offset_hours": 24,
          "valid_for_hours": 72,
          "requires_connection": true,
          "predecessor_policy": "terminal"
        },
        "gmail": {
          "offset_hours": 48,
          "valid_for_hours": 72,
          "predecessor_policy": "sent"
        }
      }
    }
  ],
  "audiences": {
    "Arian": {
      "CSPs": {
        "readiness_problem": {
          "variants": [
            {
              "key": "a",
              "linkedin": {
                "body": "Hi {first_name}, ..."
              },
              "gmail": {
                "subject": "A question about readiness at {company_name}",
                "body": "Hi {first_name}, ..."
              }
            }
          ]
        }
      }
    }
  }
}
```

Publication requirements:

- strict schema validation;
- unique campaign, beat, and variant keys;
- known canonical senders and ICPs only;
- channel-specific required fields;
- finite nonnegative timings and valid delivery windows;
- allowlisted placeholders only;
- no cross-channel dependencies;
- deterministic paired-variant selection from enrollment ID and beat key;
- complete copy coverage for every enabled sender/ICP/channel;
- canonical serialization and SHA-256 digest;
- immutable published versions: the same campaign/version with a different digest is rejected.

Runtime must never reread mutable files for an active enrollment. A reviewed `publish_drip_campaign` command validates the file and stores the immutable definition in the database. Enrollment freezes the campaign version, ICP, sender, selected variants, and rendered delivery content.

The first implementation does not modify `linkedin/icp_messages.json`, `gmail/icp_emails.json`, or the in-progress ICP Messages Sheet workflow. A dedicated drip authoring workflow can be added only after runtime behavior is proven.

## 6. Separate Django app and persistence

Create a top-level `drip` Django app with its own migrations, admin, services, task handlers, worker, commands, and tests.

### 6.1 `DripCampaign`

- stable campaign key;
- human-readable name;
- enabled flag;
- optional active published version;
- created/updated timestamps.

### 6.2 `DripCampaignVersion`

- campaign FK;
- numeric version;
- schema version;
- canonical full definition snapshot;
- SHA-256 digest;
- publication timestamp and publisher metadata.

Constraint: unique campaign + version. Published rows are immutable through service and admin policy.

### 6.3 `DripEnrollment`

- UUID primary key;
- campaign version FK;
- Lead FK;
- SalesOwner/operator FK;
- snapshotted operator handle and ICP;
- optional source Deal/Campaign attribution;
- trigger kind and anchor timestamp;
- status: `active`, `paused`, `handed_off`, `stopped`, `completed`, `cancelled`, `review_required`;
- automation-ownership timestamp and explicit release timestamp;
- stop/handoff reason code and detail;
- optional triggering inbound Message or Meeting;
- created/updated/completed timestamps.

Default constraint: at most one unreleased automation-owning enrollment per Lead globally. This prevents two operators or campaigns from automating the same person concurrently. A future explicit policy can weaken this only with evidence.

Once any drip delivery has sent, disabling the feature pauses the enrollment but does not silently return ownership to legacy automation. Returning to legacy requires an explicit reviewed release command, preventing rollback from double-sending.

### 6.4 `DripLane`

One row per enrollment/channel:

- channel: `linkedin` or `gmail`;
- status: `waiting`, `active`, `degraded`, `unavailable`, `review_required`, `completed`, `stopped`;
- channel-specific failure/stop code;
- last activity timestamps.

A lane state change never directly changes the sibling lane.

### 6.5 `DripDelivery`

One row per enrollment + beat + channel rendition:

- UUID primary key;
- lane FK;
- beat key and channel ordinal;
- frozen variant key, subject, and body;
- `not_before` and `expires_at`;
- optional same-channel predecessor FK;
- predecessor policy;
- status: `waiting_prerequisite`, `scheduled`, `queued`, `sending`, `retry_wait`, `sent`, `skipped`, `failed`, `uncertain`, `stopped`;
- retry counters and next-attempt timestamp;
- send-started and sent timestamps;
- provider/external IDs;
- optional persisted CRM Message FK;
- last error code and sanitized detail.

Constraint: unique enrollment + beat key + channel.

### 6.6 `DripDeliveryAttempt`

Append-only attempt ledger:

- delivery FK and attempt number;
- stable idempotency key;
- optional shared Task FK;
- status and error category;
- `submit_attempted_at` stamped immediately before external mutation;
- started/finished timestamps;
- provider ID and reconciliation metadata.

Constraint: unique delivery + attempt number and unique idempotency key.

Task rows are execution leases, not the source of truth.

## 7. Timing and channel state machines

The campaign clock anchors to enrollment by default:

```text
not_before = enrollment.anchor_at + offset_hours
expires_at = not_before + valid_for_hours
```

Coordinator behavior:

- before `not_before`: keep delivery scheduled;
- after `expires_at` without send: mark skipped/expired;
- Gmail due with a satisfied same-channel predecessor: queue regardless of LinkedIn state;
- LinkedIn due without positive connection evidence: remain `waiting_prerequisite` with no executable LinkedIn Task;
- LinkedIn becomes connected inside the window: queue that delivery;
- LinkedIn becomes connected after older windows: expire old deliveries, select at most the latest currently valid beat, preserve future beats;
- retryable channel failure: retry only that delivery/lane;
- permanent channel unavailability: stop or skip only that lane according to policy.

Connection acceptance should not reset the full campaign clock in version 1; otherwise late LinkedIn acceptance would semantically rewind the story after Gmail had advanced. A future schema may support a connection-anchored sub-sequence if a separate campaign truly needs it.

## 8. Execution topology

### 8.1 Shared transport types

Add two new `Task` values, both within the existing 20-character field:

- `drip_linkedin`
- `drip_gmail`

Payloads remain minimal:

```json
{
  "delivery_id": "<uuid>",
  "operator": "Arian"
}
```

All lifecycle, timing, copy, identity, and deduplication data come from locked drip rows.

### 8.2 Dedicated drip process

Add `manage.py run_drip_worker` as a separate process/service. It performs:

- periodic enrollment and stop reconciliation;
- delivery-window evaluation;
- idempotent task materialization;
- atomic, operator-aware claiming of `drip_gmail` tasks;
- Gmail preflight, sending, persistence, and recovery.

This process must remain alive even if a LinkedIn daemon/browser crashes. Running Gmail drip inside the existing LinkedIn daemon would defeat the required fallback independence.

The existing `GmailWorker` remains unchanged. Its current plain-read claim and global running-task reclaim behavior must not be inherited by drip.

### 8.3 LinkedIn browser bridge

The existing sender-specific LinkedIn daemon claims `drip_linkedin`. Required shared changes are explicit and narrow:

1. Add the enum value and payload validation.
2. Add `drip_linkedin` to `_linked_operator_scope_q()`.
3. Add it to `linked_account_scoped_task_types()`.
4. Add it to daemon account-wide task handling so it does not require a legacy campaign payload.
5. Add a handler entry to `_HANDLERS`.
6. Add deliberate queue priority below manual/reply-critical work and without starving existing delivery tasks.
7. Add it to stale-task recovery and `seconds_to_next()` ownership tests.
8. Add `drip_gmail` to `non_linkedin_outbound_task_types()` so a browser daemon can never claim it.

Omitting operator scoping would make the new task account-agnostic and recreate the project’s prior wrong-sender failure class. This is a release-blocking test requirement.

### 8.4 LinkedIn rate accounting

Drip LinkedIn sends must count against the existing Follow Up daily/global action caps. For true campaign independence, make `ActionLog.campaign` nullable and allow `LinkedInProfile.record_action(..., campaign=None)` for drip only. Existing outbound callers continue passing their Campaign unchanged. The `DripDeliveryAttempt` remains the detailed drip attribution ledger.

Also add a conservative drip-specific daily sub-cap so enabling drip cannot consume the entire existing follow-up budget. Initial queue priority remains below legacy follow-up/connect delivery.

## 9. Channel eligibility and send handlers

### 9.1 LinkedIn eligibility

Do not reuse exact legacy assumptions that `Deal.state == CONNECTED` and an existing outbound Message must exist. Legacy follow-up can mark a still-valid connection `COMPLETED`.

The drip eligibility service requires:

- Lead has a canonical LinkedIn identity;
- enrollment operator has positive ownership evidence (matching project-sent invitation, existing operator-owned thread, or reviewed manual enrollment);
- stored connection evidence such as `connected_at` or inbound LinkedIn activity;
- final live first-degree status under the exact sender browser immediately before send.

A LinkedIn delivery with no connection remains waiting or expires. It does not fail the enrollment or affect Gmail.

The drip LinkedIn handler:

1. Locks enrollment, lane, delivery, and current attempt.
2. Rechecks ownership, active status, window, same-channel predecessor, suppression, meeting, and reply conditions.
3. Fetches/persists the live LinkedIn conversation immediately before send.
4. Re-runs the stop policy.
5. Verifies live first-degree status.
6. Freezes/stamps the attempt boundary.
7. Calls the existing low-level LinkedIn send primitive, not `handle_follow_up`.
8. Persists deterministic drip Message and attempt state.
9. Records existing Follow Up rate usage without touching `Deal.state`.

### 9.2 Gmail eligibility

The drip Gmail handler:

1. Locks enrollment, lane, delivery, and current attempt.
2. Rechecks active status, timing, same-channel predecessor, suppression, meeting, reply, and valid email.
3. Refreshes Gmail threads for the Lead immediately before send.
4. Re-runs the global stop policy.
5. Freezes/stamps the attempt boundary.
6. Sends through a drip-specific Gmail result method that returns message and thread IDs.
7. Persists deterministic drip Message and attempt state.

It never checks `Deal.state`, connection status, or LinkedIn delivery outcome.

Email enrichment should be a later independent lane capability (`drip_enrich_email`) rather than silently routing through the legacy enrichment/Gmail handoff in the first delivery milestone.

## 10. Stop policy and human handoff

Admission defaults:

- refuse disqualified or actively suppressed Leads;
- refuse Leads with an existing meeting or unresolved human conversation;
- refuse active legacy automation unless explicit reviewed takeover is requested;
- deliberate re-engagement after historical inbound requires explicit override and a recorded reply baseline.

Runtime global stops:

- inbound LinkedIn Message attributable after the enrollment baseline;
- inbound Gmail Message attributable after the enrollment baseline;
- Meeting created for the Lead;
- suppression/disqualification;
- recommended safety: non-drip human outbound after enrollment pauses automation.

Stopping is transactional:

1. Lock enrollment.
2. Mark it `handed_off` or `stopped` with a reason and triggering record.
3. Stop both lanes.
4. Stop every nonterminal delivery.
5. Complete pending drip Tasks with an audit reason.
6. Let any running handler perform the same state check before crossing its send boundary.

Correctness does not depend on realtime callbacks. The worker periodically reconciles local Message/Meeting state, and every handler refreshes its own channel and rechecks immediately before send. Later, LinkedIn realtime and Gmail persistence may best-effort wake the same stop service to improve latency.

## 11. External-send crash safety

Exactly-once external delivery cannot be guaranteed by a database transaction. Both providers have a crash window after accepting a send but before local success is committed.

Required policy:

- Stamp `submit_attempted_at` before the external mutation.
- A stale `sending` attempt with that stamp becomes `uncertain`, never an automatic retry.
- `uncertain` affects only its channel lane and requires reconciliation or human review.

LinkedIn recovery:

- fetch the exact thread;
- match exact frozen body, sender, and bounded send time;
- reconcile to `sent` only when proof is unambiguous;
- otherwise keep `uncertain` and never duplicate automatically.

Gmail recovery:

- send with a deterministic RFC Message-ID derived from delivery UUID;
- search that ID on recovery;
- reconcile only with exact proof;
- otherwise keep `uncertain`.

Extend low-level send/persistence APIs with optional drip-specific deterministic persistence while preserving current defaults for every legacy caller.

## 12. Coexistence and automation ownership

An active drip enrollment and legacy follow-up automation must never operate concurrently for the same Lead.

Enrollment behavior:

- default: refuse when pending/running legacy `follow_up` or `gmail_follow_up` exists;
- `--take-over`: under one transaction, refuse if a legacy task is running, retire only pending legacy tasks, create the enrollment, and acquire durable automation ownership;
- record every retired Task ID and reason.

Narrow defense-in-depth integration points:

1. `enqueue_follow_up()` no-ops for the same Lead/operator when drip owns automation.
2. `gmail.handoff.maybe_schedule_gmail_sequence()` does the same.
3. Legacy LinkedIn and Gmail handlers no-op if ownership changed after their Task was queued.
4. `heal_tasks()` excludes Leads whose drip enrollment owns automation, preventing restart from recreating legacy follow-ups.

These checks consult durable enrollment ownership even if the drip send feature flag is temporarily disabled. Disabling drip pauses the new system; it must not silently reactivate legacy messages after partial drip delivery.

Do not put a drip check inside the existing shared `automation_stop_reason()`, because the drip handler would then stop itself. Use a dedicated ownership predicate and a separate drip stop policy.

Existing connection tasks and sweeps remain eligible. Drip ownership suppresses only automated follow-up messaging, not connection acquisition or acceptance detection.

## 13. Optional outbound-to-drip enrollment rules

Automatic integration is opt-in and arrives only after manual pilots.

Add `DripEnrollmentRule`:

- source legacy Campaign;
- target DripCampaign/version;
- canonical operator;
- trigger (`deal_ready`, `invitation_sent`, or later `connected`);
- enabled flag;
- optional cohort/allowlist controls.

The dedicated coordinator polls existing database evidence and enrolls idempotently. It does not add calls to `handle_connect` or `process_accepted_deal` in the initial implementation.

Recommended first production trigger: `invitation_sent`, because it is positive proof that this project/operator sent the request. Gmail can then proceed while acceptance remains pending. If product policy requires Gmail even when the LinkedIn request itself never succeeds, use `deal_ready`; that is a deliberate campaign decision rather than an accidental fallback.

## 14. Feature flags and operational controls

All flags default false:

- `ENABLE_DRIP_CAMPAIGNS`
- `ENABLE_DRIP_SHADOW_MODE`
- `ENABLE_DRIP_GMAIL`
- `ENABLE_DRIP_LINKEDIN`
- `ENABLE_DRIP_AUTO_ENROLL`

Additional limits:

- drip LinkedIn daily sub-cap;
- maximum Gmail deliveries per worker pass;
- maximum coordinator enrollments/materializations per pass;
- retry caps and bounded backoff;
- optional allowlisted Lead IDs for live pilots.

Commands should default to validation/dry-run and require `--apply` for mutations:

- `validate_drip_campaigns`
- `publish_drip_campaign`
- `plan_drip_enrollment`
- `enroll_drip`
- `release_drip_enrollment`
- `reconcile_drip_campaigns`
- `drip_status`
- `run_drip_worker`

No command sends during validation, publication, enrollment planning, or enrollment creation. External sends occur only through enabled workers processing approved active enrollments.

## 15. Implementation phases and exit gates

### Phase 0 — Clean baseline

Work:

- use the existing clean sibling worktree and `codex/drip-campaigns` branch rebased onto `cbbf2da`;
- retain only `f8a6619` and `2733157` as pre-plan prerequisites;
- confirm fetched main remains the exact merge base;
- leave the current dirty `temp` worktree untouched;
- capture baseline test results with `.venv/bin/python` and an explicit test database configuration.

Exit gate:

- clean branch/worktree;
- baseline focused and full tests recorded;
- no user work moved or committed.

### Phase 1 — Definitions and domain only

Work:

- add separate `drip` app;
- add strict definition schema/loader/publisher;
- add campaign, version, enrollment, lane, delivery, and attempt models;
- add migrations and admin;
- add dry-run planner and default-off flags;
- add one test-only/example campaign definition with nonproduction copy.

No Task types, workers, or send calls.

Exit gate:

- immutable publication proven;
- deterministic rendering proven;
- constraints and admin visibility proven;
- entire legacy suite unchanged.

### Phase 2 — Shadow coordinator

Work:

- materialize lanes/deliveries;
- implement clocks, windows, dependencies, stops, expiry, and late-accept logic;
- add coordinator in shadow mode;
- record `would_queue` decisions without creating executable Tasks;
- add observability/status command.

Exit gate:

- scenario matrix passes;
- copied/nonproduction data produces correct decisions;
- zero external sends possible by construction.

### Phase 3 — Independent Gmail pilot

Work:

- add `drip_gmail` type and browser exclusion;
- add atomic drip Gmail claim/recovery;
- add separate `run_drip_worker` service;
- add Gmail preflight, deterministic Message-ID, attempt ledger, and uncertainty handling;
- manually enroll an allowlisted internal/test cohort;
- keep LinkedIn drip disabled and auto-enrollment disabled.

Exit gate:

- email sends while source Deal remains pending;
- LinkedIn health is irrelevant to Gmail execution;
- Gmail reply stops enrollment;
- crash-window recovery never auto-duplicates;
- legacy Gmail behavior unchanged outside active enrollments.

### Phase 4 — LinkedIn pilot

Work:

- add `drip_linkedin` type, operator scoping, dispatch, priority, and stale recovery;
- add live thread refresh and first-degree verification;
- add deterministic persistence and uncertain-send recovery;
- count against existing rate caps with a drip sub-cap;
- pilot only reviewed already-connected Leads with positive operator ownership;
- keep auto-enrollment disabled.

Exit gate:

- wrong operator can never claim;
- pending/nonconnected profiles never send;
- no prior-thread requirement when live first-degree ownership is proven;
- LinkedIn failure does not affect Gmail lane;
- no `Deal.state` mutation.

### Phase 5 — Multichannel ownership pilot

Work:

- implement transactional takeover/release;
- add all four legacy guard points;
- enable both lanes for a tiny allowlisted cohort;
- exercise channel failures, replies, late acceptance, expiry, pauses, and rollback.

Exit gate:

- zero duplicate legacy+drip tasks/sends;
- reply on either channel stops all remaining deliveries;
- disable flags pause without handing back to legacy;
- reviewed release is the only handback mechanism.

### Phase 6 — Opt-in source-campaign integration

Work:

- add disabled `DripEnrollmentRule` support;
- enable one source Campaign/operator and a bounded cohort;
- enroll from existing evidence by polling;
- compare delivery, reply, skip, uncertainty, and human-handoff metrics;
- retire legacy follow-up for that source campaign only after proof.

Exit gate:

- stable production cohort over an agreed observation period;
- no regression in connect/sweep performance;
- no wrong-sender, duplicate-send, or cross-channel-block incidents;
- documented operator rollback procedure.

### Phase 7 — Authoring workflow and controlled expansion

Work:

- design a dedicated Drip Campaigns Sheet/skill if needed;
- preserve atomic campaign version publication;
- expand sender/ICP coverage one reviewed campaign version at a time;
- consider additional delivery policies only with explicit product requirements.

## 16. Required verification matrix

Configuration and publication:

- malformed definitions fail closed;
- unknown sender/ICP/channel/placeholder fails closed;
- cross-channel dependency fails publication;
- published version digest cannot change;
- active enrollments remain on frozen content after new publication.

State and timing:

- exactly one automation-owning enrollment per Lead;
- same-channel dependencies work;
- channel-independent delivery works;
- boundary times and expiry are deterministic;
- late acceptance skips stale beats and queues at most one current beat;
- no catch-up burst;
- minimum same-channel gaps are respected.

Channel isolation:

- LinkedIn pending while Gmail sends;
- LinkedIn retry/failure/uncertainty while Gmail continues;
- Gmail missing email/failure/uncertainty while LinkedIn continues;
- one completed lane does not complete or fail the sibling lane.

Stop and handoff:

- new inbound LinkedIn stops both lanes;
- new inbound Gmail stops both lanes;
- Meeting and suppression stop both;
- admission policy for historical inbound is explicit;
- optional human outbound pause works;
- running handlers recheck before send.

Queue and ownership:

- wrong sender cannot claim `drip_linkedin`;
- browser daemon cannot claim `drip_gmail`;
- atomic delivery and Task materialization under concurrent workers;
- one Delivery produces at most one executable Task at a time;
- stale attempted sends become uncertain rather than retrying;
- all payload validation and task timing queries include new types.

Legacy regression:

- no enrollment + all flags false equals current behavior;
- active drip suppresses every legacy enqueue/heal/send path;
- connection and sweep paths remain unchanged;
- current follow-up, Gmail, manual reply, feed, discovery, enrichment, and status tests pass;
- full suite passes on the project’s supported PostgreSQL target before production.

Live verification:

- bounded internal/test Gmail send;
- bounded already-connected LinkedIn send with the exact operator;
- simulated and real reply-stop checks;
- process-kill recovery around pre-send and post-send boundaries;
- no broad production cohort until all uncertainty paths are visible and fail closed.

## 17. Rollback strategy

Rollback is flag-first and non-destructive:

1. Disable `ENABLE_DRIP_GMAIL` and/or `ENABLE_DRIP_LINKEDIN`.
2. Stop the dedicated drip worker.
3. Leave enrollment, delivery, and attempt audit rows intact.
4. Mark active enrollments paused; do not delete them.
5. Do not automatically reactivate legacy follow-ups.
6. Review each enrollment before explicit release/handback.

Database migrations add tables and nullable/backward-compatible queue/rate fields. Rollback should not require dropping tables in an incident. Existing outbound continues for Leads never owned by drip.

## 18. Deliberate shared-code budget

Most implementation belongs under `drip/`. Shared edits should be limited to:

- `linkedin/django_settings.py`: install the new app;
- `linkedin/models.py`: new Task choices, validation, routing, and nullable ActionLog campaign;
- one new LinkedIn migration for shared-model changes;
- `linkedin/daemon.py`: register and safely route the LinkedIn drip handler;
- `linkedin/conf.py` and `linkedin/env_spec.py`: default-off flags/limits;
- `linkedin/tasks/connect.py`/`linkedin/daemon.py` legacy scheduling guards only when Phase 5 begins;
- `gmail/handoff.py` and legacy Gmail handler guards only when Phase 5 begins;
- `AGENTS.md` and `ARCHITECTURE.md`: synchronized operational documentation.

Do not refactor existing connect, sweep, follow-up, or Gmail sequencing merely to make the new code look unified. Isolation and regression safety are more valuable than superficial reuse.

## 19. Goal execution order

Execute as separate reviewable goals/commits rather than one large change:

1. Clean branch/worktree and baseline verification.
2. Drip app, schema, models, publication, and docs.
3. Shadow coordinator and state-machine tests.
4. Independent Gmail execution and pilot tooling.
5. LinkedIn bridge and operator/rate safety.
6. Mutual exclusion and multichannel pilot.
7. Optional source-campaign enrollment rules.
8. Authoring workflow and broader rollout.

Each goal must satisfy its exit gate and full relevant regression tests before the next goal begins. No phase should enable production sending merely because its code has merged.
