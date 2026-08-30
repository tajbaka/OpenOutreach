# Architecture

Detailed module documentation for OpenOutreach. See `CLAUDE.md` for rules and quick reference.

## Entry Flow

`manage.py` (Django bootstrap + auto-migrate + CRM setup):
- Suppresses Pydantic serialization warning from langchain-openai. Configures logging: DEBUG level, suppresses noisy third-party loggers.
- No args → runs daemon: startup checks → `ensure_onboarding()` → validate `LLM_API_KEY` → `get_or_create_session(handle)` → set default campaign → `session.ensure_browser()` → `ensure_self_profile()` → GDPR newsletter override (marker-guarded) → `ensure_newsletter_subscription()` → `run_daemon(session)`.
- With `runserver` arg → auto-migrates, then delegates to Django CLI.
- Other args → delegates directly to `execute_from_command_line`.

### Startup Integrity Checks

Before the daemon does any work, `manage.py`'s no-args branch runs two
checks, both before `_ensure_db()`:

- `linkedin/version_check.py` — `check_for_updates()` runs `git fetch` and compares local `HEAD` to the current branch's upstream `@{u}`. When behind, a TTY session is prompted to pull and a headless run auto-pulls. A successful `git pull --ff-only` exits 0 because the process must restart on the newly pulled code; a failed pull logs loudly, posts `notify_error`, and exits 1. Non-git deployments are a silent no-op.
- `linkedin/env_check.py` — `check_env_vars()` logs one grouped summary of missing environment variables. Advisory only; never aborts startup.
- `linkedin/env_spec.py` — declared `EnvVar` registry consumed by `check_env_vars()`. This is the single source of truth for project-owned env vars, and `.env.example` is kept in the same order so drift is easy to spot.

## Onboarding (`onboarding.py`)

`ensure_onboarding()` ensures Campaign, active LinkedInProfile, LLM config, and legal acceptance exist. Four checks:

1. **Campaign** — interactive prompts for campaign name, product docs, objective, booking link. Creates `Campaign` owned by the onboarding operator.
2. **LinkedInProfile** — prompts for LinkedIn email, password, newsletter, rate limits. Handle from email slug.
3. **LLM config** — prompts for `LLM_API_KEY`, `AI_MODEL`, `LLM_API_BASE` → writes to `.env`.
4. **Legal notice** — per-account acceptance stored as `LinkedInProfile.legal_accepted`.

## Profile State Machine

`enums.py:ProfileState` (TextChoices) is the legacy per-Lead/per-Campaign outreach automation state machine: QUALIFIED, READY_TO_CONNECT, PENDING, CONNECTED, COMPLETED, FAILED. It is not the canonical sales pipeline. In particular, COMPLETED means an automation sequence finished and never means Closed Won. Pre-Deal states are url_only (no description) and enriched (has description). `Lead.disqualified=True` is permanent account-level exclusion. LLM rejections are FAILED Deals with "Disqualified" closing reason (campaign-scoped). Stale project-sent invitation withdrawals are operational FAILED Deals with closing reason "Failed"; they do not globally disqualify the Lead.

`crm/models/deal.py:ClosingReason` (TextChoices): COMPLETED, FAILED, DISQUALIFIED. Used by `Deal.closing_reason`.

## Task Queue

### Feed Engagement Lanes

High-signal feed alerts expose separate context, Like, and human-approved public-comment workflows through the existing `/api/slack_enrich` endpoint. Alert blocks show the post date first, the author in a header, the post body, analysis context, and one action row with `Open post`, `Post context`, `Like`, and `Comment on LinkedIn`; the endpoint acknowledges the URL-only Open interaction so Slack does not show an error. The shared endpoint merges each feed module's registries and delegates without branching on workflow details.

`api/slack_feed_context.py` owns the read-only Post context modal. It reads the complete saved `LinkedInFeedPost` plus observations through raw SQL, renders the full post first across multiple Slack-safe sections without truncation, then shows author, post link, saved analysis, collection timestamps, and per-sender sightings. Its `Generate AI summary` action refreshes only that modal and uses the existing Vercel `LLM_API_KEY`, `AI_MODEL`, and optional `LLM_API_BASE`; summaries are transient and do not create CRM state.

`api/slack_feed_like.py` owns a compact sender selector and creates an independent sender-scoped `feed_like` task. The main daemon claims it with the same ownership and off-hours rules as feed comments, then calls the existing `ensure_feed_post_liked` Playwright action on the exact post. Reaction-control discovery polls for delayed rendering and supports both LinkedIn's `Reaction button state` label and state-bearing `aria-pressed` variants while excluding comment-level Like controls. Live reaction state makes retries idempotent: `no reaction` is clicked and verified, an existing Like is a successful no-op, a different reaction is preserved, and ambiguous state fails closed. `linkedin/feed_like_slack_status.py` updates only the Like status block, so existing comment status remains intact.

`api/slack_feed_comment.py` owns comment action parsing, modal context, sender selection, AI drafting, raw-SQL task/ledger enqueue, and pending-only cancellation. Submit closes the modal, creates a sender-scoped `feed_comment` task plus `LinkedInFeedComment`, and edits the original alert in place with an inline queued status and `Cancel queued comment`. `api/slack_feed_common.py` provides compressed source-block transport and source-message update fallback for the independent Vercel feed modules. `linkedin/feed_slack_status.py` preserves those blocks and replaces only the single feed-comment status block. The matching sender daemon replaces the queued state with sent, failed, uncertain, or skipped and removes the cancel control. Status delivery prefers the interaction `response_url` and falls back to `chat.update` on the original channel/timestamp; it never creates a Slack thread reply. The daemon permits the task through off-hours sleeps, opens the exact collected post, and submits through Playwright UI only. After typing, the action searches six nearby comment-composer ancestors for explicit submit controls and text-only `Post`/`Comment` variants. The bounded search reaches LinkedIn's deeply nested current Comment submit while excluding post-level controls outside the composer. Submit verification then polls rendered LinkedIn comment items, including the signed-in renderer's `replaceableComment_urn:li:comment` component, for the draft text while explicitly excluding text that remains in the editable composer. Chuka is shown as Eddy in Slack but remains canonical in payloads. The ledger records queued/running/sent/failed/uncertain/skipped state and is stamped immediately before the submit click, so duplicate sent/uncertain attempts and recovered post-submit tasks fail closed. Long drafts still use `human_type`'s configured per-keystroke cadence; its Playwright timeout is derived from text length and the selected delay so normal human-speed comments are not cut off at 30 seconds. The ledger is read-only in Django Admin. `manage.py run_feed_comment_once --handle <django-username>` provides a bounded live-QA path: it verifies the authenticated LinkedIn identity, claims exactly one due matching `feed_comment`, and never claims other daemon task types. These workflows reuse the existing Slack app, endpoint, and Vercel DB/Slack/LLM env vars.

Queue, cancel, and daemon outcome states explicitly update the source alert through the interaction response URL, falling back to `chat.update`; failures are logged instead of silently creating a separate status message.

New feed-comment tasks receive a 60-second scheduling grace period, giving the operator a usable pending-only cancellation window before a sender daemon can claim the task.

Each approved feed-comment task also runs the isolated `linkedin/actions/feed_like.py` action before comment submission. It polls for the exact post's primary reaction control and reads either the live `Reaction button state` ARIA label or an explicit `aria-pressed` state: `no reaction` is clicked and polled until `Like`, an existing `Like` is a successful no-op, and any other reaction is preserved. Comment reaction controls are excluded; component UUIDs and CSS classes are never used for state. Expected Like failures and unverified clicks are logged and reflected in the eventual Slack sent status, but fail open so the human-approved comment still proceeds.

Persistent queue backed by `Task` model. Worker loop in `daemon.py`: `seconds_until_active()` guard pauses outside active hours/rest days → atomically claim the next sender-owned task by priority/fairness → set campaign on session → RUNNING → dispatch via `_HANDLERS` dict → COMPLETED/FAILED. Manual replies are first; a sweep overdue beyond `CONNECTION_SWEEP_MAX_QUEUE_DELAY_MINUTES` receives bounded fairness; status summaries are next; due follow-up/connect delivery precedes normal maintenance. Failures are captured by `failure_diagnostics()`. `heal_tasks()` reconciles on startup and recovers only this sender's running browser tasks older than `TASK_RUNNING_STALE_MINUTES`; it never globally resets another sender's task.

Low connect-pool notifications are post-action signals. A completed connect task must have written a matching sender/campaign `ActionLog.CONNECT` since that task started before `CONNECT_LOW_POOL_THRESHOLD` is evaluated. Empty tasks retained by older active campaigns therefore cannot emit restart-time low-pool alerts.

Task types (handlers in `linkedin/tasks/`, signature: `handle_*(task, session, qualifiers)`):

1. **`handle_connect`** — Unified via `ConnectStrategy` dataclass. Regular: `find_candidate()` from `pools.py`; freemium: `find_freemium_candidate()`. Unreachable detection after `MAX_CONNECT_ATTEMPTS` (3). The More-menu fallback recognizes native links/buttons and LinkedIn's ARIA-button `<div role="button">` variant; connect issue rows keep the requested lead ID as identity and store the browser's query-bearing URL only in metadata.
2. **`handle_sweep_connections`** — Account-wide but sender-scoped by `Task.payload.operator`. Each sender daemon claims only its own sweep row and visits that logged-in account's `mynetwork/invite-connect/connections/` once per `CONNECTION_SWEEP_INTERVAL_HOURS`. The default incremental path derives browser depth from the latest successful sender `WorkflowRun(name="connection-sweep")`, bootstrapping from the most recent legacy completed sweep and applying `CONNECTION_SWEEP_OVERLAP_HOURS`; the oldest PENDING invitation never controls recurring browser depth. The scanner extracts each rendered/virtualized batch in one browser-side call, accumulates by public ID, and stops at the connection-date cutoff, idle end, `CONNECTION_SWEEP_MAX_SECONDS`, or `CONNECTION_SWEEP_MAX_ROUNDS`. The hard budget covers acceptance processing as well (with at most one in-flight acceptance allowed to overrun). Complete runs advance the watermark; incomplete runs write `WorkflowRun(name="connection-sweep-incomplete")`, preserve the target cutoff, and retry after `CONNECTION_SWEEP_INCOMPLETE_RETRY_MINUTES`. Empty selectors are incomplete rather than a false watermark. After Playwright work the handler recycles the DB connection, cross-references all sender PENDING Deals by public ID, transitions matches to CONNECTED, computes the post-accept follow-up delay with `ActionLog.ActionType.FOLLOW_UP`, and enqueues `follow_up`. `ENABLE_INCREMENTAL_CONNECTION_SWEEP=false` restores the legacy cutoff while keeping hard runtime bounds; `ENABLE_SWEEP_CONNECTIONS=false` disables the lane. Plain accepted invites are not posted individually to Slack; accepted-and-replied leads still post. Sweep completion no longer posts send-count status; that lives in the hourly account-agnostic `status_summary` task.
3. **`handle_follow_up`** — Per-profile. Sends rigid ICP LinkedIn follow-up sequence steps from `icp_messages.json`, gated by `ENABLE_FOLLOW_UP` and the follow-up rate limit. Queued tasks freeze template routing in `payload.icp`; when that field is absent on legacy tasks, the handler uses `resolve_icp(lead)`, so stamped `Lead.icp` such as `CMMC Buyers` wins before the legacy classifier backfills blanks. Payloads may carry `sequence_name`, `channel`, and `step_index`; missing values default to `linkedin_connect_followup` / same channel / `0`. Owner scoping compares outbound `Message.sender` values through `linkedin.operators.resolve_operator`, so new LinkedIn display variants such as `"athena aghdami"` must be added there. Stop checks are DB-local only via `linkedin.tasks.stop_checks.automation_stop_reason`: inbound LinkedIn/Gmail message, existing `crm.Meeting`, disqualified lead, or suppression. On send failure it re-enqueues the same step in 24h. On non-final success it records `ActionLog`, persists an outbound `crm.Message`, and enqueues the next step after that step's `delay_hours`, calculated from `Deal.connected_at` and normalized into configured active hours/rest days, while keeping the Deal `CONNECTED`; final LinkedIn success marks the Deal `COMPLETED` but does not stop an already-queued Gmail lane. Step-level dedup for a non-final already-sent step keeps the Deal `CONNECTED` and ensures the next step is queued; only a final-step dedup marks `COMPLETED`. Post-send retries only retry the state write so a dead DB connection cannot double-count the action or duplicate the next-step Task. Gmail sequencing is scheduled from post-accept paths (`handle_connect`, `handle_sweep_connections`, and the no-reply backfill command) through `gmail.handoff.maybe_schedule_gmail_sequence`; it queues either `enrich_email` or `gmail_follow_up` when `ENABLE_GMAIL_SEQUENCE=true`, the operator has a Gmail mapping, and no local stop condition exists. LinkedIn and Gmail are fail-open lanes: one failed/skipped task does not block the other, Gmail scheduling exceptions are logged and swallowed at the LinkedIn boundary, and templates must be standalone rather than referencing a previous channel-specific send. ICP blocks can declare `"media": ["demo.gif"]`; templates in that block may reference `{demo.gif}` to attach a file resolved from `assets/follow_up/` or `assets/followup/`, while legacy `{add demo.gif}` still works. The ICP Messages Sheets sync renders multi-step copy as interleaved columns: `Followup Message N`, `Email Subject N`, `Email Body N`. Push flattens each step's first variant into its column and pull rebuilds LinkedIn plus Gmail JSON, preserving existing per-step `delay_hours` because Sheets carries copy, not cadence, and preserving JSON-only fields such as `media`. Editable ICP Messages tabs include `CSPs`, `3PAOs/Assessors`, `Advisors`, `Channel`, `Investor / Portfolio Ops`, `Accelerator / Ecosystem`, `CMMC Buyers`, and `CMMC Advisor/Channel`; the investor-channel values are rows in the existing Arian and Chuka tabs, not new tabs.
4. **`handle_manual_reply`** — Slack-to-LinkedIn reply lane. Slack modal submit inserts a `manual_reply` Task with `lead_id`, `operator`, `message`, Slack message coordinates, and original Slack blocks. The queued Slack status includes a cancel button backed by the Vercel endpoint; cancel deletes only a still-`pending` task, and reports if the daemon already started claiming/sending it. `Task.objects.claim_next()` atomically flips the selected task from `pending` to `running`, so a successfully cancelled reply cannot still be sent by a daemon that had only read the row. The daemon claims manual replies ahead of normal outbound work, scoped by `payload.operator`, and sends through the same logged-in Playwright page via `send_raw_message`. Manual replies use the direct-thread UI composer with human typing and deliberately disable the Voyager API fallback, so a UI send failure fails the task instead of sending instantly. Manual replies bypass active-hours sleeps when due; while no reply is currently due, the daemon caps sleep to `MANUAL_REPLY_POLL_SECONDS` (default 60) during both active and off-hours so newly queued replies are picked up quickly without running normal off-hours automation. Manual replies do not consume connect/follow-up quotas, do not advance sequences, and do not change Deal state; the durable outreach side effect is the outbound `crm.Message` with a `manual-reply:` synthetic external id. Before sending, the handler checks that same `crm.Message` ledger for an existing same lead/operator/body manual reply and skips duplicates, covering the crash-after-send/before-task-complete window. Slack sent/failed acknowledgements are best-effort via `chat.update` on the original notification, falling back to the interaction `response_url`.
5. **`handle_status_summary`** — Account-agnostic hourly ops summary. Any daemon may claim the `status_summary` task, post one Slack snapshot for every reportable expected sender, and enqueue the next run for one hour later. Each line item is per sender: invites sent today, LinkedIn follow-ups today, email follow-ups today, manual replies today, newly accepted since the previous status task window, connect tasks run today, and qualified remaining. Senders with no heartbeat for the active day, or whose connect lane is blocked by daily/weekly limits, are omitted; if every sender is omitted, the task reschedules without posting Slack. The task is seeded during daemon startup and is allowed through active-hours sleeps like manual replies so status does not depend on a connection sweep, but it suppresses Slack posting when normal outreach is intentionally inactive: outside active hours/rest-day windows and no pacing catch-up lane is open for any sender.

## Drip Campaigns (`drip/`)

The drip subsystem is implemented in this repository as a separate Django domain, not as another repository and not as another state inside the current connection/post-connection campaign. It deliberately reuses only the persistent `linkedin.Task` transport and existing provider runtimes. `DripCampaign`, immutable `DripCampaignVersion`, `DripEnrollment`, independent `DripLane` rows, frozen `DripDelivery` rows, and `DripDeliveryAttempt` submission-boundary ledgers own the lifecycle. Drip never advances, completes, or fails `Deal.state`. A database constraint permits only one nonterminal drip enrollment per Lead across every drip campaign, one lane per enrollment/channel, and one delivery per lane/theme/step; a conditional recipient-owner constraint prevents two nonterminal lanes from owning the same provider recipient.

**Manifests and reviewed enrollment.** One schema-v1 JSON manifest carries ordered themes for canonical ICPs, the same canonical sender set across each audience, shared intent, and independent sender-specific `linkedin`/`gmail` renditions. Validation rejects duplicate JSON keys, noncanonical ICPs/operators, unknown fields/placeholders, invalid delays, empty present renditions, LinkedIn subjects, and any Gmail subject that differs from the first subject for that sender/audience. Publication stores a normalized immutable snapshot plus SHA-256 content hash; disk changes never mutate an enrolled version. `validate_drip_campaign` is read-only. `publish_drip_campaign`, `enroll_drip_campaign`, `review_drip_handoff`, and `reconcile_drips` are no-write by default and require `--apply` for mutation. `plan_drip_enrollments` requires an exact canonical operator, repeated explicit Lead IDs, and a new private review artifact; apply revalidates its hash, published version, Lead snapshot, eligibility, and human reviewer before creating anything. Its PostgreSQL lock targets only the campaign row while the nullable active-version relation is joined, avoiding an invalid `FOR UPDATE` on the outer-join side. There is no ICP-wide auto-enrollment path.

**Independent handoff and ownership.** Every enrollment has one LinkedIn lane and one Gmail lane. A lane keeps the frozen canonical operator, provider account, sender/Send-As identity, recipient identity, exact handoff evidence, and its own theme anchor. LinkedIn handoff requires a connected/completed Deal, no live current `follow_up` Task for that Lead/operator, and the exact persisted final `linkedin_connect_followup` message for the current frozen ICP template length; canonical `Message.operator` owns new rows, with resolved `Message.sender` only for older rows. Gmail handoff requires every current `gmail_fallback` step exactly once, no live current Gmail/enrichment Task, exact account/Send-As ownership, one safely mailbox-scoped raw thread ID, one original subject, and valid RFC Message-ID/References continuation metadata. A current Gmail Task whose durable submission marker has no exact persisted automation-key Message is an unresolved provider outcome and blocks handoff permanently until a human reconciles the evidence. The narrow `review_drip_handoff --not-applicable` path is only for a channel sequence that truly never ran; any unresolved Gmail submission, current raw automation key, legacy `gmail-send:` evidence, LinkedIn sequence evidence, or live current Task blocks that attestation. Evidence appearing after an earlier `not_applicable` review also blocks either channel's handoff. The Lead is locked before enrollment/lane state. Once `handed_off_at` exists, `drip_owns_channel()` remains true through pause, stop, or completion, and current connect/follow-up healing plus Gmail handoff/enrichment/send paths fail closed rather than silently reclaiming the channel.

**Reconciliation and timing.** `reconcile_drips` runs one finite database-only pass under a PostgreSQL transaction advisory lock and Lead-first row locks. It never opens a browser, calls Gmail/LinkedIn, sleeps, or loops as a service. Dry-run returns deterministic JSON decisions without writes. Apply checks the shared stop policy, evaluates the two handoffs independently, skips omitted channel renditions explicitly, advances completed themes, freezes rendered copy, and links at most one outstanding Delivery/Task to each lane. A terminal Task left on a planned/queued delivery is safely detached and the same frozen Delivery may receive a new Task only while campaign/enrollment/lane controls are active; a running Task is never detached. First-theme timing uses `max(enrollment.activated_at, current_sequence_completed_at)`. Later same-theme steps use the previous successful same-channel `sent_at`; the next theme starts fresh at the preceding theme's final successful send. Only LinkedIn due times are normalized into the current active-hours/rest-day window. Gmail due times remain channel-local and browser-hour independent. Apply records aggregate `WorkflowRun(name="drip-reconcile")` evidence. No reconciler schedule is installed by the code change.

**Stop semantics.** `linkedin.tasks.stop_checks.lead_automation_stop_reason()` reads only persisted local state: inbound LinkedIn/Gmail `crm.Message`, qualifying persisted `crm.Meeting`, `Lead.disqualified`, and suppression. It does not query providers, Granola, Gemini, or live conversations. Current and drip enqueue/handler boundaries recheck this policy, and drip executors recheck immediately before the provider submission boundary. Newly created inbound messages from LinkedIn persistence and Gmail context ingestion schedule an after-commit callback that retires resolvable pending current automated messaging work and atomically stops the enrollment, both lanes, planned/queued deliveries, and pending drip Tasks; `manual_reply` is not retired. The accepted tradeoff remains the ingestion window: Gmail replies are unknown until context sync persists them, and LinkedIn replies are unknown until the realtime listener/backfill persists them.

**Execution.** `drip_linkedin` has payload `{"delivery_id": ..., "operator": ...}` and is claimable only by the matching existing sender daemon while that sender still owns an active traditional Campaign. Current follow-up/connect work precedes it in queue priority. The executor validates frozen ownership, connection proof, stop/timing/predecessor state, and the account-wide follow-up limit; it uses one direct-message UI route with an immediate pre-click attempt stamp. Confirmed success persists `crm.Message`, `ActionLog`, Delivery, and Attempt without changing the Deal. A proven pre-submit failure releases the same Delivery for a later reconciliation; a possibly submitted or stale post-boundary attempt becomes `unclear`, pauses the lane, and is never automatically rerouted or retried.

`drip_gmail` is excluded from LinkedIn claims. `daemon_supervisor.py` launches a separate `run_gmail_worker --account <mapped-account>` child for the checkout's resolved mailbox, and restarts/stops it with the browser daemon. The account-scoped worker atomically claims both current `gmail_follow_up` and `drip_gmail` Tasks for every configured operator/Send-As alias routed through that OAuth mailbox. The drip executor either continues the exact inherited current thread using its raw thread ID, original subject, final RFC parent and accumulated References, or opens a new thread only after reviewed `not_applicable`; every later drip Gmail delivery continues the lane-owned thread. It persists provider message/thread/RFC IDs separately and never changes LinkedIn or Deal state. Stale recovery returns known pre-submit work to retryable state and turns a post-boundary unknown into `unclear`.

**Gmail RFC threading.** Gmail may replace the `Message-ID` supplied in a MIME request. After a confirmed provider submission, `GmailClient` reads the sent message metadata, requires one strict provider RFC header, and returns that canonical stored value; current and drip persistence validate the delivered parent and every stored Reference before continuation. Current Gmail Tasks persist `gmail_submission_attempted_at` in their payload immediately before provider execution. A marker without its exact outbound automation-key Message becomes failed, permanently blocks automatic recreation, and blocks drip handoff. A marker with that exact Message can be requeued only to run the sent-step dedupe path, heal a missing successor from the persisted `sent_at`, and complete bookkeeping without resending. Drip uses its attempt ledger to mark an unresolved delivery `unclear` and pause its lane instead.

**Gmail delivery identity.** Send-As aliases are outbound identities, not assumed inboxes. Each operator uses a verified same-person `@boundera.io` Reply-To inside the connected OAuth mailbox: `ariant@boundera.io`, `leili@boundera.io`, `athena@boundera.io`, or `eddy@boundera.io`. OAuth setup and runtime validation require both From and Reply-To identities to belong to that mailbox. Context ingestion watches the full live mailbox identity set, attributes either configured identity to the exact operator, and Granola matching treats both static identity sets as internal.

**Operations and current status.** Django Admin exposes campaign and enrollment pause/resume/stop controls, lane-level pause/resume/stop controls, and read-only version/delivery/attempt evidence. `drip/campaigns/README.md` documents the dry-run-first operator workflow. The code, migrations, routing, executors, guards, and automated tests are implemented; this repository change does not include a production campaign manifest, does not publish or enroll a live cohort, does not install a periodic reconciliation job, and does not claim the controlled pilot is complete.

## Standalone Invitation Withdrawal

`manage.py withdraw_invitations --account primary|backfill [--since YYYY-MM-DD] --before YYYY-MM-DD [--limit N] [--apply]` is intentionally outside the Task queue and daemon. `--since` is an optional inclusive midnight boundary and `--before` is an exclusive boundary in `ACTIVE_TIMEZONE`; omitting `--limit` attempts every visible LinkedIn Sent Invitation in the approximate date window, while providing it caps confirmed withdrawals, not CRM candidates processed. Default no-apply mode remains a DB-only attribution report: it prints the account/operator, evidence counts, exclusions, selected date range, CRM scan pool, and withdrawal target. Apply mode is LinkedIn-date driven and is not limited to that CRM pool, so a zero-candidate CRM pre-plan does not skip the authenticated live scan; at normal verbosity it omits the full CRM candidate row dump so long live runs stay observable, while `-v 2` restores the debug list. `--apply` acquires a same-sender PostgreSQL advisory lock on a dedicated connection, rejects a fresh `DaemonHeartbeat` or persistent Chromium `Singleton*` marker, then opens `StandaloneLinkedInSession` and verifies Voyager's self-profile resolves to the requested operator.

CRM reconciliation is account-wide and requires positive OpenOutreach attribution. Current rows need `invitation_sent_at` plus same-operator `invitation_sender`. Legacy pre-plan rows need a nonblank `sent_note` and exactly one CONNECT `ActionLog` for the selected `LinkedInProfile` and same Campaign written from 0 through 10 seconds after the Deal's PENDING `update_date`; a log that can match two Deals is ambiguous and excludes both from the pre-plan. PENDING alone, partial ledgers, other senders, missing public IDs, and multiple proven Deals for one LinkedIn profile do not prevent date-only LinkedIn cleanup. Before any click, `actions/invitations.py` opens `/mynetwork/invitation-manager/sent/` and gradually scrolls the actual `<main>` container with randomized ordinary mouse-wheel increments and pauses. It scans live sent-invitation cards by visible `Sent N days/weeks/months ago` labels until it reaches a genuinely stagnant list end, hits the two-hour time cap, satisfies `--limit`, or passes the optional `--since` boundary; LinkedIn's displayed People total is not trusted as a completion boundary because it can underreport loaded invitation history. There is no hard scroll-round cap; rounds remain telemetry. Each scroll round extracts all loaded card profile URLs, Withdraw aria labels, and sent-age labels in one browser-side pass so runtime scales with LinkedIn loading rather than per-card Playwright calls. The visible labels are approximate and intentionally tolerated because LinkedIn coarsens month labels. The click is scoped to each date-eligible card's own Withdraw control; the confirmation must expose one unambiguous Withdraw control, and that exact card must disappear before success is recorded. Every confirmed click writes `InvitationWithdrawalRecord` with public ID, visible name, visible label, source, and optional Deal link. A live card under the verified sender plus an exact public-ID match to one PENDING Deal owned by that sender's Campaign is authoritative legacy attribution even when the DB pre-plan was empty: the command links the record, stamps `invitation_sender` and `invitation_withdrawn_at`, marks the Deal FAILED/Failed without fabricating an exact `invitation_sent_at`, and writes one `ActionLog.WITHDRAW_INVITE`. Proven pre-plan candidates retain their stricter ledger validation. The post-click write retries once on a dead DB connection, and the same operator's automatic connect lane rejects any Lead with confirmed CRM withdrawal history.

## Qualification ML Pipeline

Long withdrawal batches retry exact-card lookup when a LinkedIn reload transiently destroys Playwright's execution context. Cleanup also tolerates a leftover confirmation dialog detaching itself during cancellation; the following exact-card lookup remains authoritative.

GPR (sklearn, ConstantKernel * RBF) inside Pipeline(StandardScaler, GPR) with BALD active learning:

1. **Balance-driven selection** — n_negatives > n_positives → exploit (highest P); otherwise → explore (highest BALD).
2. **LLM decision** — All decisions via LLM (`qualify_lead.j2`). GP only for candidate selection and confidence gate.
3. **READY_TO_CONNECT gate** — P(f > 0.5) above `min_ready_to_connect_prob` (0.9) promotes QUALIFIED → READY_TO_CONNECT.

384-dim FastEmbed embeddings stored directly on Lead model, per-campaign GP models at ``Campaign.model_blob` (BinaryField)`. Cold start returns None until >=2 labels of both classes.

## Django Apps

Three apps in `INSTALLED_APPS`:

- **`linkedin`** — Main app: Campaign (owned by one User), LinkedInProfile, SearchKeyword, ActionLog, Task, LinkedInDiscoveryLead, and LinkedIn feed collection job/post/observation models. All automation logic.
- **`crm`** — durable contacts/messages/meetings plus the canonical sales domain (`SalesOwner`, `Account`, `Opportunity`, contacts, Actions, stage events, and Sheet baselines). Legacy `Deal` remains outreach automation state and defines `ClosingReason`.
- **`chat`** — `ChatMessage` model (GenericForeignKey to any object, content, owner, answer_to threading, topic).

## CRM Data Model

- **Campaign** (`linkedin/models.py`) — `name` (unique), `user` (FK to User), `product_docs`, `campaign_objective`, `booking_link`, `is_freemium`, `action_fraction`, `seed_public_ids` (JSONField).
- **LinkedInProfile** (`linkedin/models.py`) — 1:1 with User. Credentials and outbound rate limits (`connect_daily_limit`, `connect_weekly_limit`, `follow_up_daily_limit`). Discovery volume is configured globally through `DISCOVERY_DAILY_LIMIT`, not per profile. Methods: `can_execute`/`rate_limit_reasons`/`record_action`/`mark_exhausted`. In-memory `_exhausted` dict records LinkedIn-reported daily exhaustion separately from DB-enforced daily/weekly/global caps.
- **SearchKeyword** (`linkedin/models.py`) — FK to Campaign. `keyword`, `used`, `used_at`. Unique on `(campaign, keyword)`.
- **ActionLog** (`linkedin/models.py`) — FK to LinkedInProfile + Campaign. `action_type` (connect/follow_up/withdraw_invite), `created_at`. Composite index on `(linkedin_profile, action_type, created_at)`.
- **Lead** (`crm/models/lead.py`) — Per LinkedIn URL (`linkedin_url` = unique). `public_identifier` (derived from URL). `first_name`, `last_name`, `company_name`. `description` = parsed profile JSON. `embedding` = 384-dim float32 BinaryField (nullable). `disqualified` = permanent exclusion. `embedding_array` property for numpy access. `get_labeled_arrays(campaign)` classmethod returns (X, y) for GP warm start. Labels: non-FAILED state → 1, FAILED+DISQUALIFIED → 0, other FAILED → skipped.
- **Deal** (`crm/models/deal.py`) — Per campaign (campaign-scoped via FK). `state` = CharField (ProfileState choices). `closing_reason` = CharField (ClosingReason choices: COMPLETED/FAILED/DISQUALIFIED). `reason` = qualification/failure reason. `invitation_sent_at`, `invitation_sender`, and `invitation_withdrawn_at` form the positive project-send/withdrawal ledger; PENDING state alone is never evidence that OpenOutreach sent the invitation. `connect_attempts` = retry count. `backoff_hours` = check_pending backoff. `creation_date`, `update_date`.
- **Account / Opportunity** (`crm/models/sales.py`) — stable UUID-backed account sales motions, separate from Deal. Opportunity owns the explicit sender, canonical stage and stage-entered time, sales-motion step, last meaningful activity, manual pin, value/probability, and won/lost outcome. CRM v2 also persists whether it is an Active Account, the deterministic admission reason/reason set and evidence tier, evaluation time, and reversible inactive metadata. Weak automated rows are deactivated rather than deleted or closed; human sales fields remain authoritative.
- **OpportunityContact / OpportunityAction** (`crm/models/sales.py`) — many stable Lead relationships per Opportunity with champion/decision-maker/stakeholder roles, plus durable owner/target/due/waiting/draft/disposition/handled state for the next work item. Names are display values only.
- **OpportunityStageEvent / OpportunitySheetState** (`crm/models/sales.py`) — stage audit history and per-human-field Sheet baselines used for conservative three-way merge conflict detection.
- **Task** (`linkedin/models.py`) — `task_type` (`Task.TaskType` choices; `Task.save()` skips task-type choice validation so deploy-skew rows can still be marked failed), `status` (pending/running/completed/failed), `scheduled_at`, `payload` (JSONField), `error`, `started_at`, `completed_at`. Composite index on `(status, scheduled_at)`. Pending/running `sweep_connections` and `discovery` rows require `payload.operator`; discovery payloads carry the `mynetwork_recommendations` source, section/scroll counters, per-run seen identities, and a bounded profile queue. Startup reconciliation replaces pending search-era payloads with fresh recommendation state. Migration `0022` fails pending/running legacy `withdraw_invites` rows before removing that task choice; migration `0023` adds discovery.
- **LinkedInDiscoveryLead** (`linkedin/models.py`) — separate non-CRM collection table, globally unique by canonical public identifier and profile URL. Stores parsed Voyager profile data plus the first storing operator/account, one potential ICP, and collection timestamps. Rows never become outbound-eligible through this model.
- **LinkedInFeedCollectionJob / LinkedInFeedPost / LinkedInFeedObservation** (`linkedin/models.py`) — feed collector ledger. Jobs are one per sender/day and carry retry/completion state. Posts are canonical activity/text records. Observations record which sender account saw each post and how often.
- **FedRAMPMarketplaceSourceState / FedRAMPMarketplaceSignal** (`linkedin/models.py`) — durable official-marketplace baseline and review ledger. Source states retain changelog IDs and compact product snapshots. Signals use a unique canonical transition key and retain Codex decisions plus `slack_notified_at`, making collection and notification idempotent across machines that share the database.
- **ChatMessage** (`chat/models.py`) — GenericForeignKey to any object. `content`, `owner`, `answer_to` (self FK), `topic` (self FK), `recipients`, `to` (M2M to User).

## Key Modules

- **`daemon.py`** — Worker loop with outbound scheduling plus sender-aware discovery gating, `_build_qualifiers()`, `heal_tasks()`, freemium import, `_FreemiumRotator`. Weekday claims exclude discovery until the sender completes connection work; once the actual connectable pool is empty, discovery wins over synthetic pacing catch-up from self-rescheduling connect tasks. Rest-day discovery is unrestricted, while manual replies/status summaries retain priority.
- **`discovery/config.py`** — strict discovery setting validation, local-day boundaries, actual-pool-aware weekday connection-completion checks, and unrestricted rest-day gating. Empty connect-task retries are queue-healing state, not unfinished connection work, so they cannot block discovery.
- **`discovery/sources/mynetwork_recommendations.py`** — section-rooted `/mynetwork/grow/` extraction for `Suggestions for you` and dynamic `People you may know...` blocks, including exact section-scoped Show All overlays and bounded scrolling.
- **`discovery/sources/profile_recommendations.py`** — optional depth-1 extraction from a visited profile's `More profiles for you` rail and exact browse-map overlay; depth-1 profiles never expand recursively.
- **`discovery/sources/recommendation_common.py`** — canonical profile-anchor extraction, source metadata, bounded overlay scrolling, authentication checks, and exact overlay dismissal shared by the two read-only sources.
- **`discovery/screening.py`** — low-temperature structured batch scorer against the current sender's enabled discovery ICP descriptions. The model returns one best ICP plus a 0-100 fit score per card; code applies `DISCOVERY_VISIT_SCORE_THRESHOLD`, whitespace-canonicalizes returned profile IDs before strict unknown/duplicate validation, and still rejects invented ICP names.
- **`discovery/collector.py`** — deterministic CRM/discovery/suppression skips, atomic env-configured per-sender daily-cap enforcement, Voyager profile persistence, bounded task cursor/counters, next-day rollover, and sender-scoped startup task reconciliation.
- **`tasks/discovery.py`** — daemon handler entrypoint for one bounded discovery unit.
- **`management/commands/start_discovery.py`** — dry-run configuration/capacity/eligibility inspection and explicit task enqueue.
- **`management/commands/run_discovery_once.py`** — controlled live runner that keeps one selected browser profile open for up to `--max-tasks` sender-scoped discovery Tasks (default one), including their short continuation delays, and exits without claiming any other queue lane.
- **`management/commands/probe_discovery_recommendations.py`** — read-only saved-session selector probe. It fails closed on a fresh daemon/browser owner, verifies the authenticated identity, My Network sections/Show All, and one profile browse-map overlay, then writes only a sanitized local artifact.
- **`feed_collection.py`** — Daily LinkedIn home-feed collector helpers: sender/day job scheduling, CDP page collection, DOM extraction, canonical post upsert, and per-sender observation dedupe.
- **`marketplace_listener.py`** — Fetches and schema-validates the official FedRAMP changelog and full snapshot, detects new legacy Ready and Program-path Initial Implementation transitions, deduplicates the two source paths, and persists source baselines and target signals transactionally.
- **`granola.py`** — Read-only Granola API client. Bearer-authenticated note listing, exact-note retrieval, schema checks, and complete cursor-paginated transcripts; all transport/auth/shape failures become `GranolaError` without exposing the key.
- **`marketplace_analysis.py`** — Serializes unreviewed marketplace signals with CRM matches for Codex, validates Codex decisions, gates high-signal alerts, groups offerings by provider, and records Slack notification completion.
- **`management/commands/collect_linkedin_feed.py`** — Short-lived collector child command spawned by `daemon_supervisor.py`; connects to the daemon browser over CDP and stores feed posts.
- **`diagnostics.py`** — `failure_diagnostics()` context manager, `capture_failure()` saves page HTML/screenshot/traceback to `/tmp/openoutreach-diagnostics/`.
- **`tasks/connect.py`** — `handle_connect`, `ConnectStrategy`, `enqueue_connect`/`enqueue_follow_up`. Connect-note rendering uses `icp_outbound.safe_company_name()` so `"Unknown Company"` never leaks into outbound notes.
- **`actions/invitations.py`** — Human-paced Sent Invitations manager scanners, visible sent-age parsing, exact profile-card/name matching helper, URL-only date cleanup withdrawal, scoped Withdraw dialog confirmation, and post-click card-disappearance verification.
- **`invitation_withdrawal.py`** — Account-wide positive-evidence planning for CRM reconciliation, optional bounded date window, unique legacy connect-log matching, daemon/browser conflict checks, dedicated Postgres sender lock, date-based Sent-card batch execution, and withdrawal ledger persistence.
- **`management/commands/withdraw_invitations.py`** — Required account/date command interface, DB-only dry-run report, explicit `--apply`, credential-slot resolution, and authenticated LinkedIn identity verification.
- **`tasks/sweep_connections.py`** — `handle_sweep_connections`, `enqueue_sweep_connections`, shared `reconcile_pending_connections` and `process_accepted_deal`. Replaces legacy `check_pending`.
  Its pre-browser Pending ledger is deliberately a narrow values projection; it does not hydrate Campaign or Lead objects. Only LinkedIn-matched Deal IDs are hydrated afterward, with `Campaign.model_blob`, campaign documents/seeds, and `Lead.embedding` deferred. This prevents inactive campaigns with large trained models from multiplying those blobs across every Pending row and blocking the sender daemon before Playwright starts.
- **`tasks/follow_up.py`** — `handle_follow_up`, rigid ICP LinkedIn DM send routed through frozen `payload.icp` or legacy `resolve_icp`, sequence payload shim, rate limiting.
- **`tasks/manual_reply.py`** — `handle_manual_reply`, Slack-composed LinkedIn reply sends from the daemon's logged-in browser account.
- **`pipeline/qualify.py`** — `run_qualification()`, `fetch_qualification_candidates()`.
- **`pipeline/search.py`** — `run_search()`, keyword management.
- **`pipeline/search_keywords.py`** — `generate_search_keywords()` via LLM.
- **`pipeline/ready_pool.py`** — GP confidence gate, `promote_to_ready()`.
- **`pipeline/pools.py`** — Composable generators: `search_source` → `qualify_source` → `ready_source`.
- **`pipeline/freemium_pool.py`** — Seed priority + undiscovered pool, ranked by qualifier.
- **`lead_analysis.py`** — Offline Codex lead review queue/apply helper. Serializes leads awaiting qualification with profile and campaign context, validates Codex decision JSON, and applies decisions as campaign-scoped Deals without calling an LLM.
- **`followup_analysis.py`** — legacy lead/name-based Codex queue/apply helper used only by explicit `generate_followups --legacy`. It can write the former operator-tab shape without sending; canonical draft export/apply lives in `crm_followup_analysis.py` and `canonical_followup_command.py`.
- **`ml/qualifier.py`** — `Qualifier` protocol, `BayesianQualifier`, `KitQualifier`, `qualify_with_llm()`.
- **`ml/embeddings.py`** — FastEmbed utilities, `embed_profile()`.
- **`ml/profile_text.py`** — `build_profile_text()`.
- **`ml/hub.py`** — HuggingFace kit loader (`fetch_kit()`).
- **`browser/session.py`** — `AccountSession`: handle, linkedin_profile, page, context, browser, playwright. `campaigns` property (via Campaign.user FK). `ensure_browser()` launches/recovers browser. Cookie expiry check via `_maybe_refresh_cookies()`.
- **`browser/registry.py`** — `AccountSessionRegistry`, `get_or_create_session()`.
- **`browser/login.py`** — `start_browser_session()` — browser launch + LinkedIn login.
- **`browser/nav.py`** — Navigation helpers and `goto_page()`; profile discovery does not globally harvest profile links.
- **`db/leads.py`** — Lead CRUD, `lead_to_profile_dict()`, `get_leads_for_qualification()`, `disqualify_lead()`.
- **`db/deals.py`** — Deal/state ops, `set_profile_state()`, `increment_connect_attempts()`, `create_freemium_deal()`.
- **`db/enrichment.py`** — Lazy enrichment/embedding (`ensure_profile_embedded()`).
- **`db/chat.py`** — `save_chat_message()`.
- **`db/urls.py`** — `url_to_public_id()`, `public_id_to_url()` — LinkedIn URL ↔ public identifier conversion.
- **`db/messages.py`** — `persist_thread()`: idempotent get_or_create per `(source, external_id)`; derives LinkedIn direction from a normalized sender match against the Lead name, stripping common honorifics like `Dr.` while explicit daemon operator senders still force outbound. LinkedIn outbound echoes that arrive with the lead as sender are also forced outbound when they match stored `sent_note` text, a narrow legacy connect-note pattern, or a self-addressed opener such as `Hey <lead given-name token>,`; the token form handles stored middle initials and extra given names. Falls back to `now()` on malformed timestamps. Called from `actions/conversations.py:get_conversation` as a best-effort side effect — never breaks the caller.
- **`gmail/data_sync.py`** — Direct Gmail relationship/context helpers. Every exact email-bearing Lead is considered independently of legacy Deal state and `Lead.disqualified`; those remain send-suppression concerns. Exact identities are searched in batches of at most 40. A 500-hit truncated OR query is recursively split for fairness; a single noisy address is bounded at 2,000 hits and reported as capped. At most 80 unique threads are fetched per mailbox/run, with deferred/checkpoint telemetry for convergent follow-up runs. Drafts, scheduled mail, machine replies and list mail are excluded, full threads are date-clipped, group messages map per exact RFC participant, Send-As aliases resolve outbound ownership, and mailbox-local Gmail IDs are namespaced before `crm.Message` persistence. A separate metadata-only, newest-first 90-day scan inspects at most 500 message hits / 500 candidate threads and returns exact bidirectional external participants (including account provenance) that have no Lead; it never creates Leads or outbound work. Gmail-delivered Gemini/Meet notes persist into `crm.Meeting.gemini_notes_raw` with conservative title/date/lead matching.
- **`conf.py`** — Config loading (dotenv), `CAMPAIGN_CONFIG`, path constants, `get_first_active_profile_handle()`.
- **`exceptions.py`** — `AuthenticationError`, `TerminalStateError`, `SkipProfile`, `ReachedConnectionLimit`, `SheetsError`.
- **`onboarding.py`** — Interactive setup.
- **`agents/follow_up.py`** — ReAct agent for follow-up conversations. Tools: `read_conversation`, `send_message`, `mark_completed`, `schedule_follow_up`.
- **`actions/`** — `connect.py` (`send_connection_request`), `status.py` (`get_connection_status`), `message.py` (`send_raw_message`), `profile.py` (profile extraction), `search.py` (LinkedIn search), `conversations.py` (`get_conversation`).
- **`notifications/sheets.py`** — durable People publisher. Adds `Lead ID` only at the trailing edge, resolves stable ID before exact legacy LinkedIn URL, detects duplicate/ambiguous identities, indexes same-run appends immediately, and writes only managed changed cells. Rows are never cleared, reordered, or pruned; unknown columns and formula cells are not round-tripped through displayed values. `sync_sheets` is publication-only, shares the CRM v2 cross-process advisory lock, and performs no LLM synthesis or sales eligibility decisions.
- **`notifications/crm_sheets.py`** — shared safe Sheet primitives plus retired-surface adapters used only for exact first-cutover import, inventory, backup, and recovery. Legacy Opportunities, Pipeline, Recovery, and sender Followups are not canonical publication targets after v2 activation.
- **Canonical CRM v2 Sheet projection (`crm_v2_publish.py`, `notifications/crm_v2_sheets.py`)** — the account-first serializer and safe adapters for exactly two concise surfaces: one stable row per admitted `Active Accounts` opportunity and one owner-filterable current `Actions` queue. Admission evidence is explicit (`Why active`, evidence tier, meaningful touch, `Attention`, and `Who owes`); source threads stay out of the working view. Missing projection rows are cleared in place without deleting stable IDs, formulas, operator columns, or worksheets, and human cells retain the conservative three-way merge. Pipeline and Recovery are retired because their useful state is already represented on these two surfaces.
- **CRM v2 DB reconciliation (`crm_v2_reconcile.py`)** — the transactional write bridge from resolved account evidence to durable Account/primary Opportunity/contact state. Dry-run executes the apply path under rollback. Exact Opportunity/Lead links win; unanchored account domain/name ambiguity fails closed. Only admitted evidence creates rows, exact disqualified Leads remain linkable contacts, blank domains are filled only from one unique business domain, and stale bootstrap/system Opportunities are reversibly deactivated. Manual/Sheet Opportunities and pins are authoritative. The service never creates Actions or changes owner, stage, sales-motion step, value, or probability.
- **Canonical CRM domain** — `crm.SalesOwner`, `Account`, `Opportunity`, `OpportunityContact`, `OpportunityAction`, `OpportunityStageEvent`, and `OpportunitySheetState` are UUID-backed durable sales state. Legacy `Deal` remains outreach automation state. `MeetingParticipant` and `MeetingNote` add multi-contact and Granola/Gemini context while legacy Meeting fields remain for additive rollout. `Message.operator` stores strict sender provenance when known.
- **CRM v2 admission/action policy** — `crm_v2_policy.py`, `crm_v2_evidence.py`, and `crm_v2_actions.py` admit accounts from explicit human/Sales Motion state, real meetings, human Gmail, or substantive bidirectional LinkedIn evidence in that order. One-sided outbound remains People-only. At most one current action is generated per opportunity; exact thread-level direction decides `Needs response` versus `Waiting`, missing owner/target fails closed, and exact target Don't send suppresses outreach without erasing sales relevance. Human current actions, stage, owner, closure, and manual pins remain authoritative.
- **Legacy Followup generator** — the former lead/name-based sheet rebuild is retained only behind explicit `generate_followups --legacy` for deliberate recovery. Canonical drafting consumes persisted Actions and publication uses `refresh_crm_v2 --apply --routine`.
- **`docs/followups-sort-buttons.gs`** — Google Apps Script (paste into the spreadsheet's Extensions → Apps Script). Adds a "Followups" menu with two within-section sort actions on any `<Operator> - Followups` tab: "Sort: Action needed" (both Sent toggles = No first, then PRIORITY desc) and "Sort: Days since (oldest first)". Reads formulas alongside values so HYPERLINK cells survive the sort; writes each section's data rows back as one range so divider merges stay intact. Column order depends on `FU_HEADERS` in `notifications/sheets.py`.
- **Sales Motion account tracker skill (`skills/boundera-sales-motion/`)** — a repo-portable, non-secret Codex skill and deterministic native-tab clone/verifier for the separate Sales Motion workbook. The live `Template` tab remains authoritative; the helper uses `secrets/sheets-service-account.json` at runtime and preserves merges, dimensions, dropdowns, and conditional formats. `docs/sales-motion-summary.md`, `docs/sales-motion-framework.md`, and `docs/sales-motion-video-transcript.md` provide the concise, exact-task, and timestamped source layers respectively.
- **Boundera copy skills (`skills/boundera-sales/`, `skills/boundera-icp-messages/`)** — `boundera-sales` is intentionally limited to one-off email, LinkedIn, SMS/Slack, recap, scheduling, and objection copy outside the structured campaign Sheet. `boundera-icp-messages` exclusively authors or reviews sender-specific connection-note, LinkedIn-follow-up, and Gmail copy in the ICP Messages Google Sheet, defaulting authoring work to the exact Chuka tab while preserving the runtime no-fallback rule. Its human-maintained persona angles are separate from deployed JSON copy; it reads current LinkedIn and Gmail JSON and shares the sales skill's official-source 20x/Rev5 and research-backed length references. Neither skill carries a static capability inventory: a named product claim requires targeted evidence from the current FedRampGPT documentation, implementation, and tests, and existing campaign copy is not product evidence. The Sheet skill forbids targeted edits through the full-tab-clearing `--push` path and treats `--pull` as an explicit publication step. Neither skill applies the 15-step sales-motion framework to copy.
- **Sales calendar links (`linkedin/calendar_links.py`)** — canonical Arian Cal.com URLs for first-time introductions, established-opportunity next steps, technical/product deep dives, general calls, and quick chats. Sales drafting should select from the named constants instead of embedding remembered URLs.
- **`notifications/synthesis.py`** — retained legacy/manual helper only. The People publisher no longer invokes it, so publication cannot make sales decisions or mutate Lead/Deal synthesis state.
- **`api/client.py`** — `PlaywrightLinkedinAPI`: browser-context fetch (runs JS `fetch()` inside Playwright page for authentic headers). `get_profile()` with tenacity retry.
- **`api/voyager.py`** — `LinkedInProfile` dataclass (url, urn, full_name, headline, positions, educations, country_code, supported_locales, connection_distance/degree). `parse_linkedin_voyager_response()`.
- **`api/newsletter.py`** — `subscribe_to_newsletter()` via Brevo form, `ensure_newsletter_subscription()`.
- **`api/messaging/send.py`** — Send messages via Voyager messaging API.
- **`api/messaging/conversations.py`** — Fetch conversations/messages.
- **`api/messaging/utils.py`** — Shared helpers: `get_self_urn()`, `encode_urn()`, `check_response()`.
- **`setup/freemium.py`** — `import_freemium_campaign()`, `seed_profiles()`.
- **`setup/gdpr.py`** — `apply_gdpr_newsletter_override()`.
- **`setup/self_profile.py`** — `ensure_self_profile()`.
- **`setup/seeds.py`** — User-provided seed profiles: parse URLs, create Leads + QUALIFIED Deals.
- **`management/commands/discover_inbox_leads.py`** — Standalone LinkedIn Messaging inbox lead discovery. Uses the same env-backed `StandaloneLinkedInSession` account slots as message backfill (`LINKEDIN_USERNAME`/`LINKEDIN_PASSWORD` and `BACKFILL_LINKEDIN_USERNAME`/`BACKFILL_LINKEDIN_PASSWORD`). After login it opens the visible browser to `/messaging/`, then crawls Voyager messaging conversations from that authenticated browser context. The first batch uses LinkedIn's recent-conversation query; older pages use the same `lastUpdatedBefore` / `nextCursor` category query emitted by scrolling the conversation list in the UI, stopping at the 90-day default window or `--max-pages`. It skips existing leads by canonical LinkedIn URL, `Lead.public_identifier`, stored profile URN, or existing `crm.Message.thread_external_id`, and classifies non-duplicate 1:1 threads with the Boundera FedRAMP/CMMC `inbox_lead_relevance.j2` prompt. Campaign objective/docs are deliberately ignored for relevance; `--campaign` only chooses the destination Deal campaign. Full Voyager profile enrichment is preferred; when LinkedIn returns private/restricted 403s, the command falls back to the inbox participant payload (name, headline, profile URL / `fsd_profile` URN). Default mode is dry-run; `--apply` creates `Lead` + `Deal(state=CONNECTED)` rows, stamps canonical `Lead.icp` (`CSPs`, `3PAOs/Assessors`, `Advisors`, `Channel`, `CMMC Buyers`, `CMMC Advisor/Channel`), stores the LinkedIn thread through `persist_thread`, and stamps `Deal.last_reply_at` from newest inbound. It does not enqueue `Task` rows.
- **`management/commands/sync_gmail_context.py`** — Direct Gmail relationship/context command. `--dry-run` previews writes; `--operator` / `--account` scope the Gmail OAuth account; `--campaign`, `--lead-id`, and positive `--limit` are explicit diagnostics, while `--all-leads` is a deprecated no-op because all email identities are eligible by default. `--skip-unmapped-discovery` disables the bounded email-first candidate scan; its window/candidate caps have positive CLI overrides. Apply-mode default discovery atomically stores opaque thread-version checkpoints and structured candidates in private mode-0600 `data/gmail/<account>-context-state.json`; the direct API remains the programmatic result path. `--skip-threads` runs only Gemini/Meet note ingestion; `--skip-notes` runs only prospect email-thread ingestion. Google API request/response logging is suppressed and console output is aggregate-only by default. Non-dry-runs write aggregate `WorkflowRun(name="data-sync")` rows for all operators that share the synced Gmail account.
- **`management/commands/granola_notes.py` / `granola_sync.py`** — standalone lookup remains read-only; canonical refresh uses one incremental metadata scan, fetches details/transcripts only when needed, caches unmatched/ambiguous notes, and rematches them after Opportunity links exist. Matching is deterministic and loose note-body matching is impossible. Granola failure preserves cache/watermark and selects stored Gemini without aborting other CRM surfaces.
- **`management/commands/analyze_lead_qualification.py`** — Offline Codex lead qualification review. Export mode writes leads awaiting qualification to JSON with headline, profile text, company, location, public LinkedIn URL, current state, campaign objective, product docs, and an explicit decision schema. Apply mode reads Codex decisions (`lead_id`, optional `campaign_id`, `qualified`, `confidence`, `icp`, `reason`, `suggested_action`) and creates/updates campaign-scoped Deals: positives become `QUALIFIED` or `READY_TO_CONNECT` with `--ready`, rejects become `FAILED` with `ClosingReason.DISQUALIFIED`. It does not set global `Lead.disqualified=True`, does not call an app LLM, and is not in the daemon live connect loop.
- **`management/commands/sync_crm_v2_context.py`** — default no-write/apply-gated context phase. It refreshes Gmail/Gmail-delivered Gemini, strictly creates only validated corporate email-first Leads from private discovery state, relinks Gmail only when new Leads require it, and incrementally syncs Granola. Output is aggregate-only and it never sends.
- **`management/commands/preview_crm_v2.py`** — reads configured Sales Motion accounts and People Don't send state, resolves the active-account universe, and writes a private mode-0600 review artifact. It mutates neither DB nor Sheets.
- **`management/commands/refresh_crm_v2.py`** — canonical locked reconciler/publisher. Default mode runs the exact DB mutation path under rollback and performs zero Sheet writes. First apply requires a recent matching reviewed preview, publishes People as a preservation-checked prerequisite, imports only exact stable-ID human state, creates a full private workbook backup, stages and verifies both v2 tabs, and activates them atomically. Routine apply requires both canonical v2 tabs and no legacy canonical titles. DB failure after title activation invokes exact title compensation; post-commit cleanup removes obsolete archives except unresolved legacy material deliberately retained for review.
- **`management/commands/refresh_crm.py`** — retired legacy publisher. It refuses to run once either `Active Accounts` or `Actions` exists and must not be scheduled or used as a fallback.
- **`management/commands/generate_followups.py`** — canonical Codex draft queue by default. Export contains persisted, explicitly owned current Actions plus bounded conversation/context after eligibility. Apply validates Action/Opportunity/Lead IDs and the full semantic fingerprint atomically, fills only a blank draft/channel, and republishes through routine CRM v2; it never sends. The former lead/name tab rebuild requires explicit `--legacy`.
- **`management/commands/export_sales_search.py` / `export_sales_list.py`** — Sales Navigator people-search/list exporters using the dedicated `SALES_NAV_LINKEDIN_USERNAME` / `SALES_NAV_LINKEDIN_PASSWORD` session. The exported CSV remains compatible with `add_seeds --csv` and includes review metadata: `Profile URL`, `First Name`, `Last Name`, `Company`, `Title`, `Geo Region`, `Degree`. Prefer writing exploratory exports under ignored `artifacts/leads/`; keep `leads/` for intentional import-ready inputs.
- **`management/setup_crm.py`** — Idempotent CRM bootstrap (Site creation).
- **`admin.py`** — Django Admin: Campaign, LinkedInProfile, SearchKeyword, ActionLog, Task, ChatMessage.
- **`django_settings.py`** — requires Postgres through `DATABASE_URL` for every non-test runtime and fails closed when it is absent; pytest alone receives in-memory SQLite. Apps: crm, chat, linkedin.

## Realtime Inbound Message Listener (`linkedin/realtime/`)

Near-realtime detection of inbound LinkedIn DMs. Gated by `ENABLE_REALTIME_LISTENER` (`conf.py`, default `false`). Any failure degrades gracefully to the existing polling path — realtime is an enhancement, not a dependency.

### Architecture: Separate Child Process (v2)

The listener runs as a **separate child process** — `manage.py listen_realtime` — which the daemon spawns and supervises via `linkedin/realtime/supervisor.py` (`ListenerSupervisor`). This is the key architectural decision: the listener does NOT run in-process with the daemon.

**Why a separate process is required.** Playwright's sync API is built on a greenlet model: one event loop per process, and CDP event handlers and Playwright's task loop share it. An earlier in-process design (v1) attempted to drive CDP `Network.dataReceived` event callbacks while the daemon's task loop also drove sync Playwright — this corrupted Playwright's sync greenlet state and made the approach unworkable. Running the listener in its own process gives it a clean, independent Playwright/asyncio loop with no contention.

**Persistent browser context.** The daemon launches Chromium using `launch_persistent_context` (storing state under `data/profile-<account>/`) with a fixed `--remote-debugging-port` controlled by `LISTENER_CDP_PORT` (default 9222, localhost-only). This port is opened when `ENABLE_REALTIME_LISTENER` or `ENABLE_LINKEDIN_FEED_COLLECTOR` is on. The listener calls `connect_over_cdp` to attach to this already-running browser and shares its one browser context — one device fingerprint, one cookie jar. From LinkedIn's perspective this looks like one browser with two tabs, not two browsers, which is the correct bot-detection posture.

**`StandaloneLinkedInSession`** (used by `backfill_messages` and sales-nav flows) stays on `launch()` + per-account JSON cookie files (`data/<label>_cookies.json`). It is not migrated to a persistent context; only the daemon uses `launch_persistent_context`.

### Modules

- **`supervisor.py`** — `ListenerSupervisor`: spawns `manage.py listen_realtime` as a subprocess, restarts it on unexpected death, gives up after 5 consecutive spawn failures, and runs/stops the child according to listener-specific hours (`LISTENER_ACTIVE_START_HOUR`, `LISTENER_ACTIVE_END_HOUR`, `LISTENER_REST_DAYS`) rather than outbound active hours.
- **`listener.py`** — `run_listener` / `_run_one_connection`: calls `connect_over_cdp` to attach to the daemon's browser, opens a `/messaging/` tab in the shared context, enables the CDP `Network` domain, calls `Network.streamResourceContent` to opt in to streaming, and receives `Network.dataReceived` events carrying base64-encoded SSE bytes. Reconnects automatically on a dropped CDP connection.
- **`sse.py`** — `RealtimeSSEBuffer`: accumulates base64-encoded CDP stream chunks, decodes them, and frames the raw bytes into complete SSE events (splitting on `\n\n`).
- **`parser.py`** — `parse_realtime_event(raw_event) → ParsedRealtimeMessage | None`: decodes the SSE `data:` payload as JSON, walks LinkedIn's realtime envelope, and extracts sender URN, conversation URN, message URN, body text, and `sent_at` timestamp. Returns `None` for non-message events (presence pings, typing indicators, etc.).
- **`handler.py`** — `handle_realtime_event(raw_event, account_label)`: orchestrates parse → lead lookup → `persist_thread` → `stamp_inbound_linkedin_reply` → `notify_message_received`. Outbound echo events are persisted for thread completeness but not Slack-notified. Inbound replies stamp `Deal.last_reply_at`; if the matching Deal is still `Pending`, it is promoted to `Connected` and `connected_at` is set from the reply timestamp. Reply Slack notifications include the full Slack-safe quoted message body, preserving line breaks and only truncating near Slack's 3000-character section limit, and carry the triggering LinkedIn `thread_external_id` so the manual-reply modal can show the right sender-specific thread when one Lead is shared across operators. Slack notification HTTP uses certifi's CA bundle so daemon hosts with stale system trust stores still post reliably. All exceptions are caught and logged so a bad event never crashes the listener.
- **`heartbeat.py`** — Writes and reads `data/listener-heartbeat-<account>.json` (timestamp + account label). Updated by the listener process; read by startup catch-up to compute how long the listener was offline.
- **`lead_lookup.py`** — `resolve_lead_for_realtime(conversation_urn, sender_urn) → Lead | None`: queries the DB first by conversation URN (matched against `crm.Message.thread_external_id`), then falls back to sender member/profile URN (matched as a substring in `Lead.description`). If the conversation history points at one of our own operator profiles, a different sender-URN lead wins so old self-profile thread pollution does not suppress inbound alerts.
- **`catchup.py`** — `run_startup_catchup(account_label)`: reads the heartbeat file; if the gap since the last heartbeat exceeds `LISTENER_CATCHUP_GAP_MINUTES` (default 30), prompts the operator on TTY to run `backfill_messages --account primary --skip-prereq-gate`, or logs a warning when running headless.

### Why CDP `Network.streamResourceContent`, Not `eventSourceMessageReceived`

LinkedIn's `/realtime/connect` endpoint delivers a `text/event-stream` body over a regular `fetch()` call — it is not opened via the browser's native `EventSource` API. Playwright's `page.expect_event("websocket")` and CDP's `Network.eventSourceMessageReceived` only fire for native `EventSource` connections; they produce zero events here. The correct tap is `Network.streamResourceContent` (to opt in to streaming) followed by `Network.dataReceived` events, whose `data` field carries base64-encoded chunks of the raw SSE bytes. This was verified against a live LinkedIn session.

### Data Flow

```
CDP Network.dataReceived (base64 chunk)
  → RealtimeSSEBuffer.feed() → complete SSE event string
  → parse_realtime_event()   → ParsedRealtimeMessage | None
  → handle_realtime_event()
      → resolve_lead_for_realtime()  → Lead | None
      → persist_thread()             → crm.Message (idempotent)
      → stamp_inbound_linkedin_reply()
      → notify_message_received()    → Slack (replies webhook)
```

### Lifecycle

- **Supervisor**: `ListenerSupervisor` runs inside the daemon. It spawns `listen_realtime`, watches for unexpected exits (restart), gives up after 5 consecutive failures, and runs/stops the child according to listener-specific hours. Defaults are 0–24 with no rest days, so inbound Slack notifications can continue while outbound automation sleeps.
- **Reconnect**: inside the listener process, `_run_one_connection` wraps a single CDP session; `run_listener` loops around it so a dropped CDP connection triggers a clean reconnect without a full process restart.
- **Startup catch-up**: the daemon calls `run_startup_catchup(account_label)` during startup. If the heartbeat gap exceeds `LISTENER_CATCHUP_GAP_MINUTES` (default 30 min), it either prompts the operator interactively (TTY, defaulting to "no" after 10 seconds so startup continues unattended) or emits a `WARNING` log (headless) recommending:

```bash
.venv/bin/python manage.py backfill_messages --account primary --skip-prereq-gate
```

`--skip-prereq-gate` bypasses the interactive staleness prompt inside `backfill_messages` so it can be called non-interactively. `backfill_messages` snapshots existing LinkedIn message IDs before each thread fetch; after the thread is persisted, any newly created inbound `crm.Message` rows emit the same `notify_message_received` Slack reply notification as realtime listener events. Existing message IDs are not re-notified on reruns. The catch-up query includes normal sender-scoped `Connected`/`Completed`/`Failed` Deals plus owned `Pending` Deals that already have a stored LinkedIn thread, so replies to invite-note threads are not left as Invite Sent. Inbound replies use the same `stamp_inbound_linkedin_reply` repair path as realtime.

## Enrichment (`linkedin/enrichment/`)

Phone-number enrichment, **operator-triggered from Slack**. The
`EnrichmentWorker` always runs; auto-enqueue phone lookup on every inbound reply
is opt-in via `ENABLE_AUTO_PHONE_ENRICHMENT` (`conf.py`, default off). Email
enrichment is not Slack-triggered: it is the browserless prerequisite for the
post-accept Gmail sequence, gated by `ENABLE_GMAIL_SEQUENCE`.

**Trigger.** Every inbound-reply Slack notification (`notify_message_received`)
carries a "Reply on LinkedIn" button, a "Lead context" button, plus a
"📞 Get phone number" `static_select` menu — waterfall (default) /
bettercontact / leadmagic / prospeo. The operator's pick/button/modal submit is
POSTed by Slack to a Vercel serverless function (`api/slack_enrich.py`), which
verifies the Slack request signature (`SLACK_SIGNING_SECRET`, HMAC-SHA256) and
routes each Slack intention through an explicit action-id-to-handler dispatch table.
Enrichment picks parse `(lead_id, provider)` and INSERT an `enrich_phone`
`Task`; reply modal submits INSERT a daemon-dispatched `manual_reply` `Task`.
The function uses raw `psycopg` (no Django import), and `SLACK_BOT_TOKEN` is
required for `views.open` / `views.update`; queued status updates prefer the
interaction `response_url` and fall back to `chat.update` when metadata is
available, while daemon sent/failed status uses `chat.update` with the task's
saved Slack blocks. Manual reply sends opt into detailed send exceptions, so a
browser/UI send failure records the underlying send reason plus the diagnostic
screenshot/HTML folder path in `Task.error` instead of only a generic lead id.
The reply modal fetches the recent LinkedIn `crm.Message`
thread via raw SQL and renders a compact transcript above the reply textbox;
its bottom-left "Draft reply" action calls the configured OpenAI-compatible
endpoint (`LLM_API_KEY`, `AI_MODEL`, optional `LLM_API_BASE`) and fills the
reply textbox with the suggested LinkedIn reply. Drafts use a larger recent
thread window and are prompted to warm up the conversation, clarify the ask,
and avoid pitching Boundera unless product context is needed. New reply/context buttons
scope that preview by the triggering `thread_external_id`, while legacy
buttons fall back to the latest inbound LinkedIn thread for that lead. The
Lead context modal fetches deterministic Lead/Deal/profile/thread context via
raw SQL; its AI summary button updates the same modal. Generated summary/draft
text is saved to `linkedin.SlackLeadContextArtifact`, scoped by `(lead,
operator, thread_external_id, kind)`, so closing and reopening context/reply
flows can reuse the latest sender-specific sections. Slack `private_metadata`
only carries a compact open-modal cache while another action runs; when that
cache is too large, the reply-modal thread preview is discarded before the
original Slack source blocks so queued/cancel/sent status updates can still
replace the source notification. The loading row and newly generated section
render at the bottom of the active modal so progress and result appear below
existing context. The queued Slack status includes a cancel button whose
payload points at the inserted task id; cancelling deletes the task only if it
is still pending. If the preview fetch fails, the reply modal falls back to a
plain textbox. The `Task` table is the entire contract between
the function and the daemon — they never talk directly. The function dedups
against an existing `PENDING`/`RUNNING` `enrich_phone` task for the same
`(lead, provider)` (best-effort — a duplicate is harmless); two *different*
providers can be queued for one lead at once. Separately, the realtime
listener's handler (`linkedin/realtime/handler.py`) can still auto-enqueue a
`waterfall` task on a persisted inbound reply when `ENABLE_AUTO_PHONE_ENRICHMENT`
is on, with the same per-`(lead, provider)` dedup. Either path writes
`payload={lead_id, bettercontact_request_id, provider}`.

**Post-accept Gmail trigger.** When a lead reaches `CONNECTED`, the connect
path, sweep path, and no-reply backfill command call
`gmail.handoff.maybe_schedule_gmail_sequence`. The helper checks DB-local stop
conditions, suppression, Gmail enablement, operator Gmail mapping, and sender/ICP
email templates. Gmail template routing uses `resolve_icp(lead)`, so a stamped
`Lead.icp` such as `Channel` wins before the legacy classifier backfills blanks.
If the lead already has `Lead.email`, it queues durable
`gmail_follow_up` step 0; otherwise it queues `enrich_email`. Gmail and LinkedIn
steps use independent `delay_hours`; Gmail step 0 is anchored to
`Deal.connected_at`, while later Gmail steps are anchored to the previous Gmail
send time so catch-up runs cannot send multiple Gmail steps back-to-back. A
failed or skipped Gmail task does not block later LinkedIn steps, and a failed
LinkedIn step does not block Gmail. Both lanes stop on any inbound LinkedIn or
Gmail reply. Default post-accept cadence is LinkedIn at Day 0, Gmail at +0.33
hours, LinkedIn at Day 4, and Gmail at Day 8; later added Gmail steps default to
weekly spacing after Day 8. `delay_hours` supports fractional values, so roughly
20-minute Gmail offsets are valid.

**Gmail package.** The top-level `gmail/` package owns the Gmail post-accept lane:
OAuth/token loading (`auth.py`), Gmail API send/search (`client.py`), scheduling
(`handoff.py`), the worker loop (`worker.py`), task handlers
(`tasks/enrich_email.py`, `tasks/follow_up.py`), and email sequence copy
(`icp_emails.json` via `templates.py`). Gmail templates are separate from
`linkedin/icp_messages.json`, which remains LinkedIn/connect/follow-up copy.
`gmail/auth.py` maps Arian and Leili to `arian_boundera`, while Athena, Eddy,
and Chuka use `eddy_boundera`; Chuka sends as `eddy@getboundera.com`, which is a
verified alias on that OAuth account.
Missing Gmail copy for a sender/ICP/step is treated as that lane being disabled
for the lead and skips cleanly; malformed template rows still fail loudly.
Gmail subjects/bodies are parsed against an explicit placeholder allowlist
(`first_name`, `last_name`, `company_name`, `my_name`, `our_company_name`,
`our_website_url`) before rendering, `company_name` uses the same safe
`Unknown Company` fallback as LinkedIn copy, and
`manage.py validate_gmail_templates` renders every checked-in Gmail step with a
fake lead as a pre-send guard. In the ICP Messages Sheet pull path, blank email
subject/body cells for an otherwise valid ICP row save that sender/ICP's Gmail
block as an empty list so stale JSON copy cannot keep sending after an operator
clears the Sheet.
`manage.py gmail_oauth` creates per-account tokens under `data/gmail/`;
tokens request Gmail send, compose/draft, settings, and readonly scopes;
`manage.py gmail_send_test` sends a direct live test message through the mapped
operator alias.

`gmail/data_sync.py` is the Gmail-backed replacement for the Gmail-accessible
parts of the old Claude data-sync workflow. `manage.py sync_gmail_context` uses
the same OAuth tokens to batch-search every exact `Lead.email` without consulting
Deal state or `Lead.disqualified`, fetches each unique thread once, maps RFC
participants exactly, filters automated replies/newsletters, and persists human
messages through `linkedin.notifications.gmail_threads.persist_gmail_threads`.
It also scans a bounded recent mailbox window for exact bidirectional external
participants not represented by a Lead and returns them as structured review
candidates; it never guesses a company from message text or auto-creates a Lead.
The command separately searches Gmail-delivered Gemini/Meet note emails from
`gemini-notes@google.com` or `meetings-noreply@google.com`. Note emails attach to existing `crm.Meeting`
rows by title/date; when no meeting row exists, the command creates a synthetic
Meeting only if the note title uniquely identifies one Lead. Generic notes are
reported as unmatched rather than guessed. Successful non-dry-runs write
`WorkflowRun(name="data-sync")` for every operator mapped to the synced mailbox,
so the followup workflow's freshness check sees this direct-Gmail sync. This
path does not fetch Google Calendar attendee metadata or full Drive docs; those
still require Calendar/Drive API support if Gmail's note email is missing or
ambiguous.

Each daemon starts its `GmailWorker` with the daemon's resolved operator handle,
and `Task.objects.next_gmail(operator=...)` filters on `payload.operator`. This
keeps multi-node deployments from letting one sender's daemon claim another
sender's Gmail task and fail because that local node lacks the mapped OAuth
token.

**Worker.** `EnrichmentWorker` (`worker.py`) is a single background thread
`run_daemon` always spawns alongside the listener supervisor (no longer
flag-gated — the Slack menu is always available so enrichment must always be
processable). It claims `enrich_phone` and `enrich_email` tasks via
`Task.objects.next_enrichment()` — the outbound loop excludes `ENRICH_PHONE`,
`ENRICH_EMAIL`, and `GMAIL_FOLLOW_UP` from `claim_next`/`seconds_to_next`, and
`heal_tasks` excludes enrichment tasks from the stale-`RUNNING` reset, so the two
never race. The worker
reclaims its own stale `RUNNING` tasks at `start()` (the daemon has no clean
shutdown — this is the crash-recovery path). HTTP-only, so it is not gated on
active hours. Single-threaded is load-bearing: `next_enrichment` is a plain
read, not a locking claim.

**Waterfall.** `run_waterfall` (`waterfall.py`) iterates a provider chain.
`handle_enrich_phone` routes on the task payload's `provider` field: the
default `"waterfall"` runs the full `PROVIDER_CHAIN`; a specific provider name
(looked up in `PROVIDERS_BY_NAME`) runs that provider only, with **no
failover** — an unrecognized name logs a warning and falls back to the full
chain. The full chain is BetterContact → LeadMagic → Prospeo. `FOUND`/`NOT_FOUND` is terminal
(BetterContact's `NOT_FOUND` is authoritative — it is itself a 20+ provider
waterfall); `API_FAILURE` escalates. BetterContact is async (submit → poll,
resumable via the persisted `bettercontact_request_id`) and short-circuits to
`API_FAILURE` when the lead lacks the `last_name`/`company_name` its submit
needs. LeadMagic and Prospeo are synchronous and LinkedIn-URL native.
Providers implement the `PhoneProvider` protocol (`base.py`); transport calls
use certifi's CA bundle so daemon hosts with stale system trust stores can
still reach provider APIs. Transport failures raise `HttpError`
(→ `API_FAILURE` with structured status/body metadata persisted to
`Task.error` by the worker plus a cooldown-gated ops Slack degraded alert
keyed by provider/reason/status), malformed responses raise `EnrichmentError`.

**Email enrichment.** `handle_enrich_email` uses `BetterContactEmailProvider`,
which submits BetterContact in email-only mode:
`enrich_email_address=true`, `enrich_phone_number=false`. The parser reads the
email result separately from the phone result and stores a normalized email in
`Lead.email`. `Lead.email_providers_tried` records definitive email provider
answers (`FOUND` or `NOT_FOUND`); `API_FAILURE` is not recorded so it remains
retryable. On `FOUND`, the handler queues `gmail_follow_up` step 0. On
`NOT_FOUND`, it records the tried provider and stops the Gmail cadence for that
lead. On `API_FAILURE`, it leaves the provider retryable and marks the task
failed.

**Outcome (multi-number).** `handle_enrich_phone` (`linkedin/tasks/enrich_phone.py`)
lets a lead carry many numbers. `Lead.phones` is a JSON list of
`{number, provider, found_at}` — `FOUND` appends one entry (deduping a number
already present); `Lead.phone_numbers` is a bare-string convenience property.
`Lead.phone_providers_tried` records every provider that returned a definitive
result (`FOUND` or `NOT_FOUND`); `API_FAILURE` is not recorded, so it stays
retryable. Skip is per-provider — a single-provider task skips if that
provider is already in `phone_providers_tried`, but first returns a cached
`Lead.phones` number for that same provider when one exists so the provider API
is not billed again for legacy/manual rows. A waterfall task first returns any
cached phone from the provider chain, otherwise runs only providers not already
in `phone_providers_tried`, and skips once every provider is tried.
`FOUND`/`NOT_FOUND` post a Slack message via
`notify_phone_enriched`; all-`API_FAILURE` posts nothing and marks the task
`failed`.

## Node Monitoring (`linkedin/monitoring/`)

Liveness + degraded-state monitoring with no third-party service. Each
daemon is a "node"; the design relies only on Neon and the ops Slack
webhook. Gated by `ENABLE_NODE_MONITOR` (default on).

**Peer liveness — "is the daemon process alive".** A dead daemon cannot
report its own death, so peers report it. The `NodeMonitor` background
thread (`node_monitor.py`, same start/stop pattern as `EnrichmentWorker`)
runs every `MONITOR_INTERVAL_SECONDS`:

1. **Heartbeat** — `write_heartbeat()` stamps this node's `DaemonHeartbeat`
   row (`linkedin` app; one row per sender, keyed by the resolved operator
   handle) with `last_alive = now()` and clears `down_alerted_at`.
2. **Peer scan** — `check_peers()` considers every *other* node whose
   `LinkedInProfile.active` DB switch is enabled; a monitored peer whose
   `last_alive` is older than `PEER_STALE_MINUTES` is reported down to the
   ops Slack channel via `notify_degraded`. Historical heartbeat rows for
   inactive profiles are ignored.

The thread runs through the daemon's off-hours sleeps (separate thread),
so the heartbeat reflects "process alive", not "actively working".
`down_alerted_at` is an atomic claim+cooldown marker: the peer that wins
the `filter(...).update(down_alerted_at=now)` posts (so N peers don't all
alert for one outage), and the row is re-claimable only after
`DEGRADED_REALERT_HOURS`. `last_alive = NULL` means intentionally stopped —
the daemon calls `clear_heartbeat()` on a clean empty-queue exit so peers
don't false-alarm. For an intentionally retired sender, clear the `active`
checkbox on its LinkedIn profile in Django Admin; the checkbox is editable
directly from the profile list. It is authoritative even if the sender remains
listed in `EXPECTED_OUTBOUND_SENDERS`. **Coverage needs ≥2 daemons running**:
a lone daemon has no peer to watch it (an accepted limitation).

**Sender activity — "is the alive sender actually progressing".** The same
`NodeMonitor` tick also runs `check_expected_sender_activity()`. Expected
senders come from `EXPECTED_OUTBOUND_SENDERS` when set, otherwise from active
LinkedIn profiles that own active campaigns. After
`SENDER_ACTIVITY_GRACE_MINUTES` from the active-day start, an expected sender
with work should have outbound `ActionLog` rows. A fresh heartbeat plus stale
due outbound work and no recent `ActionLog` for
`SENDER_ACTIVITY_STALE_MINUTES` alerts as "outbound activity looks stuck".
Before declaring a sender stuck, the checker calls
`LinkedInProfile.can_execute()` for the due action types. If the sender is
blocked by the daily/weekly connect or follow-up limit, it alerts as "hit a
rate limit" and does not classify the outbound lane as stuck.
`DaemonHeartbeat.activity_alerted_at` is the atomic cooldown marker for this
class of alert. Healthy observations do not clear it: peer daemons can use
different runtime rate-limit overrides, and clearing the shared marker from a
different view would re-alert every monitor interval. The marker therefore
suppresses every activity alert for the full `DEGRADED_REALERT_HOURS`. This
separates healthy cap exhaustion from "monitor thread is alive but the outbound
lane is not making progress", which plain heartbeat liveness cannot see.

**Degraded detection — "alive but not working".** Runs inside the daemon,
which is the only thing that can observe its own state (`degraded.py`):

- **`TaskFailureTracker`** — an in-process consecutive-failure counter
  wired into the daemon's task-dispatch loop (`record_success()` /
  `record_failure()` around each handler call). One instance per process,
  so it is sender-scoped by construction — no DB query, no `Task.operator`
  column. `TASK_FAILURE_STREAK_THRESHOLD` failures in a row → one alert.

Listener heartbeat files are still written for startup catch-up, but there is
no self-alerting "realtime listener looks stuck" check. All monitoring alerts
route to the ops Slack channel (`SLACK_WEBHOOK_URL`). Monitoring is an
enhancement — tick exceptions are logged and never crash the outreach daemon.

## LinkedIn Feed Collection

The feed feature is split into collection and Codex-reviewed analysis. Collection saves LinkedIn home-feed posts visible to each sender account. The app never calls an analyzer LLM for this lane; a daily Codex automation reads the exported review queue, decides which posts matter, then applies structured decisions back to the DB. Slack alerts are sent only when Codex decisions mark high/urgent hits.

**Trigger.** `daemon_supervisor.py` is the daily wake-up process. When `ENABLE_LINKEDIN_FEED_COLLECTOR=true`, it starts a nonblocking child after `LINKEDIN_FEED_COLLECTION_HOUR:LINKEDIN_FEED_COLLECTION_MINUTE` in `LINKEDIN_FEED_COLLECTION_TIMEZONE` (default 17:00 America/Toronto):

```
.venv/bin/python manage.py collect_linkedin_feed
```

The supervisor tracks the child separately from the main daemon, so feed scrolling cannot block daemon restarts or git polling. A nonzero collector exit is retried after `LINKEDIN_FEED_COLLECTION_RETRY_MINUTES`.

For manual historical collection, `manage.py collect_linkedin_feed --backfill-days 14 --max-posts 1000 --stop-after-seen 200` bootstraps one bounded sender/day timeline job per local collection date for the current daemon sender. Jobs run oldest-to-newest so each completed day becomes the cutoff for the next day. `--since-days 14` remains available as a coarse one-off scan to `now - 14 days`, but it does not preserve daily timeline windows.

**Browser/session.** The collector resolves the same daemon account as outbound automation (`get_daemon_handle()` -> `LinkedInProfile` -> `resolve_operator()`), connects to the daemon's existing Chromium over CDP (`LISTENER_CDP_PORT`), opens a new `/feed/` page in the shared context, extracts visible post cards, closes its tab, and exits. There is no standalone LinkedIn login fallback in v1; if CDP is unavailable, the job fails cleanly and becomes retryable.

**Sequential cutoff.** Each job computes a sender-specific cutoff before scrolling. If a previous completed job exists for the same `(operator, account_username)`, the cutoff is that job's `scheduled_for` plus `LINKEDIN_FEED_COLLECTION_CUTOFF_OVERLAP_MINUTES` (default 1). If this is the first job, the cutoff is the previous local day's scheduled collection time plus the same overlap. LinkedIn's rendered relative labels (`5h`, `1d`, `2w`, etc.) are parsed into approximate `posted_at` values; once the collector reaches a parsed post timestamp at or before the cutoff, it stops without saving that older post. Historical `--backfill-days` jobs also pass an upper bound: older days stop claiming posts after that day's scheduled collection time, and today's job claims through the actual run time. This makes daily collection sequential by date/time while keeping a one-minute boundary overlap.

**Storage.**

- `LinkedInFeedCollectionJob` — one job per `(operator, account_username, collection_date)`, with `pending/running/completed/failed`, scheduled/start/finish timestamps, retry error, and collection counts.
- `LinkedInFeedPost` — canonical post record keyed by activity URN when available and a content hash fallback. Stores author metadata, an exact post permalink or activity-URN feed-update fallback, post text, raw extraction payload, and first/last seen timestamps. New URL-less cards are discarded before persistence; legacy URL-less rows remain for history but are excluded from analysis and alerting. The raw payload retains candidate anchor links so permalink selector drift can be diagnosed later.
- `LinkedInFeedObservation` — per-sender visibility record keyed by `(post, operator, account_username)` with first/last seen timestamps and `seen_count`.

The same post may appear in several sender feeds, so analysis should happen at the post level while response context can inspect observations to see which sender accounts saw it.

**Collection limits.** The date/time cutoff is the primary stop rule. `LINKEDIN_FEED_COLLECTION_MAX_POSTS` caps total extracted posts per run. `LINKEDIN_FEED_COLLECTION_STOP_AFTER_SEEN` remains a safety stop when LinkedIn returns unparseable or non-chronological feed items and the sender keeps re-encountering already-observed posts. `LINKEDIN_FEED_COLLECTION_SCROLL_PAUSE_SECONDS` controls the wait between scrolls.

**Analyzer.** `manage.py analyze_linkedin_feed` has two modes and does not call an LLM. Export mode selects resolvable posts with `analyzed_at IS NULL`, newest first, and writes every matching row to a JSON review queue for Codex (`--output`, `--since-days`, `--reanalyze`; `--limit` is only an optional manual/debug cap). Apply mode reads Codex-produced decision JSON (`--apply-json`) and saves `analyzed_at`, `intent` (`none/low/medium/high/urgent`), `audience` (`csp/advisor_partner/assessor/channel/other/not_relevant`), `topics`, `relevance_reason`, `suggested_action`, and `raw_analysis` onto `LinkedInFeedPost`. High/urgent posts in CSP/advisor/assessor/channel buckets post to `SLACK_HIGH_SIGNAL_URL` unless `--no-slack` is passed; successful alerts stamp `slack_notified_at`. Legacy URL-less rows are excluded as alert candidates and related grouped sightings. Alert formatting starts with the localized post date, uses a header block for the author, and ends with side-by-side Open and Comment actions. The Codex review instructions explicitly include: people who want a GRC automation tool, FedRAMP tool, FedRAMP 20x tool, CMMC/FedRAMP/GRC help, or want to work as / find a GRC, FedRAMP, CMMC, 3PAO, assessor, advisor, channel, or partner resource. A Pete Strouse-style FedRAMP advisory/partner opportunity is high-intent.

## Configuration

- **`.env`** (project root) — `DATABASE_URL` (required for non-test runtimes), `LLM_API_KEY` (required), `AI_MODEL` (required), `LLM_API_BASE` (optional). For Docker, pass via `docker run -e`.
- **CRM Sheets** — `GOOGLE_SHEETS_ID` and `GOOGLE_SHEETS_CREDENTIALS_PATH` are required by `sync_sheets`, `preview_crm_v2` when People safety state is read, and `refresh_crm_v2`; those commands fail closed rather than silently doing nothing. `GOOGLE_SHEETS_TAB_NAME` defaults to `People`. `SALES_MOTION_VERSIONS_GOOGLE_SHEETS_ID` identifies the separate read-only Sales Motion workbook and must never be used as the CRM write target.
- **Meeting context** — `GRANOLA_API_KEY` enables primary Granola sync; `GRANOLA_API_BASE` and `GRANOLA_HTTP_TIMEOUT_SECONDS` control its read-only client. A missing or unavailable key leaves cached/stored Gemini as fallback and does not widen action eligibility.
- **`conf.py` schedule** — `ACTIVE_START_HOUR` (9), `ACTIVE_END_HOUR` (17), `ACTIVE_TIMEZONE` ("America/Toronto"), `REST_DAYS` ((5, 6) = Sat+Sun). Daemon sleeps outside this window.
- **`conf.py` profile discovery** — `ENABLE_PROFILE_DISCOVERY` (default `false`), `DISCOVERY_DAILY_LIMIT` (25), `DISCOVERY_VISIT_SCORE_THRESHOLD` (70), and hard card/section/scroll/profile-recommendation/profile-visit/consecutive-no-match/run-time caps. On weekdays discovery is a fallback only after the daemon closes its connect lane for the day, connect tasks are scheduled beyond the local day, or no connectable work remains. Self-rescheduling empty connect tasks and their synthetic pacing catch-up do not block that handoff; real connectable work retains priority. Rest days are unrestricted. Daily boundaries use `ACTIVE_TIMEZONE`; there are no discovery start/end hours.
- **`conf.py` realtime** — `ENABLE_REALTIME_LISTENER` (default `false`), `LISTENER_CDP_PORT` (default 9222, localhost-only), `LISTENER_CATCHUP_GAP_MINUTES` (30), `LISTENER_PUMP_SLICE_SECONDS` (30), `LISTENER_ACTIVE_START_HOUR` (0), `LISTENER_ACTIVE_END_HOUR` (24), `LISTENER_REST_DAYS` (empty).
- **`conf.py` feed collection** — `ENABLE_LINKEDIN_FEED_COLLECTOR` (default `false`), `LINKEDIN_FEED_COLLECTION_HOUR` (17), `LINKEDIN_FEED_COLLECTION_MINUTE` (0), `LINKEDIN_FEED_COLLECTION_TIMEZONE` ("America/Toronto"), `LINKEDIN_FEED_COLLECTION_RETRY_MINUTES` (60), `LINKEDIN_FEED_COLLECTION_CUTOFF_OVERLAP_MINUTES` (1), `LINKEDIN_FEED_COLLECTION_MAX_POSTS` (200), `LINKEDIN_FEED_COLLECTION_STOP_AFTER_SEEN` (15), `LINKEDIN_FEED_COLLECTION_SCROLL_PAUSE_SECONDS` (2).
- **`conf.py` node monitoring** — `ENABLE_NODE_MONITOR` (default `true`), `MONITOR_INTERVAL_SECONDS` (300), `PEER_STALE_MINUTES` (15), `DEGRADED_REALERT_HOURS` (6), `TASK_FAILURE_STREAK_THRESHOLD` (5), `EXPECTED_OUTBOUND_SENDERS` (empty → infer), `SENDER_ACTIVITY_GRACE_MINUTES` (60), `SENDER_ACTIVITY_STALE_MINUTES` (90).
- **`conf.py:CAMPAIGN_CONFIG`** — `min_ready_to_connect_prob` (0.9), `min_positive_pool_prob` (0.20), `connect_delay_seconds` (10), `connect_no_candidate_delay_seconds` (300), `check_pending_recheck_after_hours` (24), `check_pending_jitter_factor` (0.2), `qualification_n_mc_samples` (100), `enrich_min_interval` (1), `min_action_interval` (120), `embedding_model` ("BAAI/bge-small-en-v1.5").
- **Prompt templates** (at `linkedin/templates/prompts/`) — `qualify_lead.j2` (temp 0.7), `search_keywords.j2` (temp 0.9), `follow_up_agent.j2`.
- **`requirements/`** — `base.txt`, `local.txt`, `production.txt`, `crm.txt` (empty — DjangoCRM installed via `--no-deps`).

## Docker

Base image: `mcr.microsoft.com/playwright/python:v1.55.0-noble`. VNC on port 5900. `BUILD_ENV` arg selects requirements. Dockerfile at `compose/linkedin/Dockerfile`. Install: uv pip → DjangoCRM `--no-deps` → requirements → Playwright chromium.

Self-hosted Postgres testing is separate from the app container: `compose/selfhost-postgres.yml` runs Postgres 17 on `127.0.0.1:55432` with SSL enabled by default, using ignored local secrets generated by `make selfhost-db-prepare`. A public shared host sets `SELFHOST_POSTGRES_BIND=0.0.0.0` and `SELFHOST_POSTGRES_PORT=5432`, optionally persisted in ignored `compose/.env`, and still needs any NAT router/firewall to forward inbound TCP `5432` to the Docker host. `make selfhost-db-restore-copy` dumps the current `.env` `DATABASE_URL` and restores it into the local Docker DB with a localhost target guard; when local Postgres client tools are missing, the restore script uses the running Postgres container's clients. This is a staging path for migrating from Neon to one shared self-hosted Postgres; production cutover still requires stopping all daemons, taking a final dump, restoring on the central host, and updating every `DATABASE_URL`.

## CI/CD

- `tests.yml` — pytest in Docker on push to `master` and PRs.
- `deploy.yml` — Tests → build + push to `ghcr.io/eracle/openoutreach`. Tags: `latest`, `sha-<commit>`, semver.

## Dependencies

`requirements/` files. DjangoCRM's `mysqlclient` excluded via `--no-deps`. `uv pip install` for fast installs.

White-label outreach uses four canonical `Lead.icp` values across CSV normalization, LinkedIn templates, Gmail templates, and the ICP Messages tabs: `White Label Product/Executive`, `White Label Partnerships`, `White Label Delivery`, and `White Label Champions`. Sender copy is populated for Arian and Chuka only, champion rows use an introduction/routing ask, and each LinkedIn connection-note bucket carries two short variants for within-sender message testing.

A1 FedRAMP Ready outreach uses the canonical `Lead.icp` value `Rev5 Ready`. CSV aliases normalize into that value, while Arian and Chuka route LinkedIn and post-accept Gmail copy around carrying Ready work forward after the July 28, 2026 transition to legacy status.

Stage-aware direct-buyer outreach uses four additional composite `Lead.icp` values without a schema migration: `20x Initial Implementation`, `Active FedRAMP Path`, `FedRAMP Mature`, and `CSP Stage Verify`. Together with `Rev5 Ready`, these route current Arian/Chuka LinkedIn and Gmail copy through the existing one-key template path. CSV normalization collapses Agency/FedRAMP In Process into `Active FedRAMP Path`, certified or mature programs into `FedRAMP Mature`, and unverified federal portfolios into `CSP Stage Verify`; the last bucket asks for the owner or exact stage rather than asserting one.

Investor-channel outreach uses two canonical `Lead.icp` values across CSV normalization, LinkedIn templates, Gmail templates, and the existing sender ICP Messages tabs: `Investor / Portfolio Ops` and `Accelerator / Ecosystem`. The former routes investor-platform and portfolio-support contacts; the latter routes private cohort and startup-program operators. Arian and Chuka carry the active copy. No schema migration or additional Sheet tab is required, and inbox classification remains unchanged.

Core: `playwright`, `playwright-stealth`, `Django`, `django-crm-admin`, `pandas`, `langchain`/`langchain-openai`, `jinja2`, `pydantic`, `jsonpath-ng`, `tendo`, `termcolor`, `tenacity`, `requests`
ML: `scikit-learn`, `numpy`, `fastembed`, `joblib`
