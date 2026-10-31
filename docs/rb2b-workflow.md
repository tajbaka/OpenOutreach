# RB2B visitors → CRM leads → worker-sent connection requests

This supersedes the discovery-inbox, Slack-polling and Codex in-app-browser (IAB)
send designs. The webhook stores unverified events in `RB2BVisitor`. A scheduled
Codex review qualifies them **without opening LinkedIn**; a qualified person
becomes a normal `crm.Lead` immediately and gets one queued `rb2b_connect` Task.
The existing Arian daemon then does every LinkedIn check and sends the single
connection request through the normal connect path. No discovery promotion,
Campaign, Deal, ActionLog, follow-up, Gmail or InMail is involved. An intake row
is a processing receipt, not a second user-managed lead list.

Visitor outcomes (sent, held, failed, uncertain) go to the existing **#rb2b-v2**
channel through the existing bot and `RB2B_SLACK_CHANNEL_ID`. Actual inbound
LinkedIn messages go through `SLACK_REPLIES_WEBHOOK_URL`, with the existing
Reply on LinkedIn and Lead context controls. Changing outcome routing does not
change the reply channel. No Slack channel polling is needed.

## Rollout on the Windows sender computer

Code preparation is not rollout. Vercel auto-deploys main, and the receiver keeps
a transitional v1 writer while the new table is absent (`storage_version: 1`).
Once migration 0038 commits, requests switch to the new queue
(`storage_version: 2`). Historical discovery rows are drained afterward.

1. Preserve local changes and safely update the OpenOutreach checkout to main.
   Use the existing Windows virtualenv, `.env`, database and exact Arian owner
   `ariantajbakh`. Do not run setup/bootstrap or replace credentials.
2. Check `python manage.py showmigrations linkedin`; a running supervisor's Git
   update may already have applied them. Otherwise preview with
   `python manage.py migrate --plan`, then apply `python manage.py migrate linkedin 0039`.
   Both migrations are additive (0039 only adds the `rb2b_connect` and
   `rb2b_replies` task-type choices). Investigate unrelated pending migrations rather than blindly applying
   them. Migrating enrolls and sends nothing.
3. Set `RB2B_SLACK_CHANNEL_ID=C0CDDBM8NTV` in the Windows `.env` and in the existing
   Vercel project. This is the user's existing **rb2b-v2** channel in workspace
   `T0ATUB9L8DT`. Use the existing `SLACK_BOT_TOKEN`; ensure that bot is a channel
   member with its existing `chat:write` permission. Do not add Slack read scopes.
   Preserve the existing replies webhook and all other channel configuration.
4. Verify Vercel has deployed main and the migrated receiver reports v2. If a
   manual deployment is necessary, deploy **after** the migration. Use
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
   was already in flight. The command merges exact hash-bound receipts and
   preserves every original row, event and review. RB2B-only originals are hidden
   from the discovery admin list; collector-origin rows remain visible. Old
   processing/send evidence holds the new intake record; never erase that hold or
   re-contact someone to make migration appear complete.
6. Set `ENABLE_RB2B_CONNECT=true` in the Arian checkout's `.env` (the realtime
   listener must also stay enabled: `ENABLE_REALTIME_LISTENER=true`) and fully
   restart the Arian supervisor once (environment flags are read at process
   start; a child-only restart can inherit the old value). Let an in-flight task
   finish first. Preserve campaigns, limits, stops and other workers'
   configuration. Run `python manage.py rb2b_workflow preflight`: `worker_alive`
   and `worker_connect_ready` must both be true. Check that the listener is
   healthy; a heartbeat alone does not prove LinkedIn authentication.
7. Run one review preview, then activate/update the single Windows Codex review
   automation at **09:00 and 15:00 America/Toronto daily**. Keep the Mac copy
   paused. The worker sends on its own schedule after each review.

RB2B showed seven days of trial integration access on 2026-10-30. Check provider
entitlement separately: a healthy receiver with no events does not prove that
RB2B is still delivering. Do not purchase or upgrade a plan automatically.

## Commands and decision contract

Use the checkout's Python (typically `.venv\Scripts\python.exe` on Windows).
`rb2b_workflow` never opens a browser and never sends. Keep JSON files and notes
under ignored `artifacts/rb2b-intake/`. Never commit them. Mutating commands
preview/roll back unless `--apply` is present.

- `export --output artifacts/rb2b-intake/queue.json`: every visitor with its events,
  review, status and worker outcome. The result is in the JSON `result` array.
- `review --id ID --input decision.json [--apply]`: exact-source review, CRM
  creation/reuse, frozen connection note and one queued `rb2b_connect` Task.
  Holds create no contact and no Task. Applying flushes hold notices to #rb2b-v2.
  Re-reviewing a `ready` visitor updates its note and reuses its Task; a visitor
  that failed before any send reservation may be reviewed again.
- `preflight`: Arian owner, whether the live Arian worker is beating and will
  send (`worker_connect_ready`, which requires the listener), whether it can
  connect right now, and how many RB2B Tasks are waiting.
- `notify --apply`: retry pending Slack outcomes/replies with persistent backoff.
  The worker and review already flush; this never repeats outreach. A lost Slack
  HTTP acknowledgement can cause a duplicate notice (webhooks have no
  exactly-once contract).

A qualification decision has this shape (all evidence is real, not these examples):

```json
{
  "decision": "qualify",
  "event_hash": "exact key from exported events",
  "linkedin_url": "https://www.linkedin.com/in/exact-person/",
  "checked_at": "2026-10-30T14:00:00Z",
  "identity_verified": true,
  "fit_verified": true,
  "relationships_checked": true,
  "evidence_urls": ["https://example-company.com/team"],
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
The review is at most 60 minutes old. `first_name`/`last_name` are required: the
worker refuses to send unless every one of their name tokens matches the live
LinkedIn profile. For a hold, supply `decision: "hold"`, `event_hash`, and a
specific `reason` instead. Existing operator-owned CRM fields are preserved. The
code checks suppression, account/opportunity history, conversations, campaign
history and pending work; the reviewer still checks Gmail, meetings, customer
context and company/role fit from sources. A Boolean is not research evidence.

## What the worker does

Each `rb2b_connect` Task is claimed by the Arian daemon only, inside normal
active hours or the connect catch-up window. It sits in the connect lane: a due
follow-up keeps its turn exactly as before, and a due RB2B visitor takes a due
campaign connect's turn. While `ENABLE_RB2B_CONNECT` is off, the realtime
listener is disabled, or the #rb2b-v2 channel, bot token or replies webhook is
missing, RB2B Tasks stay pending and are never claimed.

1. Recheck the DB exclusions; any match is a hold.
2. Shared limits: if `can_execute("connect")` fails, wait for the next active start.
3. Shared pacing: wait one normal connect gap after the sender's last invitation
   (campaign or RB2B). Due campaign connects are moved onto the same slot, so
   the RB2B visitor goes first; campaigns resume one gap after it.
4. Live checks, holding on any doubt: the Voyager profile must exist, keep the
   exact vanity, contain every reviewed name token and have a member URN not
   already on another visitor or CRM Lead; not 1st-degree; the profile page must
   not show Pending/Connected. The verified URN is saved on the receipt for
   reply routing. Errors here retry twice, 30 minutes apart, then the visitor is
   `failed` with nothing sent.
5. Recheck exclusions, then commit the reservation (`attempting`, `attempted_at`)
   only if the note and decision still equal the review those checks used. A
   re-review during the checks sends nothing; its own queued Task starts over.
6. Send once through the normal connect UI path with the exact saved note.
7. Confirm independently: the person must be listed on Arian's own Sent
   Invitations page (the same reader the withdrawal command uses, matched by
   exact vanity and name). The connect helper's own result only means Send was
   clicked.

| Result after the reservation | Status | Retried |
| --- | --- | --- |
| Listed on Sent Invitations after the click | `sent` | never needed |
| Custom-note limit (detected before Send) or LinkedIn refusal (`SkipProfile`) | `failed` | no |
| Clicked but not listed, Sent Invitations check failed, pending surface mid-send, controls not found, other limit pop-ups, crash, or a reservation whose outcome was never recorded | `uncertain` | no; check Sent invitations manually |

The outcome write retries once on a dropped DB connection. A reservation still
left `attempting` with no live Task is recorded as `uncertain` (with its notice)
at daemon startup and by the hourly reply catch-up; it is never resent.

Every hold and outcome posts to #rb2b-v2 with the person, company, URL and reason
(sent notices include the ICP and note).

## Send accounting and exclusions

Every reserved attempt counts against the sender's daily connection, weekly
connection and total daily budgets through its own ledger (`attempted_at`); no
fake Campaign, Deal or ActionLog is created. Failed and uncertain attempts keep
their budget. Campaign pacing math includes these attempts. Follow-ups, drips,
manual replies and reposts are not blocked by RB2B work beyond normal
single-worker sequencing.

The built-in canonical-URL digest exclusions cover two invitations independently
verified on 2026-10-30. Their identifying details and receipts stay in the private
handoff, not this public repository. Never clear these historical exclusions.
They were specifically approved weak-fit exceptions, not a qualification rule.

No CRM Sheet publisher, Active Accounts stage, customer record, campaign setting,
sequence, suppression, sender stop, or quota is changed by this workflow.

## Replies

The realtime listener posts replies as they arrive (24/7 by default) and is
required for sending. RB2B leads have no Deal, so the campaign `backfill_messages`
catch-up does not cover them; instead the Arian daemon runs an hourly
`rb2b_replies` Task during active hours. It pages back through the Voyager inbox
(no page navigation, no compose window; the same queries as the Messaging UI)
until it passes the start of the last complete scan, ten minutes overlapped; the
first scan starts at the oldest RB2B invitation. A reply that pushed an RB2B
thread onto an older page is therefore still found. The window advances only
after a *proven* complete pass: a conversation older than the window start was
reached (or an empty page after a full one), and every matched thread was read.
A page that only repeats seen conversations still follows its cursor. A GraphQL
error body or a page/thread response without its list raises, a repeated page
request or dead end stops without proof, and the 50-page cap (1,000
conversations) stops too; each of these keeps the old window, so the next hourly
run retries it. Matched 1:1 threads with new
activity are persisted through the normal message path, so a missed reply
reaches the replies channel once; overlap with the listener shares the same
message identity and notification ledger. Threads already stored for another
Lead and group threads are skipped.

Message times from these reads (and every other `parse_messages` caller) are
stored in UTC, like the realtime listener; earlier code used the host's local
time, which saved Toronto/Windows messages four to five hours early. Rows saved
before this fix keep their stored time.

## Known limits

- If more than 1,000 conversations change between two complete scans, the window
  never completes and older RB2B replies wait; the daemon logs a warning each run.
- Profiles that show no Connect control end as `uncertain`, not `failed`, because
  the connect path cannot prove no click happened.

## Validation

Run `scripts/qa_campaigns.py --suite rb2b-intake` with the repository virtualenv
and local PostgreSQL 17 (on macOS set `LC_ALL`, for example `en_US.UTF-8`). It
uses a disposable socket-only database and blocks all live delivery, network,
environment-file and credential access. Plain `pytest` in a checkout with a
`.env` uses that `DATABASE_URL` server for its test database; use the QA runner
instead. The broader campaign suite covers shared counters, task claims and
existing outbound behavior. Tests are not proof of a Windows rollout or a real
production invitation/reply.
