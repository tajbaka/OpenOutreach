# RB2B visitors → CRM leads → connection requests

This supersedes the discovery-inbox and Slack-polling designs. The webhook
stores unverified events in `RB2BVisitor`. Codex reviews those events; a qualified
person becomes a normal `crm.Lead` immediately. No discovery promotion, Campaign,
Deal, follow-up, Gmail, or InMail is involved. An intake row is a processing
receipt, not a second user-managed lead list.

Visitor outcomes (sent, held, failed, uncertain) go to the existing **#rb2b-v2**
channel through the existing bot and `RB2B_SLACK_CHANNEL_ID`. Actual inbound
LinkedIn messages go through `SLACK_REPLIES_WEBHOOK_URL`, with the existing
Reply on LinkedIn and Lead context controls. Changing outcome routing does not
change the reply channel. No Slack channel polling is needed.

## Rollout on the Windows sender computer

Code preparation is not rollout. On 2026-10-30 the Mac could not reach the shared
worker database (connection timeout). Migration, receiver cutover, existing-row
migration, and Windows runtime verification must be completed from a connected
machine before enabling the new automation. Vercel auto-deploys main. The receiver therefore retains a transitional v1
writer while the new table is absent (`storage_version: 1`), preserving intake
through rollout. Once migration 0038 commits, requests switch to the new queue
(`storage_version: 2`). Historical discovery rows are drained afterward.

1. Preserve local changes and safely update the OpenOutreach checkout to main.
   Use the existing Windows virtualenv, `.env`, database and exact Arian owner
   `ariantajbakh`. Do not run setup/bootstrap or replace credentials.
2. Preview the migration plan, then apply the additive migration:
   `python manage.py migrate --plan`, then `python manage.py migrate linkedin 0038`.
   Normal dependencies must already be current; investigate unrelated pending
   migrations rather than blindly applying them. No prospect is enrolled or sent.
3. Set `RB2B_SLACK_CHANNEL_ID=C0CDDBM8NTV` in the Windows `.env` and in the existing
   Vercel project. This is the user's existing **rb2b-v2** channel in workspace
   `T0ATUB9L8DT`. Use the existing `SLACK_BOT_TOKEN`; ensure that bot is a channel
   member with its existing `chat:write` permission. Do not add Slack read scopes.
   Preserve the existing replies webhook and all other channel configuration.
4. Verify Vercel has deployed this commit from main and the migrated receiver
   now reports v2. If a manual deployment is necessary, deploy **after** the
   migration. Use
   `python scripts/stage_rb2b_deployment.py --project-json .vercel/project.json`
   to stage only the allowlisted function files into a new temporary directory.
   If this checkout isn't linked, use the existing Vercel project `openoutreach`
   under `arians-projects-15dd6e80`, not a new project. Deploy the printed staging
   directory with the existing Vercel CLI/account. Never deploy the entire
   checkout, private artifacts, `.env`, cookies or tokens. Preserve
   `RB2B_WEBHOOK_TOKEN`, `RB2B_DATABASE_URL` and the existing RB2B authenticated URL.
   The stable route remains `https://openoutreach.vercel.app/api/rb2b_webhook`.
   Verify a provider test succeeds, unauthorized requests return 401, and the
   deployed source is this release. The v2 test response has `storage_version: 2`
   and `stored: false`. A provider test does not prove a real visitor was saved.
5. Preview then apply `python manage.py rb2b_workflow migrate-discovery --apply`.
   Wait at least 35 seconds after the verified receiver cutover (the old function
   has a 30-second maximum), then run it a second time to catch a request that
   was already in flight.
   The command merges exact hash-bound receipts and preserves every original row,
   event and review. RB2B-only originals are hidden from the discovery admin list;
   collector-origin rows remain visible and retain their classification. Old
   processing/send evidence holds the new intake record; never erase that hold
   or re-contact someone to make migration appear complete.
6. Fully restart the existing Arian supervisor once with the updated checkout,
   preserving campaigns, limits, stops and other workers' configuration. Verify
   its fresh `DaemonHeartbeat.rb2b_coordination` protocol/config and run
   `python manage.py rb2b_workflow preflight`. A stale/old worker or mismatched
   settings blocks IAB reservations. Check that the listener is enabled and
   healthy; heartbeat alone does not prove LinkedIn authentication. If an
   existing supervised task is in flight, let it finish before the restart.
7. Verify the deployed Slack interaction function and Windows worker access the
   same CRM database. Vercel masks sensitive values in env pulls; blank masked
   strings are not evidence of equality. Use existing secure configuration or
   scoped live readback without printing secrets. Do not silently repoint an
   unrelated database. Verify configured Slack destinations and bot membership.
8. Set `RB2B_IAB_SENDS_ENABLED=true` only after the preceding checks. Run one
   review preview, verify the exact Arian IAB session, then activate/update the
   single Windows Codex automation at **09:00 and 15:00 America/Toronto daily**.
   Keep the Mac copy paused. Keep Windows awake and Codex running.

RB2B showed seven days of trial integration access on 2026-10-30. Check provider
entitlement separately: a healthy receiver with no events does not prove that
RB2B is still delivering. Do not purchase or upgrade a plan automatically.

## Commands and evidence contract

Use the checkout's Python (typically `.venv\Scripts\python.exe` on Windows).
`rb2b_workflow` never opens a browser and never clicks Send. Codex uses **IAB**
for the LinkedIn UI; the command handles durable transitions. Keep JSON files,
notes and screenshots under ignored `artifacts/rb2b-intake/`. Never commit them.
Mutating commands preview/roll back unless `--apply` is present. A preview token
is always null and never authorizes a browser click.

- `export --output artifacts/rb2b-intake/queue.json`: pending work plus durable
  contact/attempt history. The result is in the JSON `result` array.
- `review --id ID --input decision.json [--apply]`: exact-source review, CRM
  creation/reuse and frozen connection note. Holds do not create contacts.
- `begin --id ID --input preflight.json --apply --output attempt.json`: reserve
  one connection attempt and return its token and exact note. **Commit and read
  this receipt before any final Send click.** Any refusal means do not send.
- `finish --id ID --token TOKEN --input outcome.json --apply`: record independent
  confirmation, a definite pre-click failure, or uncertainty. Repeating the
  identical receipt is safe; terminal receipts cannot be rewritten automatically.
- `replies --id ID --input thread.json --apply`: persist IAB-observed missed
  messages using their real LinkedIn message/thread identities. No fabricated
  message IDs or approximate timestamps. Then run `notify --apply`.
- `notify --apply`: flush pending Slack outcomes/replies, with persistent retry
  backoff. A notification retry never repeats outreach. Normal reruns are
  deduplicated. A lost Slack HTTP acknowledgement can cause a duplicate Slack
  notification because incoming webhooks have no exactly-once contract.

A qualification decision has this shape (all evidence is real, not these examples):

```json
{
  "decision": "qualify",
  "event_hash": "exact key from exported events",
  "linkedin_url": "https://www.linkedin.com/in/exact-person/",
  "member_urn": "urn:li:fsd_profile:EXACT_ID_FROM_LINKEDIN",
  "checked_at": "2026-10-30T14:00:00Z",
  "identity_verified": true,
  "fit_verified": true,
  "relationships_checked": true,
  "invitations_checked": true,
  "evidence_urls": ["https://www.linkedin.com/in/exact-person/"],
  "fit_reason": "Sourced explanation of company, role and audience fit",
  "first_name": "Example",
  "last_name": "Person",
  "company_name": "Example Company",
  "title": "Verified current role",
  "email": "",
  "icp": "Advisors",
  "note": "Reviewed personalized connection note, no more than 200 characters."
}
```

Use an exact existing Arian audience. Published-program-only audiences also
require `message_version_id`; this validates the audience without enrollment.
The review is at most 60 minutes old. For a hold, supply `decision: "hold"`,
`event_hash`, and a specific `reason` instead. Existing operator-owned CRM fields
are preserved; verified sender/member identity is stored on the linked receipt.
The code checks suppression, account/opportunity history, conversations, campaign
history and pending work. Codex still must check current LinkedIn UI, full
relationship context and company/role fit; a Boolean is not research evidence.

Pre-send evidence is at most five minutes old and contains `checked_at`, exact
`linkedin_url`, `sender: "Arian"`, and these true checks:
`sender_verified`, `identity_verified`, `no_pending_invitation`, `not_connected`,
`no_conversation`, `relationships_checked`, `no_platform_restriction`.

A sent outcome contains `status: "sent"`, `sender: "Arian"`, exact `linkedin_url`,
exact saved `note`, `checked_at`, `sent_invitations_verified: true`, and a private
`screenshot` path. A definite failure must include `status: "failed"`,
`send_clicked: false`, and `reason`. A click with missing confirmation is
`status: "uncertain"` with its reason. Never retry it. A crash that leaves
`attempting` holds the sender claim lane until explicit evidence-based resolution;
do not age it into an automatic retry or clear its budget reservation.

A reply catch-up input has `sender: "Arian"`, exact `linkedin_url`,
`identity_verified: true`, `checked_at`, `thread_external_id`, and `messages`.
Each message needs its real `entity_urn`, exact `sender`, `text`, and timestamp
formatted `YYYY-MM-DD HH:MM` **in UTC**. The command shares the existing persisted
message identity with realtime, so overlap produces one notification. If IAB
cannot expose a real message identity, hold that import and report the limitation;
do not invent IDs from text or time. The realtime listener still handles live
messages when running. The scheduled IAB catch-up handles downtime; the old
campaign-only `backfill_messages` selection is not used by this workflow.

## Send accounting and exclusions

Every committed IAB attempt counts against the sender's daily connection, weekly
connection and total daily budgets. It has its own durable ledger; no fake
Campaign, Deal or ActionLog is created. Failed and uncertain attempts retain
budget conservatively. Normal campaign pacing includes these reservations.
The Task claimer and repost claimer share the sender lock with `begin`, and
`begin` refuses while the sender has a running browser Task/repost. After an IAB
attempt, campaign connect/follow-up work honors its pacing window. This requires
updated workers; do not enable IAB sends against old processes.

The built-in canonical-URL digest exclusions cover two invitations independently
verified on 2026-10-30. Their identifying details and receipts stay in the private
handoff, not this public repository. Never clear these historical exclusions.
They were specifically approved weak-fit exceptions, not a qualification rule.

No CRM Sheet publisher, Active Accounts stage, customer record, campaign setting,
sequence, suppression, sender stop, or quota is changed by this workflow.

## Validation

Run `scripts/qa_campaigns.py --suite rb2b-intake` with the repository virtualenv
and local PostgreSQL 17. It uses a disposable socket-only database and blocks all
live delivery, network, environment-file and credential access. The broader
campaign suite covers shared counters, task claims and existing outbound behavior.
Tests are not proof of a Windows rollout or a real production invitation/reply.
