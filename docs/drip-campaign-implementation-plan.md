# Drip Campaign Implementation Plan

Status: implementation plan, not yet implemented

Branch: `codex/drip-campaigns`

Scope: add a separate theme-based LinkedIn and Gmail drip subsystem to the existing OpenOutreach repository without replacing or reusing the lifecycle of the current connection and post-connection campaigns.

## 1. Outcome

OpenOutreach will support reviewed drip campaigns that communicate an ordered set of themes across LinkedIn and Gmail.

Each theme carries the point we want to communicate and supplies a channel-appropriate LinkedIn version and Gmail version. The two channels remain independent:

- Gmail can advance when LinkedIn is still waiting for a connection.
- LinkedIn failure does not block Gmail.
- Gmail failure does not block LinkedIn.
- A message depends only on earlier messages in the same channel.
- A persisted human reply on either channel stops all remaining automated outreach for that Lead and hands control to a human.

The subsystem lives in a new `drip` Django app in this repository. It uses the existing persistent `Task` table and existing executors, but it has its own campaign, enrollment, lane, delivery, and attempt lifecycle. Drip code never advances or closes `Deal.state`.

## 2. Settled design decisions

1. Drip is not a separate repository.
2. Drip does not reuse the current `FOLLOW_UP` or `GMAIL_FOLLOW_UP` lifecycle.
3. It introduces `drip_linkedin` and `drip_gmail` Task types.
4. One campaign JSON file contains the ordered themes, ICP mappings, sender-specific copy, and both channel renditions.
5. LinkedIn and Gmail are independent lanes under one enrollment.
6. Handoff from current outreach happens independently per channel.
7. The sender/account used by current outreach is retained after handoff.
8. Day 1 for a channel begins when that channel hands off to drip.
9. Timing within a theme is based only on successful sends in the same channel.
10. A later theme receives a new start when the preceding theme for that lane completes; it is not tied to an original campaign calendar.
11. The periodic reconciler creates due Tasks but never sends messages.
12. LinkedIn drip uses the existing sender-scoped LinkedIn daemon and its single browser.
13. Gmail uses the existing authentication and alias mapping but runs independently from the LinkedIn browser process, with one worker per resolved Gmail account.
14. The existing hourly CRM v2 context workflow is the only Gmail reply-ingestion path.
15. LinkedIn replies continue to arrive through the existing realtime listener and scheduled `backfill_messages` behavior for Deal-backed Leads.
16. Neither channel performs a conversation refresh immediately before a normal send.
17. Gmail threading is corrected for the current Gmail sequence and then inherited by drip.
18. LinkedIn drip uses one delivery route. If a click may have succeeded but cannot be confirmed, the delivery pauses and is not automatically sent through another route.
19. There are no new drip feature flags, Gmail polling services, provider-identity projects, drip-specific inbox recovery systems, or mailbox/sender limit systems.
20. The LinkedIn daemon keeps its existing active-traditional-Campaign startup prerequisite.

## 3. Relationship with current outbound

Current connection and post-connection campaigns remain the acquisition path. Drip begins only after a channel-specific handoff.

```text
Current LinkedIn connection/follow-up sequence
        -> LinkedIn handoff
        -> LinkedIn drip lane

Current Gmail post-connection sequence, or reviewed not-applicable decision
        -> Gmail handoff
        -> Gmail drip lane
```

The handoffs do not have to occur together.

- A pending LinkedIn connection leaves the LinkedIn lane waiting.
- Gmail may hand off and begin while LinkedIn is waiting.
- A later connection acceptance may start the LinkedIn handoff, but it must not restart current Gmail work after Gmail ownership has transferred to drip.
- Pausing drip does not silently return ownership to the current sequence.

### 3.1 LinkedIn handoff gate

The LinkedIn lane may take ownership only when all of the following are true:

1. The Lead belongs to the existing Deal-backed outbound flow.
2. The exact LinkedIn sender is known from current outreach evidence.
3. The Lead is connected according to the existing Deal/connection flow.
4. The current LinkedIn post-connection sequence has completed, proven by its configured final outbound step, or has a reviewed `not_applicable` result. `Deal.state` or the absence of a Task is not sufficient by itself.
5. No current LinkedIn follow-up Task for that Lead and sender is pending or running.
6. The shared stop policy is clear.

Once committed, current LinkedIn enqueue, healing, and handler paths must no-op for that Lead/sender/channel.

### 3.2 Gmail handoff gate

The Gmail lane may take ownership only when all of the following are true:

1. The canonical operator, resolved Gmail account, Send-As alias, and Lead email are known.
2. The current Gmail sequence has completed, proven by persisted outbound step evidence, or a reviewed decision says it never started and is `not_applicable`. A completed or missing Task is not sufficient by itself.
3. No current Gmail follow-up or email-enrichment Task for that Lead/account is pending or running.
4. If current Gmail sent any message, its owned Gmail thread can be identified.
5. The shared stop policy is clear.

Once committed, current Gmail handoff, enqueue, next-step, enrichment, and handler paths must no-op for that Lead/account/channel.

### 3.3 Ownership transaction

Handoff and every current-flow enqueue for the same Lead/channel use one shared short ownership lock. Handoff then:

1. locks the enrollment/lane and relevant current Tasks;
2. rechecks current sequence completion and the shared stop policy;
3. records the source sender/account and handoff evidence;
4. stamps `handed_off_at`;
5. makes the drip lane the sole owner of that Lead/channel combination while preserving the frozen sender/account.

No database lock remains open during a LinkedIn or Gmail provider call.

## 4. Timing semantics

Timing is channel-local and theme-local.

### 4.1 Day 1

For the first theme in a lane:

```text
theme_started_at = handed_off_at
```

For a newly finishing current sequence, handoff occurs when that sequence finishes. For a historical Lead whose current sequence finished before reviewed enrollment, handoff occurs at enrollment activation so old dates do not release an immediate backlog.

Equivalent rule:

```text
first_theme_anchor = max(enrollment_activated_at, current_sequence_completed_at)
```

`delay_days: 0` means Day 1: the next existing active sending window at or after the anchor.

### 4.2 Steps within one theme

- The first step in a channel rendition is due from `theme_started_at`.
- Every later step in that rendition is due from the previous successful same-channel `sent_at`.
- Failed, paused, skipped, or unclear sends never become timing anchors.
- Existing active-hours/rest-day normalization determines the actual LinkedIn send time.

### 4.3 Moving to the next theme

A lane enters Theme N+1 only after its Theme N rendition is complete or explicitly not applicable.

Theme N+1 receives a fresh `theme_started_at`. Its first message does not calculate a due date from a send in Theme N. Therefore, a late Theme N completion does not cause a campaign-calendar catch-up or skip; it simply allows the lane to enter the next theme.

LinkedIn and Gmail may be on different themes at the same time.

## 5. Campaign JSON

Use one reviewed JSON file per drip campaign under `drip/campaigns/`.

It maps existing ICP names to ordered themes. Each theme contains an intent and sender-specific LinkedIn/Gmail renditions.

Illustrative shape:

```json
{
  "schema_version": 1,
  "campaign_key": "fedramp_reengagement",
  "name": "FedRAMP re-engagement",
  "audiences": {
    "CSPs": {
      "themes": [
        {
          "key": "visibility_gap",
          "intent": "Explain why continuous visibility matters.",
          "senders": {
            "Arian": {
              "linkedin": [
                {
                  "delay_days": 0,
                  "body": "LinkedIn rendition"
                }
              ],
              "gmail": [
                {
                  "delay_days": 1,
                  "subject": "Email subject",
                  "body": "Email rendition"
                }
              ]
            }
          }
        }
      ]
    }
  }
}
```

Rules:

- ICP keys must match the project’s canonical ICP values.
- Sender keys use canonical operators; there is no fallback sender.
- A theme has one shared intent but channel copy must stand on its own.
- Copy must not assume the prospect received a message on the other channel.
- `delay_days` is nonnegative and may be fractional.
- The first step’s delay is from the lane’s theme start; subsequent delays are from the previous successful same-channel send.
- An omitted rendition is explicitly not applicable for that lane/theme.
- Subjects exist on Gmail only. The first Gmail delivery establishes the thread subject; later Gmail steps omit it or repeat that exact subject. A different later subject fails validation because the lane remains in one thread.
- Runtime placeholders use a strict allowlist and are rendered/frozen before Task creation.
- Publishing validates the complete file and stores an immutable normalized snapshot and content hash. Existing enrollments never change when the disk file changes.

## 6. Data model

All new lifecycle models live in `drip/models.py`.

### 6.1 `DripCampaign`

- stable key and human name;
- status: `draft`, `active`, `paused`, `retired`;
- active published version;
- timestamps.

Campaign status is the product control. No `ENABLE_DRIP_*` environment flags are added.

### 6.2 `DripCampaignVersion`

- campaign FK and monotonically increasing version;
- normalized manifest JSON and content hash;
- publication timestamp;
- immutable after publication.

An active enrollment always references one exact published version.

### 6.3 `DripEnrollment`

- campaign version and Lead;
- frozen ICP;
- status: `waiting`, `active`, `paused`, `stopped`, `completed`;
- activation/completion/stop timestamps;
- stop reason and triggering `Message` or `Meeting` where available.

Only one nonterminal drip enrollment for a Lead across all drip campaigns is permitted.

### 6.4 `DripLane`

One LinkedIn lane and one Gmail lane per enrollment.

- channel;
- frozen canonical operator;
- resolved provider account and recipient identity;
- status: `waiting_current`, `waiting_connection`, `active`, `paused`, `stopped`, `completed`;
- handoff evidence and `handed_off_at`;
- current theme index/key and `theme_started_at`;
- Gmail thread binding where applicable.

Each enrollment has exactly one lane per channel. The enrollment constraint plus channel ownership lock prevents a second drip campaign or sender from concurrently owning the same Lead/channel.

### 6.5 `DripDelivery`

One rendered channel step.

- lane, theme key/index, and step index;
- frozen subject/body;
- `scheduled_at` and `sent_at`;
- status: `planned`, `queued`, `sending`, `sent`, `failed`, `unclear`, `stopped`;
- concrete link to its one active `Task`;
- persisted outbound `crm.Message` FK;
- provider message/thread metadata.

A database constraint prevents two executable Tasks for one delivery. `Task.payload.delivery_id` is routing data, not the only duplicate barrier.

### 6.6 `DripDeliveryAttempt`

- delivery and attempt number;
- started/submission-attempted/finished timestamps;
- outcome and diagnostic detail.

The attempt ledger records the external-send boundary. An attempt that may have submitted but cannot be confirmed becomes `unclear` and is never automatically retried.

## 7. Shared stop policy

Create one Lead-level service used by current outbound and drip:

```text
lead_automation_stop_reason(lead)
```

It uses existing persisted project state:

- an inbound LinkedIn `crm.Message` for the Lead;
- an inbound Gmail `crm.Message` for the Lead;
- an existing qualifying persisted `Meeting` under current meeting semantics;
- Lead disqualification or existing outreach suppression.

No provider, Granola, Gemini, or conversation fetch occurs inside this check.

Campaign/enrollment/lane pause and stop states are checked separately inside drip services. Pausing a waiting enrollment does not suppress unfinished current outreach before handoff. An enrollment stopped because of a reply is terminal; v1 does not release or re-engage it.

Check the policy:

1. before enrollment activation and channel handoff;
2. before the reconciler creates a delivery Task;
3. at current and drip handler entry;
4. immediately before the provider submission boundary;
5. before current enqueue/healing paths recreate automated work.

For the campaign-wide current `connect` Task, the Task itself remains queued; candidate selection and the final invitation-note boundary skip an exact Lead with a known stop reason.

When an inbound Gmail or LinkedIn message is newly persisted, an idempotent after-commit hook:

1. stops active drip enrollments and both lanes;
2. marks planned/queued deliveries stopped;
3. retires pending `drip_linkedin` and `drip_gmail` Tasks;
4. retires resolvable pending current automated messaging Tasks;
5. leaves human `manual_reply` work untouched.

The hourly Gmail context workflow and LinkedIn listener/backfill are responsible for writing the `crm.Message` that drives this policy.

### Accepted ingestion window

Gmail replies can remain unknown until the next hourly `sync_crm_v2_context --apply` run, or longer if that run fails or defers the thread. LinkedIn replies can remain unknown until the existing listener or backfill persists them. One or more otherwise eligible automated messages may therefore send during an ingestion gap. This availability-first behavior is accepted for this use case.

There is no extra Gmail pre-send reply query and no LinkedIn send-time conversation refresh.

## 8. Reconciler

Add:

```bash
.venv/bin/python manage.py validate_drip_campaign <path>
.venv/bin/python manage.py publish_drip_campaign <path>
.venv/bin/python manage.py plan_drip_enrollments <campaign-key> --lead-id <id> [--lead-id <id> ...]
.venv/bin/python manage.py enroll_drip_campaign <campaign-key> --plan <reviewed-plan.json> --apply
.venv/bin/python manage.py reconcile_drips
.venv/bin/python manage.py reconcile_drips --apply
```

The initial production schedule runs `reconcile_drips --apply` daily.

Enrollment is reviewed and bounded. The planning command requires explicit Lead IDs and writes a stable-ID artifact. Apply accepts that exact reviewed artifact and refuses unlisted or changed Leads. V1 does not automatically enroll every Lead whose ICP matches the campaign JSON.

One reconciliation pass:

1. obtains a global reconciliation lock;
2. stops enrollments with known stop evidence;
3. evaluates independent channel handoffs;
4. advances completed lane themes;
5. renders and freezes the next due step when needed;
6. creates at most one outstanding delivery Task per lane;
7. records an aggregate workflow result.

It performs no Gmail or LinkedIn provider calls, owns no browser, and does not loop or sleep.

## 9. Task routing

Add Task types:

- `drip_linkedin`
- `drip_gmail`

Minimal payload:

```json
{
  "delivery_id": 123,
  "operator": "Arian"
}
```

### 9.1 LinkedIn routing

`drip_linkedin` must be:

- included in sender-owned LinkedIn Task scope;
- claimed only by the matching sender daemon;
- added to payload validation, handler dispatch, wake-time calculation, and stale-Task handling;
- explicitly exempt from the daemon path that expects `payload.campaign_id`;
- lower priority than current connection and current post-connection follow-up work;
- counted with current LinkedIn follow-ups against the existing account-wide follow-up action limit, without adding a new limit or environment setting.

The daemon continues to require an active traditional Campaign and the existing sender ICP configuration. Drip does not introduce a drip-only daemon mode.

### 9.2 Gmail routing

`drip_gmail` must be:

- excluded from every LinkedIn/browser claim set;
- claimed by the independently supervised Gmail worker;
- resolved through the existing operator-to-Gmail-account and Send-As alias mapping;
- dispatched explicitly to the current or drip Gmail handler.

Reuse the current alias model. Do not introduce another sender mapping system. Resolve aliases first, then run one worker for each actual Gmail account; that worker claims current and drip Gmail Tasks for every operator mapped to that account.

The worker’s claim must atomically move a matching pending Task to running. During cutover, the old in-daemon Gmail worker and the independent worker may not run together.

## 10. Gmail implementation

### 10.1 Correct current Gmail threading first

The current Gmail sequence must become a real thread before drip can inherit it.

Required behavior:

1. The first current-sequence email opens one Gmail thread.
2. The send result returns the raw Gmail message ID, raw Gmail thread ID, and RFC Message-ID separately.
3. Later current-sequence steps send with that Gmail `threadId` and the correct `In-Reply-To` and `References` headers.
4. The original subject is retained for thread continuation.
5. `crm.Message.thread_external_id` stores the mailbox-scoped `<account_key>:<raw_thread_id>`, never the message ID; the raw thread ID remains in the owned thread binding for Gmail API calls.
6. The first email establishes the subject; every continuation retains it even when later template steps exist.
7. Provider/account identity is retained so shared mailbox aliases resolve consistently.
8. Current sequence completion and thread evidence become available to the drip handoff service.

For an existing Lead, handoff may inherit only a thread that can be resolved to the exact current sender/account and Lead. If no current Gmail message ever sent, handoff records `not_applicable` and the first drip Gmail delivery opens a new thread. If existing history is ambiguous, the Gmail lane pauses for review rather than guessing or opening a competing thread.

### 10.2 Remove handler-local reply ingestion

The current broad handler-local Gmail search is no longer part of the send path. Both current and drip Gmail handlers rely on the existing hourly CRM v2 context workflow to persist replies and then call the shared database stop policy.

### 10.3 `drip_gmail` handler

The handler receives an already-claimed Task and:

1. loads the delivery and validates lane ownership/state;
2. checks the shared persisted-state stop policy;
3. verifies the same-channel predecessor and timing;
4. reserves one attempt in a short transaction;
5. rechecks stopped/ownership state immediately before submission;
6. opens or continues the lane’s exact Gmail thread;
7. persists the real send result to `crm.Message`, delivery, and attempt;
8. advances the lane or completes its current theme rendition.

Gmail failure affects only the Gmail lane. It never changes LinkedIn state or `Deal.state`.

## 11. LinkedIn implementation

### 11.1 Existing ingestion remains authoritative

V1 is limited to Leads covered by the existing Deal-backed LinkedIn flow. The existing realtime listener and scheduled `backfill_messages` remain the LinkedIn reply-ingestion mechanisms.

The implementation does not add:

- a new member-URN identity subsystem;
- a drip-specific inbox recovery service;
- a send-time conversation refresh;
- a second LinkedIn browser.

### 11.2 `drip_linkedin` handler

The handler receives an already-claimed Task and:

1. loads the delivery and validates the exact sender/lane ownership;
2. checks the shared persisted-state stop policy;
3. confirms the existing Deal/connection state permits LinkedIn messaging;
4. verifies the same-channel predecessor and timing;
5. reserves one attempt in a short transaction;
6. passes an `on_submit_attempt` callback to the chosen UI action; immediately before the click, that callback rechecks stopped/ownership state and commits `submission_attempted_at`;
7. sends through one chosen existing direct-message UI route returning `sent`, `pre_submit_failed`, or `unclear`;
8. records success in `crm.Message`, delivery, and attempt without changing `Deal.state`.

Do not use the current popup -> direct -> API fallback chain for drip. Once a send click may have occurred, a timeout or missing confirmation pauses the LinkedIn delivery as `unclear`. It is not sent again automatically through another route.

On restart, a stale Task whose attempt has `submission_attempted_at` but no confirmed success becomes `unclear`; it never returns to pending. A failure proven to occur before that callback may be retried normally. This is local duplicate prevention, not a new conversation-recovery system.

If the Lead is not connected, return the lane to `waiting_connection`. Gmail continues independently.

## 12. Shared-code changes and isolation boundary

Most implementation belongs under `drip/`. Shared changes are limited to:

- register `drip` in Django settings and Admin;
- add the two Task types and their payload validation/claim routing;
- add LinkedIn daemon dispatch for `drip_linkedin`;
- run the existing Gmail worker independently and dispatch `drip_gmail`;
- correct current Gmail threading and remove its handler-local reply search;
- add the shared Lead-level stop service and narrow current enqueue/handler guards;
- invoke the drip stop hook from existing Gmail and LinkedIn inbound persistence;
- add current-flow ownership guards after a channel hands off.

Drip must not:

- update current Campaign definitions;
- change the connection-request state machine;
- advance, complete, or fail a Deal;
- reuse current follow-up handlers;
- create new Google Sheets tabs;
- change CRM v2 Actions or the canonical `generate_followups` workflow;
- change manual replies, feed engagement, discovery, enrichment behavior except that known-stop guards prevent enrichment from creating new automated Gmail work.

## 13. Admin and operating controls

Django Admin provides:

- campaign/version inspection and publication state;
- enrollment and independent lane status;
- handoff evidence and timestamps;
- rendered deliveries and attempts;
- pause, resume, and stop actions;
- clear display of `unclear` deliveries requiring human review.

Published versions and delivery attempts are read-only audit records. Runtime campaign status and explicit management commands replace feature flags.

Operational prerequisites:

- the existing hourly CRM v2 context workflow remains scheduled and healthy;
- the existing LinkedIn listener/backfill operation remains as currently deployed;
- an active traditional Campaign keeps each participating LinkedIn daemon running;
- the independently supervised Gmail worker is running for the existing resolved accounts.

## 14. Implementation phases

Each phase is independently testable and leaves current outbound working.

### Phase 0 — Shared prerequisites

Implement only the shared seams required before drip can send:

1. Lead-level persisted-state stop service with a Deal wrapper for current callers.
2. Known-stop guards in current enqueue, healing, and handler paths.
3. After-commit inbound hook that stops active drip state when drip models exist.
4. Correct current Gmail thread IDs, reply headers, and sequence continuation.
5. Remove current Gmail handler-local reply ingestion in favor of hourly context sync.
6. Make Gmail Task claiming atomic and run one independent worker per resolved Gmail account using existing alias resolution.

Exit criteria:

- current LinkedIn/Gmail tests remain green;
- an hourly-ingested Gmail reply blocks later current automation through the database stop policy;
- current Gmail steps share one real Gmail thread;
- Gmail current Tasks run while the LinkedIn browser process is stopped;
- no new provider polling or feature flags exist.

### Phase 1 — Drip domain and publication

Implement models, migrations, Admin, manifest validation, immutable publication, and enrollment planning.

No drip Tasks or provider sends exist in this phase.

Exit criteria:

- invalid ICP/sender/copy/timing structures fail closed;
- published versions are immutable;
- one nonterminal enrollment per Lead and one lane per channel are enforced;
- plans explain handoff eligibility and refusal reasons without mutation.

### Phase 2 — Handoff, timing, and dry-run reconciliation

Implement independent channel ownership, handoff evidence, timing services, stop integration, and `reconcile_drips` in dry-run mode.

Exit criteria:

- LinkedIn and Gmail hand off independently with the same sender/account;
- Day 1 anchors correctly for new and historical enrollments;
- current and drip cannot own one Lead/channel simultaneously;
- late completion starts the next theme rather than using an expired campaign date;
- no Task is created in dry-run mode.

### Phase 3 — Gmail drip

Add `drip_gmail` materialization, routing, handler, threading, persistence, and tests.

Exit criteria:

- Gmail can advance while LinkedIn is unconnected or unavailable;
- current Gmail and drip never overlap;
- drip continues the exact current Gmail thread or opens one reviewed new thread;
- a persisted reply stops both lanes;
- Gmail never changes `Deal.state`.

### Phase 4 — LinkedIn drip

Add `drip_linkedin` materialization, sender-scoped daemon routing, one-route UI sending, and unclear-outcome handling.

Exit criteria:

- only the matching sender daemon claims the Task;
- no `campaign_id` is required in drip payload;
- unconnected Leads wait without blocking Gmail;
- no conversation refresh occurs before sending;
- a possibly successful click is never followed by an automatic alternate send;
- LinkedIn never changes `Deal.state`.

### Phase 5 — Controlled pilot

Publish one reviewed campaign version and manually enroll a small Deal-backed cohort.

Create that cohort through explicit `--lead-id` planning and the resulting reviewed plan artifact; do not use an unbounded ICP-wide apply.

Exercise:

- independent handoff dates;
- Gmail-first progress while LinkedIn waits;
- current-thread Gmail continuation;
- LinkedIn connection acceptance after Gmail has advanced;
- Gmail and LinkedIn replies arriving through existing ingestion;
- process restart before send and unclear outcome after LinkedIn click;
- pause, resume, and human takeover.

Only after the pilot passes should the daily reconciler be scheduled broadly.

## 15. Required tests

### Manifest and domain

- schema, canonical ICP, canonical sender, placeholder, and timing validation;
- immutable version snapshots and deterministic rendering;
- enrollment/lane uniqueness and state transitions;
- one executable Task per delivery.

### Handoff and timing

- same sender/account preserved across current and drip;
- current LinkedIn and drip LinkedIn cannot overlap;
- current Gmail and drip Gmail cannot overlap;
- current sequence completion is proven by persisted final-step evidence rather than Task absence/status alone;
- independent lane handoffs;
- historical enrollment anchors at activation;
- same-theme delay uses previous successful same-channel `sent_at`;
- next theme gets a fresh start only after previous completion.

### Stop behavior

- inbound Gmail in `crm.Message` blocks both lanes;
- inbound LinkedIn in `crm.Message` blocks both lanes;
- persisted Meeting and suppression/disqualification block automation;
- pending current and drip automated Tasks are retired;
- a claimed handler rechecks stop state before provider submission;
- manual replies remain allowed.

### Gmail

- first send opens one thread;
- later current and drip sends reuse the actual Gmail thread ID;
- provider message ID and thread ID are never confused;
- raw Gmail IDs, mailbox-scoped CRM IDs, and RFC Message-ID remain distinct;
- later Gmail steps retain the first message’s subject;
- hourly-ingested reply stops later sends without a handler-local Gmail query;
- `drip_gmail` is never claimed by a browser daemon;
- Gmail runs while LinkedIn is stopped.

### LinkedIn

- `drip_linkedin` is sender scoped and routes without a current campaign payload ID;
- current connection/follow-up Tasks remain ahead of drip;
- unconnected Lead waits;
- no send-time conversation fetch occurs;
- one UI route and the immediate pre-click callback are used;
- a pre-click failure may retry safely;
- a post-click ambiguous result pauses and never auto-resends;
- a stale Task with `submission_attempted_at` becomes unclear rather than pending;
- successful send persists `crm.Message` and leaves Deal unchanged.

### Regression

Run the complete existing connection, sweep, current LinkedIn follow-up, current Gmail, listener/backfill, suppression, manual reply, feed, discovery, CRM, and Admin suites.

## 16. Rollback

Rollback is database-controlled:

1. pause active drip campaigns;
2. stop the daily reconciler schedule;
3. retire pending `drip_linkedin` and `drip_gmail` Tasks;
4. leave sent deliveries and attempts as audit history;
5. leave stopped ownership and reply evidence intact so current automation cannot restart;
6. leave current `Deal.state` untouched.

No environment-flag rollback path is added.

## 17. Explicit non-goals

This implementation does not include:

- a separate repository;
- separate unrelated LinkedIn and Gmail campaign JSON files;
- a new Google Sheets campaign tab;
- a new LinkedIn browser or drip-only daemon mode;
- daemon startup without an active traditional Campaign;
- a new Gmail reply poller;
- a Gmail provider query before every send;
- a LinkedIn conversation refresh before every send;
- a new LinkedIn member-URN identity project;
- a new drip-specific LinkedIn inbox recovery system;
- new mailbox, sender, or drip pacing/limit infrastructure;
- cross-channel predecessor requirements;
- automatic retry after a possibly successful LinkedIn click;
- drip-driven `Deal.state` transitions;
- changes to CRM v2 Actions, `generate_followups`, or sender-specific ICP Messages Sheets.

## 18. Definition of done

The feature is complete when:

1. a reviewed versioned campaign maps ICPs and themes to sender-specific LinkedIn and Gmail copy;
2. current outreach hands each channel to drip independently without overlap and with the same sender/account;
3. each lane starts Day 1 from its own handoff and advances using only same-channel timing;
4. Gmail continues when LinkedIn is unconnected or failed;
5. Gmail current and drip messages continue one correct thread;
6. existing hourly Gmail ingestion and LinkedIn listener/backfill persist replies that stop all remaining automation;
7. LinkedIn drip uses the existing matching daemon with no send-time refresh and no alternate send after an unclear click;
8. current connection and post-connection behavior remains unchanged outside the narrow handoff/stop/threading guards;
9. all required tests pass; and
10. a controlled real cohort completes without duplicate, cross-owned, or post-reply sends beyond the explicitly accepted ingestion window.
