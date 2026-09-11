# Connection-first omnichannel v1

Status: sections 3–6 and sender restart control implemented and verified in
isolated no-send QA. The user subsequently authorized pushing the finished code
and testing sender restart flags. Feature-branch publication is the current
release step; main/live rollout is held for one-time supervisor bootstrap
coordination. Section 7's new omnichannel campaign pilot has not started.
Updated: 2026-09-10.

Implementation branch: `codex/connection-first-omnichannel-v1`, created from
`main` at `b7ea7952`. The subsequent push authorization supersedes the original
uncommitted-work gate for this release. Preserve the tested drip-role work.
Publishing the feature branch does not deploy it to main-tracking machines.
Before the requested live flag test, install the schema and code and verify a
full supervisor restart on both selected machines; Git's child-only restarts
cannot load the new checker into an already running supervisor. Leave flags
false until that bootstrap is verified. Do not create or activate a new
omnichannel campaign as part of the restart-control test.

## 1. Goal and scope

Ship a small, opt-in change that starts the existing Gmail sequence after a
confirmed LinkedIn connection request, without waiting for acceptance. Keep the
existing LinkedIn post-acceptance follow-ups. Preserve the existing post-acceptance
email option for other campaigns. Give independent drip campaigns the same
`{role}` wording support as current messages.

This plan records implementation work, not permission to activate campaigns,
send messages, change live lead records, migrate the shared database, or restart
remote workers. Those actions need a separately approved rollout. Do not migrate
the existing Arian/Eddy pilots as part of implementing v1.

Suggested implementation goal:

> Complete sections 3–6 of this plan: preserve the verified drip role support,
> implement invitation-triggered Gmail as a new-campaign opt-in, prove timing,
> idempotency, enrichment, suppression and frozen-message behavior in isolated
> no-send QA, and deliver a reviewed rollout/rollback package. Leave existing
> campaigns unchanged and stop before live activation or sending. Work on
> `codex/connection-first-omnichannel-v1` and leave all changes uncommitted for
> user review; do not commit, push or merge without subsequent approval.

Section 7 is the separately authorized live pilot. Section 8 is a deferred
roadmap, not part of the v1 completion criteria.

## 2. Agreed behavior

| Event | LinkedIn | Gmail |
| --- | --- | --- |
| Invitation confirmed sent | Keep the normal pending invitation | Start the configured email clock; queue missing-email lookup if eligible |
| Still not accepted when email is due | No LinkedIn follow-up | Send email 1 if all current send checks pass |
| Accepts before or after email 1 | Start the existing post-acceptance LinkedIn sequence | Continue the same email sequence; no second enrollment, restart, or new clock |
| Human reply on either channel is recorded | Stop remaining automated outreach under the existing shared stop policy | Same stop policy |
| No reply | Continue normal LinkedIn timing only if connected | Email 2 follows the successful email 1 send, not the invitation or acceptance date |
| Invitation failed, was not sent, or has an unclear outcome | Preserve existing failure/review behavior | Do not start invitation-triggered email from that event |

Acceptance is neither a requirement for this email sequence nor a reason to stop
it. “Pre-acceptance email” describes its start condition, not a cutoff date.

Campaign setup remains simple:

- Existing campaigns retain post-acceptance email, as today.
- Every new campaign's creation workflow must explicitly ask the user:
  **“Should this campaign's email sequence start before or after LinkedIn acceptance?”**
  - **Pre-acceptance:** start the email clock after the connection request is
    confirmed sent, using the reviewed delay without waiting for acceptance.
  - **Post-acceptance:** start the email clock only after acceptance.
- Record the user's choice before creating the campaign. Do not silently select
  an option, reuse another campaign's preference, or treat a default as consent.
- Keep email 1 and email 2 populated for the desired sequence. Leaving them blank
  disables that email copy; it does not move it to a pre-acceptance lane.
- A campaign chooses one start mode for the existing email sequence. V1 does not
  create both a pre-acceptance and a second post-acceptance email sequence.
- Keep messages standalone. Email must not assume they accepted or read a
  LinkedIn follow-up. No unrequested rewriting or Sheet imports.
- Explicit ICP assignments still choose the copy. `{role}` only changes wording;
  it does not classify a lead or choose an audience.

## 3. Drip role wording — completed

- [x] Accept `{role}` in independent drip LinkedIn bodies and Gmail subjects/bodies.
- [x] Reuse `linkedin/message_roles.py` and the existing `Lead.role_tag`.
- [x] Render `CFO/Finance` as `finance leaders`, `Founder/CEO` as `founders`, and
  the other canonical tags using the same shared lookup.
- [x] Use `companies` for a missing/blank tag. Reject an unknown nonblank tag when
  the selected copy references `{role}`. Leave role-free copy unaffected.
- [x] Freeze rendered delivery text before Task creation. Polls and retries must
  not rewrite it after a lead-role edit.
- [x] Add no role fields, role inference, audience routing, or lead mutations.
- [x] Verify with 42 new regression cases and the full isolated QA suite:
  1,245 tests passed and 756 imported-copy previews rendered.

Files: `drip/manifest.py`, `drip/services/reconciliation.py`,
`tests/drip/test_role_wording.py`. Documentation is updated in `AGENTS.md`,
`ARCHITECTURE.md`, and `drip/campaigns/README.md`.

Local evidence: `artifacts/qa/campaigns/20260910T224334878896Z/README.md` and
`run.json`. This passed without live accounts, provider access, or the shared DB.
The code is currently a working-tree change; passing tests does not mean it has
been committed, deployed, or exercised by a live drip campaign. Preserve it while
implementing the rest of this plan.

## 4. Small email-trigger implementation

### 4a. Persist an explicit campaign choice

- [x] Add one narrowly scoped, validated campaign start-mode setting. Proposed
  values: `post_acceptance` and `invitation_sent`. Preserve `post_acceptance` for
  existing records; this migration default is not a choice for new campaigns.
- [x] Make the campaign-creation workflow ask the pre-/post-acceptance question
  above and persist the user's explicit answer. Pause creation until answered;
  do not infer it from copy, past preferences, or a preselected option.
- [x] Require the reviewed start mode in noninteractive new-campaign creation
  and imports. Missing/invalid values must fail before creating campaign records,
  enrolling leads or scheduling work. Show the choice in the creation preview.
- [x] Preserve all existing campaigns, schedules and frozen deliveries during
  migration. Do not infer opt-in from nonblank email copy or an environment flag.
- [x] Support explicit configuration and faithful export/import. Enabling the new
  mode must require exact sender ownership, not the first active profile selected
  by the current campaign-import creation path.
- [x] Keep initial v1 opt-in limited to newly created, version-bound campaigns.
  Reject mode changes after enrollment/outreach starts; no historical backfill
  or silent fallback between start modes.

Use the existing enrollment/delivery identities. Do not add a general orchestration
engine, new ICP fields, role snapshots, or per-lead strategy metadata.

### 4b. Trigger once from confirmed invitation evidence

- [x] Call the canonical Gmail scheduler after a successful invitation has been
  durably recorded with the exact Deal, `invitation_sent_at`, and canonical
  `invitation_sender`. Make committed evidence the prerequisite for scheduling.
- [x] Do not trigger from a connect attempt, a button click alone, a failed or
  unclear delivery, or merely finding an already-pending invitation on a profile.
- [x] Use the confirmed invitation timestamp as the first-email anchor in the
  new mode. Never substitute `now()` on a retry or an unrelated acceptance date.
- [x] Preserve the existing post-acceptance anchor for default-mode campaigns.
  A new-mode lead already connected without a qualifying invitation is an
  explicit skip/review case, not fabricated invitation evidence.
- [x] Make acceptance, repeated sweeps, repeated scheduler calls, and concurrent
  recovery reuse the same frozen Gmail delivery and Task identity. Acceptance
  must never reset the sequence or pull a due date forward.
- [x] Add bounded, idempotent missing-work recovery for a crash after invitation
  persistence but before Gmail scheduling. Recover only opted-in eligible work;
  rotate past held rows so a full page of skips cannot starve later invitations.
  Record the exact sender/campaign scope, scan cursor and outcome counts in the
  existing `WorkflowRun` audit table, not new per-lead strategy fields;
  never recreate an uncertain send or overwrite an existing schedule.

Primary touchpoints: `linkedin/models.py` and a narrow migration;
`linkedin/management/commands/import_campaign.py` / `export_campaign.py`;
`linkedin/tasks/connect.py`; `gmail/handoff.py`; acceptance/recovery callers;
`linkedin/message_delivery.py` and its runtime validation only as needed.

### 4c. Timing and final send checks

- [x] First email due time is the selected start anchor plus its reviewed delay,
  respecting the chosen sender window. Later emails retain the current
  successful-previous-email anchor so downtime cannot compress the sequence.
- [x] Preserve Task and frozen-delivery due dates through restart, retry,
  acceptance and quota/window deferral. Recheck both immediately before sending;
  an early claim must defer the same identity, not send early or rerender it.
- [x] Continue exact sender/account, campaign-active, stop, suppression, frozen
  enrollment and drip-ownership checks at enqueue and final submission.
- [x] Confirm existing Gmail submission markers and dedupe prevent repeat sends.
  An ambiguous provider outcome stays held for reconciliation, never auto-resent.

The current imported first-email delay is approximately 20 minutes (`0.33h`),
not an approved invitation-to-email delay. Proposed pilot delay: 24 hours,
subject to user review with the actual frozen copy. Do not silently alter shared
JSON timing or the live pilots. Review the later-step delay at the same time.

The Gmail worker is independently running and claims due work; do not assume the
LinkedIn daemon's active-hours sleep provides a Gmail send window or rate limit.
Prove the new lane's final timing guard and use a bounded, staggered pilot.
Broad Gmail throughput/pacing changes are a separate scaling decision.

### 4d. Email enrichment and shared stops

- [x] Use existing suitable email addresses; otherwise queue the existing lookup
  once after the invitation, early enough to finish before the email due time.
  No prerequisite to mass-enrich the entire Marketplace lead list.
- [x] Keep lookup timing separate from send timing. Finding an address early must
  not make the frozen email immediately due. A late result queues the same
  unsent email through normal scheduling rather than restarting the sequence.
- [x] Test and document which worker consumes `enrich_email`; do not assume the
  Gmail sender worker consumes enrichment Tasks. Verify actual deployed pickup
  separately during the section 7 live pilot.
- [x] Atomically claim due enrichment work across machines. Email claims and
  stale recovery must be exact-sender scoped; preserve the shared legacy phone
  queue and never reclaim fresh work. Retain Task/provider request identities.
- [x] Missing/unusable addresses or exhausted lookup attempts hold/skip only the
  email lane with a visible reason. LinkedIn can continue. Lookup success is not
  proof of deliverability; retain existing suppression checks and document the
  pilot's address-review and bounce-handling procedure rather than assuming a
  deliverability-validation gate already exists.
- [x] Recheck replies, meetings, disqualification and suppression before lookup,
  after lookup, before enqueue, and at final send. A response on either channel
  must stop both automated lanes under the existing shared policy.
- [x] Preserve the current reply-ingestion model. Gmail replies are known after
  ingestion, LinkedIn replies after listener/backfill persistence; this is not
  an instant provider-wide stop guarantee. Verify ingestion freshness for a pilot.
- [x] Log distinct reasons for missing email work: flag/config disabled, no copy,
  no mapping, no qualifying invitation, stopped lead, lookup failure, drip-owned,
  or uncertain send. Do not silently label an absent Task as healthy.

Primary touchpoints: `gmail/handoff.py`, `gmail/tasks/enrich_email.py`,
`gmail/tasks/follow_up.py`, `gmail/submission.py`, `gmail/worker.py`, the existing
enrichment worker, and shared stop checks.

## 5. No-send QA acceptance matrix

- [x] Extend the isolated harness to include all modified paths, especially
  `tests/gmail/test_handoff.py`, `tests/gmail/test_worker.py`, invitation hooks,
  enrichment, submission recovery and any new migration tests. Do not assume
  the existing full-suite file list already includes every relevant test.
- [x] Test both Arian and Chuka/Eddy mailbox identities, not just one sender.
- [x] Run every applicable imported audience through actual version-bound
  rendering; verify subject/body, role fallback, sender signature and no leftover
  placeholders. Preserve manual audience assignment, not automatic routing.
- [x] Use simulated time to exercise multi-day sequences without waiting days.
  Simulate failure/restart/race cases against the real scheduling and persistence
  code with provider responses stubbed. Do not manufacture ambiguous live sends
  by crashing real workers or change existing campaign dates to force tests.

| Case | Required result |
| --- | --- |
| Existing/default campaign | Behavior, schedule and frozen copy unchanged |
| New-campaign creation | Explicit question asked; selected mode shown in preview and persisted exactly |
| New-campaign creation/import without an explicit valid choice | No silent default; no campaign, enrollment or Task created |
| Confirmed new invitation, known email | Exactly one first Gmail delivery, anchored to the invitation |
| Missing email | One lookup; original send clock survives early/late enrichment |
| Attempt failed, unclear, or only already-pending observed | No new invitation-triggered email |
| Accepts before email 1, between emails, or after sequence completion | No restart, duplicate, re-anchor or pulled-forward email |
| Repeated hooks, simultaneous sweep/recovery, or restart after receipt | Same delivery identity; missing work recovered once |
| Future Task/delivery claimed early or after downtime | No early send or compressed successor timing |
| Reply on either channel, meeting, suppression or disqualification | Both automated lanes stop; final-boundary race checked |
| No address, wrong sender, no copy, inactive campaign or drip ownership | Explicit safe hold/skip; no accidental send |
| Confirmed send followed by DB/process failure | Bookkeeping-only recovery, never a second provider send |
| Provider submission outcome unknown | Hold for reconciliation, no automatic retry |
| Role changes after freeze | Existing delivery unchanged; newly rendered copy uses the shared lookup |

Run `.venv/bin/python scripts/qa_campaigns.py` using its private socket-only
PostgreSQL cluster and blocked external delivery. Keep machine-readable results,
render previews and the safety receipt. Do not run tests against the shared DB,
live Gmail or LinkedIn sessions. Re-run after the final change, not just the role
patch. Test migration defaults and confirm no pending model migrations are missing.
The 1,245-test role-support run is a historical baseline. The final combined
implementation evidence is recorded in section 6a below. Offline tests do not prove live authentication, browser
selectors, remote worker configuration, provider delivery or inbox placement.

## 6. Implementation completion and handoff

- [x] Sections 4–5 are complete with passing evidence, or explicitly marked
  blocked with a concrete reason; unfinished work is not reported as shipped.
- [x] Update `AGENTS.md`, `ARCHITECTURE.md`, and `docs/gmail-sequence-plan.md` to
  describe the actual implemented options. The older spec is not evidence that
  invitation-triggered email already exists.
- [x] Provide the config schema, tested revision, migration procedure, QA report,
  fail-closed pilot template, preview workflow and rollback procedure. Present the uncommitted feature-
  branch diff for user review, identifying the base revision and tested working-
  tree changes. Do not commit, push or merge until separately approved after review.
- [x] Rollback stops the exact new pilot and prevents outstanding new-mode Gmail
  Tasks from sending, without deleting history, unfreezing copy, reverting live
  data, or changing unrelated campaigns. Do not rely only on reverting the mode.
- [x] Stop before shared-DB migration, publication/enrollment, remote restarts or
  sending. Ask for the remaining rollout choices and approval together.

## 6a. Initial implementation evidence and review boundary

Verified on 2026-09-10 Toronto / 2026-09-11 UTC, before the sender-restart add-on.
For the latest combined source fingerprint and QA, see section 6b. Initial run:
[QA report](../artifacts/qa/campaigns/20260911T002714729138Z/README.md),
[machine-readable receipt](../artifacts/qa/campaigns/20260911T002714729138Z/run.json),
[case results](../artifacts/qa/campaigns/20260911T002714729138Z/case_results.csv),
[exact message previews](../artifacts/qa/campaigns/20260911T002714729138Z/message_previews.csv).

- **1,529 passed, zero failures/collection errors; 1,148 previews.**
- Private socket-only PostgreSQL 17; blocked external providers; no live accounts,
  browser sessions, sends, or shared-DB access. The disposable cluster was removed.
- Both imported JSON stores stayed unchanged. All 37 audiences × Arian and
  Chuka/Eddy have exact subject/body and unresolved-placeholder checks; actual
  handler paths exercise default post-acceptance and new invitation-start flows.
- Simulated time covers acceptance before email 1, between emails and after
  Gmail completion, as well as replies, early/late lookup, missing addresses,
  restarts, partial saves, concurrent claims/materialization and uncertain sends.
- Migration rehearsal preserves existing Campaign/Deal/enrollment/Task/delivery
  snapshots and leaves existing campaigns in post-acceptance mode.
- Tested base: `b7ea79525edc2a61ba4687932565bca7199cda07`. Changes were
  uncommitted on `codex/connection-first-omnichannel-v1` at this QA run.
- Tested code-source SHA-256:
  `15bb4321b70e192a03af16d2e056f5498408b7529788541947028a108659855f`
  (570 source/config files; checked before/after and independently after the run).
  This fingerprints code and tested inputs, not every documentation file.

| Requirement | Main regression evidence |
| --- | --- |
| Explicit choice, exact owner, safe preview and partial saves | `tests/test_campaign_email_mode.py`, `tests/management/test_campaign_email_mode.py` |
| Existing-data migration preservation | `tests/migrations/test_campaign_gmail_start_mode.py` |
| Confirmed invitation, actual acceptance, all imported copy | `tests/campaign_qa/test_imported_campaigns.py`, `tests/gmail/test_invitation_start.py` |
| Both due dates, final stop boundary, uncertain-send holds | `tests/gmail/test_current_timing.py`, `tests/gmail/test_versioned_delivery.py` |
| Early/late/failed lookup, later reviewed address | `tests/gmail/test_enrichment_boundaries.py`, `tests/gmail/test_invitation_start.py` |
| Fair bounded recovery and real SQL concurrency | `tests/gmail/test_invitation_recovery_cursor.py`, `tests/test_message_delivery_locks.py`, `tests/enrichment/test_worker.py` |
| Independent-drip role wording and frozen copy | `tests/drip/test_role_wording.py`, existing drip reconciliation tests |

The [rollout packet](connection-first-omnichannel-v1-rollout.md) includes a
deliberately invalid [pilot template](examples/connection-first-pilot.template.json).
It is not an approved recipient manifest or live campaign. Review sender,
pre/post choice, exact recipients, copy, delays, active hours and cap together
before authorizing deployment, shared migration, enrollment, restart or sending.

At the initial QA handoff, no shared migration, publication/import, campaign activation, sender restart,
live lookup or send was performed. Existing live campaigns 44/45 were not
modified by this work. No commit, push or merge was made. There are no remaining
implementation blockers in sections 3–6; section 7 remains entirely unverified
and approval-gated.

Known limits are explicit: offline QA does not establish auth, remote pickup,
fresh reply ingestion, browser selectors, actual delivery or inbox placement.
Atomic enrichment claims do not provide exactly-once provider billing through
a crash between lookup submission and persisting its request ID. Stale recovery
assumes age-qualified work is no longer running; review deployed thresholds
against provider timeouts. No universal drip-orchestration migration, automatic
bounce handling, mass enrichment or broader throughput controls were added.

## 6b. User-requested sender restart control add-on

The subsequent request to remotely restart a sender's workers is implemented on
the same feature branch. `LinkedInProfile.restart_requested` defaults
false (migration 0032). Its sender-scoped supervisor consumer, preview-first
request command and Admin checkbox are described in
[sender restart operations](sender-supervisor-restart.md).

Latest combined isolated QA:
[report](../artifacts/qa/campaigns/20260911T014208799188Z/README.md) and
[receipt](../artifacts/qa/campaigns/20260911T014208799188Z/run.json).
**1,603 tests passed; 1,148 message previews; zero failures/collection errors.**
This includes the original omnichannel implementation and restart-control tests:
exact sender scope, migration preservation, flag-only updates, real SQL races,
requests arriving during restart, cancellation, failed child starts, DB/table-lock
outages, combined code/flag changes and a control timer unaffected by child crashes.

Latest code-source SHA-256:
`6af3c548d87c5abcf41a1679efdf7308e191b7ec95f66116517fabc81b631290`
(577 source/config files, unchanged during the run and independently rechecked).
Both imported JSON stores stayed unchanged. Private PostgreSQL was stopped and
removed; no live DB access, provider calls, flags, migrations or restarts were used.

The add-on is not deployed. The user subsequently authorized pushing the code
and testing the flags after rollout. The release review found no blocker and
the source fingerprint still matches the passing combined QA. Main/live rollout
is held for coordination of the schema installation and full supervisor bootstrap
on selected machines; an older running supervisor cannot load the new polling
behavior by restarting only its children. No live flag has been set. The new
omnichannel campaign pilot below remains separately approval-gated and unverified.

## 7. Separately authorized live pilot

Use a small cohort selected to cover behavior, not one live recipient per ICP.
All applicable ICP/role copy variants are exercised offline in section 5.

- [ ] Review sender, exact recipients, frozen copy, first-email delay, later
  delays, active hours and a small staggered send cap. Start with internal
  recipients; a production cohort requires its own explicit approval.
- [ ] Use consenting, not-yet-connected recipients to prove the invitation
  trigger. Already-connected internal accounts cannot prove that path. Do not
  clone the same person into multiple fake leads or remove/re-add connections
  merely to manufacture test evidence.
- [ ] Verify Gmail flag, account mapping, auth, worker/enrichment pickup, reply
  ingestion and the exact deployed code on each sender machine. Investigate the
  previously observed accepted-lead email handoff gaps before calling Gmail healthy.
- [ ] Apply the approved migration/config and launch only the intended campaign.
  Do not create competing local processes for a sender running remotely.
- [ ] Pre-acceptance case: confirm the invitation, observe email arriving while
  still pending, then verify acceptance starts LinkedIn follow-ups without
  restarting, duplicating or rescheduling the email sequence.
- [ ] Post-acceptance control: verify no email is sent before acceptance and the
  existing sequence starts normally afterward.
- [ ] Exercise one LinkedIn reply and one Gmail reply; verify each is correctly
  ingested and prevents remaining automation on both channels.
- [ ] Observe a no-reply sequence through at least one second email and second
  LinkedIn follow-up at their reviewed times. Check sender, thread, frozen copy
  and real recipient receipt, not just initial invitations.
- [ ] Verify real enrichment pickup where needed. Leave deterministic failed,
  missing and delayed lookup scenarios to isolated tests; lack of an address
  must remain a visible email hold, not a claimed successful live send.
- [ ] Monitor using the product's recurring mechanism only when requested. Keep
  read-only checks separate from authority to repair, resume or send.
- [ ] Inspect exact Task/delivery IDs, due dates, provider receipts, thread
  content and reply-stop evidence. A fresh heartbeat proves only process
  liveness; clean logs alone do not prove receipt, correct timing or no duplicates.
- [ ] Pause on duplicate identity, early send, wrong copy/sender, stale reply
  ingestion or ambiguous submission. Reconcile evidence before retrying.

No full-scale launch is implied by passing fixture tests or sending invitations.
Judge the pilot by both-channel behavior through follow-ups and a reply stop.
Normal cadence may require several days of observation. Shorter delays may be
used only in a separately reviewed internal-QA configuration; they prove the
sequence wiring, not the normal production cadence. Never shorten existing
campaign schedules for QA. Mark unobserved scenarios pending, not passed.

## 8. Deferred: broader independent-drip orchestration

The longer-term design discussed was to make connection requests an acquisition
step, with independent drip responsible for email from invitation time and
LinkedIn after acceptance. Optional post-acceptance email campaigns could then be
explicit follow-on campaigns. That is not the small v1 above.

Before committing to that migration, separately scope and test:

- Exact support for the current shared ICP audience keys. Drip currently validates
  legacy canonical buckets; completed `{role}` support does not fix that mismatch.
- Version-aware handoff from current campaigns rather than legacy template counts.
- Safe automatic enrollment/reconciliation triggers and campaign/channel ownership.
  Current drip ownership is not released merely by pausing/completing a lane.
- Removal of competing current follow-up scheduling only after replacement is proven.
- Email-first enrollment/enrichment where an address is initially missing.
- An explicit handoff or completion rule for any separate post-acceptance email
  campaign, avoiding concurrent owners and duplicate coverage. Today this is not
  a plug-and-play second campaign toggle.
- Cross-channel contact spacing, if wanted, as a separate behavior choice; current
  lanes have independent timers rather than a unified contact calendar.
- Wider migration, rollback and live QA across the real audience set.

Reference: [existing independent-drip architecture plan](drip-campaign-implementation-plan.md).
Do not rewrite that subsystem, enable a reconciler, publish drip manifests or
retire current campaigns to complete this v1 plan.
