# Priority on normal campaigns

## Compatibility publication

The release now retains the deployed `api/rb2b_webhook.py` unchanged so publishing
campaign capability does not remove the current collection endpoint. Passive
historical reservation accounting and exact-member reply lookup/notification
delivery remain in `linkedin/legacy_rb2b.py`. That module cannot enqueue or send
invitations. Its outbox drain only handles historical `visitor:` and `reply:`
keys; the external integration owns its separate `event:` and `review:` notices.
Generic enrollment also refuses historical attempted/ready/terminal visitor
records even under a new source/reference. Retired connection Tasks remain
unclaimed and unchanged.

These compatibility measures permit code publication with existing campaigns
at their default priority and external enrollment disabled. They do not clear
the separate historical handover or authorize new RB2B enrollments. Keep the
replacement review paused until that gate, explicit campaign/version/audience
approval, receiver/callback configuration and runtime readiness are verified.
The older removal/pinning warnings below describe the pre-compatibility
revision; do not remove the retained endpoint or passive guards in a later
cleanup without a separately verified cutover.

Implementation prepared for publication and review. No campaign has been created, activated or
reconfigured, and no worker has been restarted. This worktree also contains the
selective reversal documented in `campaign-priority-clean-start.md`. Do not
deploy that combined reversal without the Windows receipt handover below.

## Scheduling contract

### Explicit connection-hours exception

External qualification and enrollment are independent of sender working hours.
For an explicitly approved campaign, set `CONNECT_OUTSIDE_HOURS_CAMPAIGN_IDS`
to a comma-separated list of its actual numeric IDs in that sender's worker
environment. The default is empty. Only normal connection Tasks for active,
owned, listed campaigns can run outside working hours and on rest days.
The recurring RB2B review does not change this setting or enable a campaign.

This exception does not enable follow-ups, Gmail, discovery, another sender or
another campaign outside its existing schedule. Existing catch-up behavior is
preserved. Daily/weekly/total limits, LinkedIn restrictions, sender stops,
campaign activity, frozen messages, due times, and normal pacing still apply.
An exhausted opted-in campaign rechecks those same guards at local midnight
(including DST), rather than waiting for the next workday. A quota is never
reset or increased by the exception. Preflight reports the configured IDs;
this is configuration evidence, not proof a remote worker has loaded them.

The intended RB2B rollout selects only Arian's campaign 58. Keep this setting
unset in the live environment until the historical handover, callbacks and
runtime readiness gates are cleared; source publication alone activates none
of these exceptions and requires no database migration.

`Campaign.connection_priority` is an integer from 0 to 32767. Existing campaigns
receive 0. Larger values get the next **due connection** turn ahead of smaller
values, within the same sender's eligible campaigns. Equal priorities retain
the former order. Priority neither changes a task's scheduled time nor makes a
future, disabled, finished or foreign-sender campaign eligible.

The scheduler first chooses the task lane exactly as before. Only if it selected
a normal `connect` task can another due campaign connection take that turn.
Manual replies, follow-ups and maintenance fairness retain their existing order.
Connections are reordered within the group ahead of the next due follow-up;
a later high-priority connection cannot jump across that follow-up and push it
back on a subsequent turn. It becomes eligible for priority after that boundary.
The normal connection handler still enforces hours, rests, restrictions, daily,
weekly and total limits, pacing, exclusions and frozen copy. Priority cannot
interrupt an in-progress task or reserve extra quota. A campaign with a steady
stream of higher-priority due work can delay lower-priority connections until
those higher-priority tasks are no longer due; it cannot displace follow-up turns.

The field is available in campaign admin and JSON import/export. Importing older
JSON without it preserves the existing value. A narrow CLI previews a change:

```
python manage.py set_campaign_priority CAMPAIGN_ID 10 --owner-username EXACT_USERNAME
```

Adding `--apply` changes only the priority. It does not activate the campaign or
retime pending work. These examples are documentation, not rollout authorization.

## External enrollment

The optional `campaign_enrollments submit --input FILE` command accepts a reviewed
identity and an exact existing campaign, owner, message version and audience. It
previews by default; `--apply` additionally requires
`ENABLE_CAMPAIGN_INTEGRATIONS=true`. No campaign is created and no caller-supplied
connection note or per-person priority is accepted. The ordinary campaign owns
copy, acceptance, LinkedIn/Gmail sequence timing and reply stopping.

```json
{
  "source": "example-integration",
  "reference_id": "person:123",
  "campaign_id": 123,
  "owner_username": "exact-owner-username",
  "message_version_id": 123,
  "linkedin_url": "https://www.linkedin.com/in/exact-person/",
  "first_name": "Example",
  "last_name": "Person",
  "company_name": "Example Company",
  "email": "",
  "icp": "exact-published-audience-key",
  "role_tag": "",
  "callback_url": "https://approved-receiver.example/completed"
}
```

One `(source, reference_id)` binds one immutable input and normal message
enrollment. An identical retry returns its current result; changed input under
that reference is held. A new reference for a person with campaign history is
also held. There is no automatic resubmission after failure or uncertainty.
Preview is read-only; apply locks the person and campaign, rechecks exclusions,
creates the normal Deal/enrollment and planned frozen connection delivery, and
appends that exact public ID to the campaign's seeds. It reuses the campaign's
pending/running connection task without changing its date. A disabled campaign
remains disabled. Existing identity conflicts, relationships, suppression,
conversations, opportunities, campaign history or pending person work cause holds.
Research remains the integration's responsibility; these database checks do not
establish company or role fit.

`CampaignSubmission` is an enrollment receipt and notification outbox, with no
invitation executor or Task type. Status is available through:

```
python manage.py campaign_enrollments status --source example-integration --reference-id person:123
```

## Completion callbacks

### Local outcome handoff

A trusted integration sharing the existing database can explicitly use the
local CLI transport instead of hosting an HTTPS callback. Submit an empty
`callback_url`, then run `campaign_enrollments outcomes --source SOURCE
--operator OPERATOR --apply`. This freezes the same normal completion receipt
and its actual persisted event UUID. No event is returned before final campaign
evidence exists. After committing that event into its own deduplicated outbox,
the integration calls `ack-outcomes` with the exact submission/event ID pairs
in `--input FILE`, plus the same source/operator and `--apply`.

Only local-destination receipts with matching source, operator and event UUID
can be acknowledged. Export is repeatable until acknowledged; acknowledgement
is idempotent. The HTTP worker skips empty destinations. Neither operation
changes a Lead, Deal, delivery, Task, invitation, sender budget or sequence.
Callback hosting/signing is required only for HTTPS transport. Local notification
latency follows the integration's review schedule; it does not block enrollment
while a sender's daemon is offline. Actual replies retain their existing path.

Callbacks are optional. Configure a secret of at least 32 characters and exact
hostnames in `OUTREACH_CALLBACK_ALLOWED_HOSTS`. The receiver must verify
`X-OpenOutreach-Signature: t=UNIX_SECONDS,v1=HMAC_SHA256` over `timestamp + '.' +
exact_body`, reject stale timestamps, and deduplicate the stable `event_id`.
Only HTTPS/443 destinations on the allowlist are accepted. Credentials, queries,
fragments, redirects and nonpublic DNS results are rejected; TLS validates the
original hostname while the connection uses the validated address.

An opt-in daemon thread observes normal campaign receipts and freezes one
`campaign.connection_completed` event after a final outcome. Temporary no-button
failures that the existing campaign retries are not terminal notifications.
Callback failures update only the outbox. Retries use a lease and exponential
backoff, stopping after 48 hours with a retained abandoned receipt. Event bodies
and IDs remain stable. Callback delivery is at least once; receivers must dedupe.
No callback action can reset an enrollment, delivery or invitation.

The daemon must be running for automatic delivery. An explicit separate
`campaign_enrollments callbacks --operator Arian --apply` command can drain the
same outbox. Existing campaigns do not produce these events unless enrolled by
this optional API. Actual inbound replies retain the existing replies path.

## Review and Windows handover gate

A portable smoke simulation is available in `scripts/smoke_campaign_priority.py`.
It uses fake visitors and an in-memory database, denies network/PostgreSQL/
subprocess access and environment-file reads, and writes per-case JSON receipts.
It can also test the separate integration via `--integration-dir PATH`.
`docs/windows-campaign-smoke-prompt.md` contains the one-time Windows prompt.
Run it in a review checkout; a green simulation is not a rollout approval.
Prepare the paused replacement workflow with
`docs/windows-campaign-workflow-update-prompt.md` first.

**Publication gate:** `daemon_supervisor.py` automatically pulls its upstream,
applies migrations and restarts workers unless launched with `--no-update`.
The existing Vercel project also auto-deploys `main` (confirmed by its production
deployment of 090ab5c5). Since this change removes the earlier receiver and
worker paths, those automatic cutover paths must be pinned before `main` is
published. A separate review branch/check-out can be used for the simulation.

The priority feature is additive, but this branch also removes the earlier
RB2B-only workers. Migrations 0038/0039 and their passive receipts remain exact.
Migration 0040 adds priority and 0041 adds submission receipts; **neither migrates
old RB2B work**. Do not mistake them for the abandoned connection-queue migration.

Before a separately authorized deployment, independently verify a stopped old
worker and a forward handover of attempted/sent/uncertain/ready rows, preserved
identities and member URNs, historical daily/weekly/total budget accounting and
pending outcome/reply notices. The external helper refuses historical attempted
rows and never rewrites those receipts. That guard alone does not replace the
budget/reply handover. The pending legacy-reply outbox must not be dropped or
misrouted to the outcomes channel.

The full forward handover is a release blocker, not an implemented migration.
Its live state cannot be verified under this task's no-live-database boundary.
The replacement review automation may be saved while paused; do not enable it
or update the live sender onto this release yet.

Tests use synthetic identities and blocked network/credentials. SQLite results
do not validate PostgreSQL locks or concurrent submissions; the private
PostgreSQL run remains required before release. The old review's `initdb` failed
on a 56-byte shared-memory allocation; do not change system settings or kill
unrelated applications to work around it.
