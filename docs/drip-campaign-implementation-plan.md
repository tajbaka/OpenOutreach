# Independent Multichannel Drip Campaigns — Revised Implementation Plan

Status: agreed implementation plan

Date: 2026-08-29

Scope: an intent/theme-based LinkedIn + Gmail drip subsystem in the existing OpenOutreach repository that remains isolated from the current outbound connection and post-connection flows wherever possible.

This revision replaces the earlier enrollment-calendar, delivery-window, always-on coordinator, and feature-flag design. It incorporates the agreed same-channel ordering, existing-flow handoff, Gmail threading, periodic task materialization, and cross-channel reply-stop rules.

## 1. Final architecture decisions

1. Drip stays in this repository. It is not a new repository or standalone product.
2. Drip is a separate Django app with its own models, migrations, services, commands, tests, and section in the existing Django Admin.
3. The existing connection campaigns continue to own discovery, qualification, connection requests, invitation notes, acceptance detection, and their current post-connection sequences.
4. An enrollment may be created while those current sequences are still running, but each drip channel waits for its own current-channel predecessor gate before it takes automation ownership.
5. One campaign definition contains the ordered themes and both channel renditions. LinkedIn and Gmail are not maintained in unrelated JSON files.
6. LinkedIn and Gmail progress independently. A prerequisite or failure in one channel never blocks the other channel.
7. Dependencies are same-channel only. A Gmail message never requires a LinkedIn message, and a LinkedIn message never requires a Gmail message.
8. Later drips have no campaign-calendar due date. Drip 2 begins after Drip 1 completes in that same channel, even when Drip 1 completed late.
9. `sent_at` timing applies only between multiple messages inside the same drip rendition.
10. Any exactly attributable inbound reply on LinkedIn or Gmail stops all remaining automated messages for that Lead and hands control to a human.
11. The periodic `reconcile_drips` command decides what is eligible and materializes Tasks. It does not send and is not a continuously running coordinator.
12. New `drip_linkedin` Tasks are consumed by the existing sender-scoped LinkedIn daemon, preserving one browser executor, current pacing, and anti-bot controls per LinkedIn account.
13. New `drip_gmail` Tasks are consumed by the shared Gmail worker. That worker must be independently supervised rather than living inside the LinkedIn daemon, so Gmail continues if the browser process fails.
14. Drip uses new task types and handlers. It reuses safe low-level send primitives, but it does not route its lifecycle through the current `FOLLOW_UP` or `GMAIL_FOLLOW_UP` handlers.
15. No new drip, materialization, channel, or Gmail-threading environment flags are added. Database state and explicit commands are the controls.
16. Gmail threading is always implemented correctly for both the current Gmail sequence and drip Gmail; it is not an optional mode.
17. Drip never changes `Deal.state`.

## 2. Repository baseline and current gaps

Implementation branch and worktree:

- branch: `codex/drip-campaigns`;
- clean sibling worktree: `/Users/admin/Desktop/Projects/OpenOutreach-drip-campaigns`;
- current plan commit before this revision: `4bf949c`;
- mainline base: `cbbf2da`;
- retained CRM prerequisites: `f8a6619` and `2733157`;
- the dirty `temp` worktree remains out of scope and must not be stashed, reset, switched, or committed as part of drip work.

The repository already has the core of a cross-channel stop check: `automation_stop_reason(deal)` queries persisted inbound LinkedIn and Gmail `Message` rows. The current LinkedIn and Deal-backed Gmail post-connection handlers consult it. That is useful but not yet the universal guarantee required here.

Verified gaps to fix before drip sending:

- the stop predicate requires a `Deal` instead of a `Lead`;
- Gmail tasks without `deal_id` skip the shared reply check;
- current LinkedIn follow-up does not refresh the exact live conversation immediately before its send check;
- some enqueue/recovery paths can still create Tasks for a replied Lead, even though the handler later no-ops;
- inbound persistence does not centrally stop active enrollments and retire their pending Tasks;
- cross-channel correctness depends on the latest reply having been persisted;
- current Gmail steps call a fresh-send API and do not explicitly continue the same Gmail thread;
- current Gmail persistence can confuse Gmail message IDs and thread IDs;
- broad Gmail thread search and non-exact participant attribution are too permissive for a send gate;
- `GmailWorker` currently runs inside the LinkedIn daemon and uses a non-atomic read-then-mark claim.

Phase 0 fixes these shared safety seams first, with regression tests for the current flow, before any drip Task can send.

## 3. Product model: ordered themes with independent channel lanes

A campaign is an ordered sequence of thematic drips. Each drip states the point the campaign needs to communicate and supplies independently understandable LinkedIn and Gmail versions.

```text
Drip 1 — Name the problem
  LinkedIn rendition: one or more LinkedIn messages
  Gmail rendition: one or more emails

Drip 2 — Explain the point of view
  LinkedIn rendition: one or more LinkedIn messages
  Gmail rendition: one or more emails

Drip 3 — Provide proof
  LinkedIn rendition: one or more LinkedIn messages
  Gmail rendition: one or more emails

Any qualifying reply on either channel -> stop both lanes -> human takeover
```

The theme pairing keeps the campaign coherent without forcing the channels to remain synchronized. It is expected and valid for Gmail to be on Drip 3 while LinkedIn is still waiting to begin Drip 1 because the connection request has not been accepted.

### 3.1 Same-channel dependency rule

For each channel independently:

- the first configured drip rendition waits for that channel's current post-connection sequence gate;
- a later rendition waits for the nearest earlier configured rendition in the same channel to be completed;
- a rendition is completed only when all of its configured messages were successfully sent;
- failed or uncertain delivery does not count as completion and pauses that channel for retry or review;
- a drip that omits a channel is not a dependency in that channel's chain.

There are no cross-channel predecessor fields or policies.

### 3.2 Timing rule

The first message of a newly eligible drip rendition has no offset from the previous drip. It becomes eligible when the predecessor rendition completes and is materialized on the next `reconcile_drips --apply` run.

Only message 2 or later inside the same rendition has a delay:

```text
eligible_at(message N) = sent_at(message N - 1) + delay_after_previous_sent_hours
```

Consequences:

- Drip 2 never looks at Drip 1's final `sent_at` to construct a new campaign date;
- there is no original Drip 2 due date to miss;
- if Drip 1 completes late, Drip 2 simply becomes eligible then;
- there are no delivery windows, expiry dates, calendar shifts, or catch-up bursts;
- the materializer creates at most the next eligible message for each channel lane.

The initial production schedule may run reconciliation daily. Increasing that cadence later changes delivery latency, not campaign semantics.

## 4. Handoff from the current outbound flow

Drip is separate from the current outbound system, but the first drip must not overlap messages already owned by that system. Handoff is per Lead, operator, and channel.

An enrollment may exist in a waiting state before either handoff completes. It does not retire pending or running current-sequence work.

### 4.1 First LinkedIn drip gate

The first LinkedIn drip rendition requires all of the following:

1. The Lead-level global stop predicate is clear.
2. The exact LinkedIn sender/operator is known.
3. The current LinkedIn connection/post-connection sequence for that Lead and sender is completed, or a reviewed enrollment explicitly records it as not applicable.
4. There is no pending or running current `follow_up` Task for that Lead and sender.
5. The Lead has a canonical LinkedIn identity and positive sender-ownership evidence.
6. The Lead is connected; the LinkedIn handler verifies live first-degree status immediately before sending.

Do not infer completion merely from the absence of a Task or from `Deal.state` alone. Use current sequence Message evidence, Task state, sender ownership, and a recorded handoff decision.

If the connection request remains pending or fails, the LinkedIn lane keeps waiting. Gmail continues independently.

### 4.2 First Gmail drip gate

The first Gmail drip rendition requires:

1. The Lead-level global stop predicate is clear.
2. The exact Gmail operator mailbox and Lead email are known.
3. Any current Gmail post-connection sequence that actually started is completed.
4. There is no pending or running current `gmail_follow_up` Task for the Lead and operator.
5. If no current Gmail sequence was started, the enrollment records an explicit `not_applicable` gate rather than treating missing Tasks as proof of completion.

Gmail never waits for LinkedIn acceptance, LinkedIn sequence completion, LinkedIn delivery, or LinkedIn health.

This means a Lead whose connection request is never accepted can still enter the Gmail drip lane. Once that lane takes ownership, a later LinkedIn acceptance must not cause the current Gmail handoff to start a competing Gmail sequence.

### 4.3 Per-channel ownership transfer

When a channel's first-drip gate is satisfied, one transaction:

1. records the current-sequence evidence or explicit not-applicable decision;
2. stamps `DripLane.automation_owned_at`;
3. makes the first drip rendition eligible;
4. prevents future current-sequence enqueue, recovery, or handoff for that Lead/operator/channel.

Before that timestamp, the current sequence remains authoritative for the channel. After it, the drip lane is authoritative. Version 1 does not support force-taking over a pending or running current sequence.

Pausing drip after ownership transfer does not silently return the channel to the current flow. Handback requires an explicit reviewed release.

## 5. Campaign JSON contract

Definitions live in a separate namespace and never rewrite current campaign copy:

```text
drip/
  campaigns/
    fedramp-core/
      v1/
        manifest.json
```

One manifest contains the ordered intents and paired channel copy:

```json
{
  "schema_version": 1,
  "campaign_key": "fedramp-core",
  "version": 1,
  "name": "FedRAMP core multichannel narrative",
  "drips": [
    {
      "key": "readiness_problem",
      "intent": "Explain the recurring burden of point-in-time readiness."
    },
    {
      "key": "persistent_evidence",
      "intent": "Explain the value of continuously maintained evidence."
    }
  ],
  "audiences": {
    "Arian": {
      "CSPs": {
        "variants": [
          {
            "key": "a",
            "gmail_thread_subject": "A question about readiness at {company_name}",
            "drips": {
              "readiness_problem": {
                "linkedin": {
                  "messages": [
                    {
                      "key": "opening",
                      "body": "Hi {first_name}, ..."
                    },
                    {
                      "key": "nudge",
                      "delay_after_previous_sent_hours": 72,
                      "body": "One other thought, ..."
                    }
                  ]
                },
                "gmail": {
                  "messages": [
                    {
                      "key": "opening",
                      "body": "Hi {first_name}, ..."
                    }
                  ]
                }
              },
              "persistent_evidence": {
                "linkedin": {
                  "messages": [
                    {
                      "key": "opening",
                      "body": "The reason I mention it is ..."
                    }
                  ]
                },
                "gmail": {
                  "messages": [
                    {
                      "key": "opening",
                      "body": "The reason I mention it is ..."
                    }
                  ]
                }
              }
            }
          }
        ]
      }
    }
  }
}
```

Contract rules:

- strict schema validation;
- unique campaign, drip, variant, and message keys;
- manifest order is the drip order;
- known canonical senders and ICPs only;
- one or both channel renditions may exist for a drip;
- the first message in a rendition has no predecessor delay;
- every later message requires a finite nonnegative delay from the previous successful send;
- no enrollment offsets, due days, valid-for windows, expiry, or cross-channel dependency fields;
- allowlisted placeholders only;
- deterministic paired-variant selection from enrollment ID and drip key;
- complete copy coverage for every published sender/ICP/channel combination;
- `gmail_thread_subject` is the subject for a newly opened Gmail thread; an exact inherited current-sequence thread keeps its existing subject;
- canonical serialization and SHA-256 digest;
- a campaign/version digest is immutable after publication.

Runtime never rereads mutable JSON for an active enrollment. Publication stores an immutable definition snapshot. Enrollment freezes campaign version, ICP, sender, selected variant, subject, and rendered message bodies.

## 6. Separate Django app and data model

Create a top-level `drip` Django app. It appears as a separate section in the existing Django Admin; no custom frontend is required for version 1.

### 6.1 `DripCampaign`

- stable campaign key and human-readable name;
- status: `draft`, `active`, `paused`, `archived`;
- active published version;
- created/updated timestamps.

Campaign status is the operational control. There is no drip environment enable flag.

### 6.2 `DripCampaignVersion`

- campaign FK and numeric version;
- schema version;
- canonical immutable definition snapshot;
- SHA-256 digest;
- publication timestamp and publisher metadata.

Constraint: unique campaign + version. Published versions are read-only in services and Admin.

### 6.3 `DripEnrollment`

- UUID primary key;
- campaign version and Lead FKs;
- SalesOwner/operator and snapshotted canonical handle;
- frozen ICP and variant mapping;
- optional source Deal/Campaign attribution;
- trigger kind;
- status: `active`, `paused`, `stopped`, `completed`;
- optional reviewed `reply_baseline_at` for deliberate re-engagement;
- stop/handoff reason, detail, and triggering Message/Meeting;
- created, updated, stopped, and completed timestamps.

Default constraint: at most one nonterminal drip enrollment per Lead. Multiple operators or drip campaigns may not automate the same person concurrently.

### 6.4 `DripLane`

One row per enrollment/channel:

- channel: `linkedin` or `gmail`;
- status: `waiting_current_sequence`, `waiting_prerequisite`, `waiting_connection`, `active`, `paused`, `stopped`, `completed`;
- current-sequence gate: `pending`, `completed`, `not_applicable`;
- evidence and decision metadata for that gate;
- `automation_owned_at` and optional reviewed release timestamp;
- latest successful channel-sync timestamp and sync error for observability;
- channel-specific failure/review reason;
- Gmail-owned thread metadata where applicable.

A lane state change never directly advances, pauses, or fails its sibling. Only a global stop changes both.

### 6.5 `DripRendition`

One row per enrollment + drip + configured channel:

- drip key and ordered position;
- channel and previous same-channel rendition FK;
- frozen intent and variant key;
- status: `waiting_predecessor`, `ready`, `active`, `paused`, `stopped`, `completed`;
- started/completed timestamps.

Constraint: unique enrollment + drip key + channel.

### 6.6 `DripDelivery`

One row per rendered message inside a rendition:

- UUID primary key;
- rendition FK and message key/index;
- frozen subject where applicable and body;
- previous-message FK inside the same rendition;
- delay after previous successful send;
- computed `eligible_at` only for within-rendition messages;
- status: `waiting`, `eligible`, `queued`, `sending`, `retry_wait`, `sent`, `failed`, `uncertain`, `stopped`;
- retry counters and next-attempt timestamp;
- send-started and sent timestamps;
- provider message/thread IDs and persisted CRM Message FK;
- sanitized error category/detail.

Constraint: unique rendition + message key. Only one nonterminal executable Task may exist for a delivery.

### 6.7 `DripDeliveryAttempt`

Append-only attempt ledger:

- delivery FK and attempt number;
- stable idempotency key;
- optional shared Task FK;
- attempt status and error category;
- `submit_attempted_at` immediately before the external mutation;
- started/finished timestamps;
- provider IDs and reconciliation metadata.

Task rows are execution leases. Drip tables are the source of truth.

## 7. Canonical Lead-level stop and human-handoff policy

Replace the Deal-only API with one canonical Lead-level service, conceptually:

```text
lead_automation_stop_reason(lead, reply_baseline_at=None)
```

It evaluates:

- exactly attributable inbound LinkedIn `Message`;
- exactly attributable inbound Gmail `Message`;
- Meeting for the Lead;
- Lead disqualification or global suppression;
- explicit human takeover;
- optionally, non-automation human outbound after enrollment.

Current post-connection flows call it without a reply baseline, so any historical inbound reply blocks more automation. Automatic drip admission also refuses historical inbound by default. A deliberate, reviewed re-engagement may record a baseline; only inbound after that baseline stops the new enrollment.

### 7.1 Exact reply attribution

LinkedIn inbound attribution must use the exact other participant/thread identity and preserve known connection-note echo protections.

Gmail inbound attribution must require the exact normalized Lead email and correct operator mailbox. A third party in a broad or multi-participant matched thread must not be recorded as that Lead's reply. Automated mail, drafts, bounces, and provider notices are not human replies; bounce/invalid-address handling affects the Gmail lane according to its own policy.

The strict LinkedIn refresh path must preserve participant/member URNs and return a typed outcome that distinguishes `refreshed`, `no_thread`, and `unavailable`. The current best-effort conversation hook may remain for non-send callers, but a send preflight may not treat a swallowed persistence/fetch failure as proof that no reply exists. `unavailable` delays LinkedIn only.

The strict Gmail refresh path must reuse the exact RFC participant and automated-message filtering in `gmail/data_sync.py`. Refresh failure delays Gmail only; it does not change LinkedIn lane state.

### 7.2 Enforcement points

The same stop service is called:

1. before current post-connection enqueue/handoff/recovery;
2. before drip reconciliation materializes a Task;
3. at the start of every current and drip message handler;
4. after refreshing the executor's own exact channel conversation;
5. immediately before every external send boundary.

Required current-flow hardening:

- current LinkedIn follow-up refreshes and persists the exact LinkedIn conversation, then rechecks before sending;
- current Gmail follow-up refreshes exact Gmail state and rechecks even for an email-first Lead with no `Deal` or `deal_id`;
- current enqueue and daemon-heal paths do not create new messaging Tasks after a known stop.

### 7.3 Reply-driven shutdown

New inbound persistence on either channel invokes an idempotent stop service after commit:

1. lock the active enrollment;
2. mark it stopped with the triggering Message;
3. stop both lanes, all nonterminal renditions, and all nonterminal deliveries;
4. retire pending `drip_linkedin` and `drip_gmail` Tasks as completed/no-op with audit context;
5. retire matching pending current LinkedIn/Gmail messaging Tasks when they can be resolved exactly;
6. ensure suppression cleanup includes current Gmail plus both drip task types, not only current LinkedIn follow-up;
7. prevent future current or drip enqueue/materialization;
8. let any already-running handler make the same final pre-send check.

The periodic reconciler repeats this check, so correctness does not depend solely on callbacks. Send-time checks remain authoritative for races.

### 7.4 Accepted cross-channel synchronization gap

Each executor refreshes its own channel and consults the latest persisted state from both channels. Channel-sync freshness is recorded and visible, but one channel's health is never a prerequisite for the other channel to send.

Therefore, if LinkedIn is unavailable and a new LinkedIn reply has not yet been ingested, Gmail may send one otherwise eligible message. Gmail must not wait for or fail because LinkedIn could not be refreshed. As soon as the LinkedIn listener, sweep, or backfill persists that reply, all remaining current and drip automation stops.

This availability-first bounded gap is explicitly accepted. The system must expose it honestly rather than claiming atomic knowledge across two external providers.

## 8. Periodic reconciliation and Task materialization

Add `manage.py reconcile_drips`:

- dry-run by default;
- `--apply` performs database mutations and Task creation;
- safe and idempotent under repeated or concurrent execution;
- suitable for a daily scheduler initially;
- never sends an external message.

For each active campaign/enrollment it:

1. applies the global stop policy;
2. evaluates each lane independently;
3. resolves the first-drip current-sequence gate or the later same-channel rendition predecessor;
4. checks within-rendition `sent_at` timing;
5. checks channel-specific eligibility such as valid email or stored connection evidence;
6. locks the delivery and confirms no executable Task already exists;
7. creates at most the next eligible Task per lane;
8. reports waiting, blocked, stopped, and materialized decisions with reason codes.

The reconciler does not self-loop, sleep, maintain browser sessions, or own provider clients. Scheduled invocation is the materialization switch; campaign/enrollment status is the product switch. There is no `ENABLE_DRIP_MATERIALIZATION` setting.

## 9. Shared queue, separate task types, and process topology

Add two `Task.TaskType` values:

- `drip_linkedin`;
- `drip_gmail`.

Payloads stay minimal:

```json
{
  "delivery_id": "<uuid>",
  "operator": "Arian"
}
```

Handlers load every lifecycle, timing, copy, identity, and deduplication field from locked drip rows.

### 9.1 Why not reuse current task types

The current `FOLLOW_UP` handler correctly enforces the current sequence's assumptions: it requires its campaign-scoped Deal state, uses current sequence metadata/timing, and updates `Deal.state`. The current `GMAIL_FOLLOW_UP` handler likewise owns current sequence rendering and self-enqueue behavior.

Those are correct for their existing jobs but not for drip lifecycle. New task types prevent either handler from accidentally mutating current campaign state or applying the wrong predecessor/timing rules. The same machines and safe low-level provider functions are reused.

### 9.2 LinkedIn execution

The existing sender-specific LinkedIn daemon claims `drip_linkedin` alongside current browser work.

Required narrow routing changes:

- operator-scope `drip_linkedin` exactly like other sender-owned browser Tasks;
- exclude it from account-agnostic claiming;
- add handler dispatch, `Task.clean()` payload validation, stale recovery, and wake-time support;
- place it after current connection/follow-up delivery in deliberate priority;
- share the existing Follow Up action cap and add a conservative drip sub-cap;
- preserve single-threaded browser execution so drip and current outreach never operate LinkedIn simultaneously for the same account.

`ActionLog.campaign` must become nullable for drip attribution, and the existing current callers must continue passing their Campaign. Drip records the same Follow Up action type with `campaign=None`, so current global/daily limits count both systems.

### 9.3 Gmail execution

Turn the current Gmail worker into an independently supervised process, for example `manage.py run_gmail_worker --operator Arian`.

Before adding drip Gmail it must:

- atomically claim Tasks instead of read-then-mark;
- process current `gmail_follow_up` Tasks exactly as today;
- be removed from the LinkedIn daemon's in-process lifecycle;
- use existing Gmail account mappings/credentials;
- recover only genuinely stale, age-qualified work owned by its task type/operator rather than resetting every running Gmail Task at startup;
- keep running when the LinkedIn daemon/browser exits.

Then extend its explicit handler map to claim `drip_gmail`. One operator-scoped worker processes both current and drip Gmail Tasks, preventing competing Gmail executors for that mailbox.

The deployment supervisor and Docker/service entrypoints must start this standalone worker. The in-daemon worker and standalone worker may not overlap during cutover; atomic claims are defense in depth, not permission to run two mailbox executors indefinitely.

## 10. Drip send handlers

### 10.1 `drip_linkedin`

1. Atomically claim the sender-scoped Task.
2. Lock enrollment, lane, rendition, delivery, and attempt.
3. Recheck campaign/enrollment/lane state, ownership, predecessor, timing, stop policy, and idempotency.
4. Resolve the exact Lead and sender-owned LinkedIn identity.
5. Fetch and persist the exact live LinkedIn conversation.
6. Require the strict refresh result; `unavailable` delays LinkedIn and cannot be interpreted as no reply.
7. Re-run the global stop policy.
8. Verify live first-degree connection under that exact sender browser.
9. Recheck the send boundary and stamp `submit_attempted_at`.
10. Call the existing low-level LinkedIn message primitive with drip-specific deterministic persistence metadata.
11. Persist CRM Message, attempt, delivery, rendition, and lane state without changing `Deal.state`.

If not connected, return the lane to `waiting_connection`; do not fail Gmail or the enrollment. Provider failure/retry/uncertainty affects only LinkedIn.

### 10.2 `drip_gmail`

1. Atomically claim the operator-scoped Task.
2. Lock enrollment, lane, rendition, delivery, and attempt.
3. Recheck campaign/enrollment/lane state, predecessor, timing, exact email, stop policy, and idempotency.
4. Refresh the exact Lead/operator Gmail conversation or owned thread.
5. Require a successful exact Gmail refresh; unavailable Gmail delays Gmail only.
6. Re-run the global stop policy without requiring LinkedIn freshness.
7. Resolve the exact current/drip thread binding.
8. Recheck the send boundary and stamp `submit_attempted_at`.
9. Send through a typed Gmail result API returning provider message ID, actual thread ID, and RFC message metadata.
10. Persist CRM Message, attempt, delivery, rendition, lane, and thread state.

It never checks `Deal.state`, LinkedIn connection, LinkedIn delivery outcome, or LinkedIn process health.

## 11. Gmail threading is a required shared fix

The current Gmail sequence must be corrected before the Gmail drip pilot.

Rules:

1. The first current-sequence email opens the sequence's exact Gmail thread.
2. Every later current-sequence email while unanswered replies in that same thread.
3. If an exact current-sequence thread exists at Gmail-lane handoff, the first drip email continues that known thread.
4. If the current Gmail sequence was explicitly not applicable and no owned thread exists, the first drip email opens one enrollment-owned thread.
5. Every later Gmail message in that drip enrollment, including later thematic drips, replies in that exact thread while unanswered.
6. Never choose an arbitrary historical unanswered thread by subject or broad search.
7. Never fake continuity by prepending `Re:` to a newly created message.
8. If a required owned thread cannot be recovered exactly, pause for review; do not silently open a duplicate thread.

Implementation requirements:

- Gmail API request includes the actual provider `threadId` for continuation;
- MIME includes correct `In-Reply-To` and `References` using RFC `Message-ID` values;
- the continuing subject matches the established thread subject;
- Gmail API message ID, Gmail thread ID, and RFC Message-ID are stored as distinct values;
- `crm.Message.thread_external_id` stores the real Gmail thread ID;
- generated RFC Message-ID is deterministic per delivery for recovery;
- exact Lead email/operator mailbox attribution replaces broad-query assumptions;
- API-request-level tests inspect new-thread and reply-thread payloads;
- existing current Gmail tests prove all sequence steps share one thread.

There is no `ENABLE_GMAIL_THREADING` setting. Correct threading is the only behavior.

## 12. External-send crash safety

No database transaction can make an external provider send exactly once. Preserve an explicit uncertainty boundary:

- stamp `submit_attempted_at` immediately before provider mutation;
- pre-submit recoverable errors may enter bounded `retry_wait`;
- a stale attempt with `submit_attempted_at` becomes `uncertain`, never an automatic retry;
- uncertainty pauses only that channel lane;
- every recovery result is audited.

LinkedIn recovery fetches the exact sender-owned thread and reconciles only an exact frozen-body/sender/bounded-time match.

Gmail recovery searches the deterministic RFC Message-ID and reconciles only exact provider evidence.

If proof is ambiguous, keep the delivery uncertain and require review rather than risk a duplicate send.

## 13. Coexistence boundaries

The following remain current-flow responsibilities:

- discovery and qualification;
- connection requests and invitation notes;
- connection acceptance sweeps;
- current post-connection LinkedIn and Gmail sequences until their respective handoff;
- manual replies, feed engagement, enrichment, status summaries, and CRM publication.

Drip reads Lead, sender, ICP, Message, Meeting, connection, and Task evidence. It does not rewrite current campaign definitions or use `Deal.state` as its lifecycle.

Narrow defense-in-depth guards are added only where necessary:

1. current LinkedIn enqueue/recovery no-ops after the LinkedIn drip lane owns automation;
2. current Gmail handoff/enqueue no-ops after the Gmail drip lane owns automation;
3. current handlers no-op if ownership changed after their Task was queued;
4. daemon healing excludes only the Lead/operator/channel already owned by drip;
5. connection acquisition and acceptance detection remain eligible even when Gmail drip ownership has transferred.

An active enrollment waiting for current-channel completion does not suppress that current channel prematurely.

## 14. Operational controls and commands

No new environment feature flags are introduced. Existing current-flow settings remain untouched.

Controls are explicit database state:

- Campaign: `draft`, `active`, `paused`, `archived`;
- Enrollment: `active`, `paused`, `stopped`, `completed`;
- Lane: waiting/active/paused/stopped/completed states;
- optional later auto-enrollment rules have their own database status.

Commands default to validation or dry-run where mutation is not intrinsic:

- `validate_drip_campaigns`;
- `publish_drip_campaign`;
- `plan_drip_enrollment`;
- `enroll_drip --apply`;
- `reconcile_drips [--apply]`;
- `drip_status`;
- `pause_drip` / `resume_drip`;
- `pause_drips --all --apply` for an operational emergency stop;
- `release_drip_lane --apply`;
- shared `run_gmail_worker --operator <handle>`.

Validation, publication, enrollment planning, enrollment creation, and reconciliation itself never call a provider send API. Only queue workers send approved active deliveries.

## 15. Implementation phases and release gates

### Phase 0A — Harden the current reply-stop invariant

Work:

- introduce the Lead-level stop service;
- retain a narrow Deal wrapper only for current callers during migration;
- enforce it in current enqueue, heal, handler-start, refresh, and pre-send paths;
- refresh the exact LinkedIn conversation before current LinkedIn sends;
- enforce exact Gmail refresh and stop checks for Gmail Leads with or without a Deal;
- add inbound-persistence stop hooks and Task cleanup;
- expose channel-sync freshness.

Exit gate:

- persisted reply on either channel blocks both current post-connection channels;
- latest same-channel reply is found by pre-send refresh;
- no-Deal Gmail is protected;
- exact reply attribution tests pass;
- all current-flow regression tests pass.

### Phase 0B — Correct Gmail threading and process independence

Work:

- add typed Gmail send results and exact provider/RFC identifiers;
- thread the existing current Gmail sequence correctly;
- replace broad send-gate attribution with exact thread/address logic;
- add atomic Gmail Task claiming;
- run GmailWorker as an independently supervised process;
- remove its in-process dependency on the LinkedIn daemon.

Exit gate:

- current Gmail sequence uses one exact thread;
- request-level threading tests pass;
- one Gmail worker cannot double-claim;
- current Gmail Tasks continue while the LinkedIn process is stopped;
- no drip models or sends are needed to prove the shared fix.

### Phase 1 — Drip domain, publication, and Admin

Work:

- add the separate `drip` app;
- add strict manifest schema/loader/publisher;
- add campaign, version, enrollment, lane, rendition, delivery, and attempt models;
- add migrations and a separate Admin section;
- add dry-run enrollment planning with per-channel current-sequence gates;
- add nonproduction example definitions.

No Task types or provider send calls.

Exit gate:

- immutable publication, deterministic rendering, constraints, Admin state, and explicit handoff decisions are proven;
- the entire current suite remains unchanged.

### Phase 2 — Periodic reconciler in dry-run

Work:

- implement the same-channel state machine;
- implement first-drip current-sequence gates;
- implement within-drip timing from previous successful `sent_at`;
- implement global stops, per-channel failure isolation, and status reporting;
- run `reconcile_drips` without `--apply` against reviewed nonproduction data.

Exit gate:

- no external send or executable drip Task is possible;
- scenario matrix proves late predecessor completion, independent channel positions, and no calendar/expiry behavior.

### Phase 3 — Gmail Task materialization and pilot

Work:

- add `drip_gmail` Task routing to the independent Gmail worker;
- enable `reconcile_drips --apply` to atomically materialize eligible Gmail deliveries;
- implement Gmail handler, thread continuation, attempt ledger, and uncertainty recovery;
- manually enroll a tiny internal/allowlisted cohort;
- leave LinkedIn drip without an executable handler until Phase 4.

Exit gate:

- Gmail sends regardless of LinkedIn connection/process health;
- current-sequence handoff prevents overlap;
- Gmail reply stops the full enrollment;
- crash recovery never auto-duplicates;
- current Gmail behavior remains correct outside owned drip lanes.

### Phase 4 — LinkedIn Task materialization and pilot

Work:

- add `drip_linkedin` operator scoping, routing, priority, and stale recovery;
- implement live thread refresh, reply stop, and first-degree verification;
- add deterministic persistence, action accounting, and uncertain-send recovery;
- pilot reviewed already-connected Leads under the exact owner account.

Exit gate:

- wrong sender cannot claim;
- pending/nonconnected Leads cannot send;
- current-sequence handoff prevents overlap;
- LinkedIn failure never blocks Gmail;
- no `Deal.state` mutation.

### Phase 5 — Manual multichannel pilot

Work:

- enable both renditions for a tiny manually reviewed cohort by activating database campaign/enrollment state;
- exercise late LinkedIn acceptance, channel drift, multi-message delays, provider failures, replies, pauses, restarts, and explicit release;
- verify the accepted LinkedIn-outage/Gmail-send behavior and subsequent stop after ingestion.

Exit gate:

- zero duplicate current+drip sends;
- reply on either channel stops all known remaining automation;
- each channel advances only from its own predecessors;
- pause never silently hands ownership back.

### Phase 6 — Optional source-campaign enrollment

Only after the manual pilot, add database-backed `DripEnrollmentRule` records mapping a current source Campaign/operator/cohort to a drip version and trigger such as invitation sent or connection accepted.

Rules remain inactive until explicitly activated in Admin. The periodic reconciler enrolls idempotently from existing database evidence; current connect/sweep send paths receive no new provider calls.

Exit gate:

- stable bounded cohort over an agreed observation period;
- no regression in connect/sweep throughput;
- no wrong-sender, duplicate-send, or cross-channel-block incidents;
- documented rollback and ownership-release procedure.

### Phase 7 — Authoring workflow and expansion

After runtime behavior is proven, design a dedicated Drip Campaigns Sheet/skill if needed. It must preserve one atomic manifest/version containing both channel renditions and may not rewrite the current ICP message files.

## 16. Release-blocking verification matrix

### Current-flow reply safety

- Gmail inbound blocks current LinkedIn follow-up;
- LinkedIn inbound blocks current Gmail follow-up;
- no-Deal/email-first Gmail task is blocked by inbound;
- current LinkedIn pre-send refresh catches a new LinkedIn reply;
- current Gmail pre-send refresh catches a new Gmail reply;
- enqueue and healing do not recreate messaging work after a known reply;
- exact Lead address/participant attribution excludes unrelated participants.

### Gmail correctness and independence

- current sequence step 2+ uses the exact thread from step 1;
- provider message ID, provider thread ID, and RFC Message-ID remain distinct;
- reply headers and subject are correct;
- drip continues an exact eligible current thread or creates exactly one owned thread when none exists;
- missing owned thread fails closed instead of opening a duplicate;
- atomic Gmail claims prevent two workers from sending one Task;
- Gmail current and drip Tasks run while LinkedIn is stopped.

### Drip ordering and timing

- first LinkedIn drip waits for current LinkedIn completion and live connection;
- first Gmail drip waits only for current Gmail completion/not-applicable state;
- Gmail can advance while LinkedIn waits;
- LinkedIn can advance while Gmail is paused;
- Drip N waits only for the previous configured rendition in that channel;
- late predecessor completion makes the next drip eligible without expiry;
- only same-drip later messages calculate from previous successful `sent_at`;
- failed/uncertain messages do not complete a rendition;
- one next executable delivery per lane; no backlog burst.

### Cross-channel stop and handoff

- new inbound LinkedIn stops both drip lanes;
- new inbound Gmail stops both drip lanes;
- reply during either current sequence prevents either drip lane from starting;
- inbound hook retires pending drip Tasks;
- handler-start and final pre-send checks protect races;
- Meeting, suppression/disqualification, and explicit human takeover stop both;
- historical inbound admission is blocked unless reviewed re-engagement records a baseline;
- LinkedIn outage does not block Gmail, and a subsequently ingested LinkedIn reply stops all remaining work.

### Queue, identity, and crash safety

- wrong LinkedIn sender cannot claim;
- LinkedIn daemon cannot claim `drip_gmail`;
- Gmail worker cannot claim browser Tasks;
- concurrent reconcilers materialize one Task per delivery;
- current and drip ownership cannot overlap within a Lead/operator/channel;
- stale post-submit attempts become uncertain, not automatic retries;
- recovery needs exact provider evidence.

### Regression

- no drip enrollment means current behavior remains unchanged except the intentional Phase 0 safety/threading fixes;
- connection, sweep, current follow-up, Gmail, manual reply, feed, discovery, enrichment, status, CRM, and Admin tests pass;
- full suite passes on the supported PostgreSQL target before production.

## 17. Rollout and rollback

Roll out one reviewed goal/commit at a time. No phase becomes production-active merely because its code merged.

Emergency pause is database- and scheduler-based:

1. Pause the affected DripCampaign and active enrollments in Admin.
2. Stop scheduled `reconcile_drips --apply` invocations.
3. Leave the shared LinkedIn daemons and independent Gmail workers running for current flows.
4. Queued drip handlers recheck paused/stopped state and complete as no-ops.
5. Preserve enrollment, delivery, attempt, Message, and Task audit records.
6. Do not automatically reactivate current messaging for a channel already transferred to drip.
7. Review and explicitly release each owned lane if handback is desired.

Migrations are additive. Incident rollback must not require dropping drip tables or deleting audit state.

## 18. Deliberate shared-code budget

Most implementation belongs under `drip/`. Shared edits are limited to the proven seams:

- `linkedin/django_settings.py`: install the drip app;
- `linkedin/tasks/stop_checks.py`: Lead-level canonical stop service;
- current LinkedIn/Gmail enqueue, heal, refresh, and pre-send guards;
- LinkedIn and Gmail inbound persistence: exact attribution and idempotent stop notification;
- `gmail/client.py`: correct typed send/thread API;
- `gmail/tasks/follow_up.py`: current sequence thread continuation and Lead-level stop checks;
- `gmail/worker.py` plus one management command/supervision seam: atomic independent Gmail execution;
- `linkedin/models.py`: new Task choices, claiming, routing, and wake-time support;
- `linkedin/daemon.py`: LinkedIn drip dispatch and removal of the in-process Gmail worker;
- one narrow rate-accounting change if drip attribution requires a nullable current Campaign;
- `AGENTS.md` and `ARCHITECTURE.md`: synchronized implementation/operations documentation whenever code changes.

Do not refactor current connect, sweep, or post-connection sequencing simply to make the new code look unified. Reuse stable primitives; keep lifecycle state separate.

## 19. Goal execution order

Execute through separate reviewable goals:

1. Lead-level cross-channel stop hardening for current flows.
2. Correct current Gmail threading, exact attribution, and independent atomic Gmail worker.
3. Drip app, manifest, models, migrations, Admin, and publication.
4. Dry-run same-channel reconciler and scenario tests.
5. Gmail Task materialization, handler, and bounded pilot.
6. LinkedIn Task materialization, handler, and bounded pilot.
7. Manual multichannel ownership/handoff pilot.
8. Optional source-campaign enrollment rules.
9. Dedicated authoring workflow and controlled expansion.

Every goal must satisfy its exit gate and relevant full regression suite before the next goal starts.
