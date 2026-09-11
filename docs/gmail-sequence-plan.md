# Gmail sequence start modes

This is the current operating spec for Gmail sequencing in OpenOutreach.

## Trigger

Campaign creation explicitly asks whether email should start before or after
LinkedIn acceptance. `Campaign.gmail_start_mode` records that answer:

- `post_acceptance`: preserve today's acceptance-triggered behavior. The migration
  assigns this to existing campaigns, without changing their Tasks or deliveries.
- `invitation_sent`: new, version-bound campaigns may start the email clock after
  their exact sender's invitation is confirmed and durably recorded. An observed
  pending invitation, failed attempt or unclear send is not sufficient evidence.

New creation/import requires an explicit mode and exact sender owner. Interactive
onboarding and Admin offer no default selection; noninteractive import rejects
a missing choice. Historical campaigns cannot be converted to invitation mode,
and mode/owner edits are refused after enrollment/outreach starts.

The scheduling hook is `gmail.handoff.maybe_schedule_gmail_sequence`, called from:

- `linkedin/tasks/connect.py`
- `linkedin/tasks/sweep_connections.py`
- `linkedin/management/commands/enqueue_no_reply_followups.py`
- A bounded startup recovery pass for exact opted-in confirmed invitations whose
  initial Gmail work is missing, even when LinkedIn follow-ups are disabled.

If `ENABLE_GMAIL_SEQUENCE=false`, the hook no-ops.
Gmail failures are logged at the existing best-effort LinkedIn boundary. This
does not claim success; the skip/failure reason must be reviewed.

## Timeline

The first Gmail step uses the selected start event plus its frozen
`delay_hours`: `Deal.connected_at` for post-acceptance, the stable
`Deal.invitation_sent_at` for invitation mode. Legacy connected records lacking
a connection timestamp retain their existing fallback; invitation mode never
invents a receipt or substitutes the restart/acceptance time.

Later Gmail steps remain anchored to the previous successful Gmail send.
LinkedIn follow-ups still wait for acceptance, with no change to their cadence.

Example:

```json
[
  {"delay_hours": 0.33, "subject": "FedRAMP 20x path at {company_name}", "body": "..."},
  {"delay_hours": 192, "subject": "Worth comparing 20x vs Rev 5?", "body": "..."}
]
```

That means Gmail step 0 can run about 20 minutes after connection acceptance even if
the next LinkedIn step is not due until later.
These are existing copy delays, not automatic approval for invitation-start
cadence. Review the frozen timing and copy before enabling a new campaign.

## Cadence Policy

The default post-accept cadence is:

- Day 0: LinkedIn follow-up 1, immediately after connection acceptance.
- +0.33 hours: Gmail follow-up 1, a same-day cross-channel touch without
  making the copy depend on LinkedIn having sent successfully.
- Day 4: LinkedIn follow-up 2, staying inside the first business week.
- Eight days after Gmail follow-up 1: Gmail follow-up 2.

Future LinkedIn steps added from Sheets default to 96-hour spacing. Future
Gmail steps default to 0.33h, 192h, then weekly after that. This keeps the
first email close to the first LinkedIn follow-up while preserving independent,
standalone copy in case either lane fails open.

Acceptance never restarts an invitation-start email sequence. Repeated hooks
reuse the frozen delivery and its existing Task; terminal/interrupted sends are
held, not retried by an acceptance or startup hook. Missing-hook recovery does
not backfill existing post-acceptance campaigns or shorten pending work.
Each bounded recovery pass records its sender/campaign scope, last Deal ID and
scanned/scheduled counts in `WorkflowRun(name="invitation-gmail-recovery")`.
The cursor rotates past held rows and wraps, so early missing-copy rows cannot
permanently block later invitations. Completed/failed lookups are holds, not
scheduled-work successes.

Gmail rechecks both the Task and frozen delivery due times at the submission
boundary. An early claim defers the same Task. Invitation mode additionally
honors the configured active-hours/rest-day window at this boundary; legacy
campaign windows are not retroactively changed. The Gmail worker preserves
pending deferrals rather than incorrectly completing them. This is not a new
daily cap, rate limiter or cross-channel contact-spacing engine.

## Email lookup

The daemon's HTTP-only `EnrichmentWorker` consumes `enrich_email`; the independent
account-scoped `GmailWorker` consumes Gmail sends, not lookups. Invitation mode
queues a missing-address lookup immediately after the invitation while retaining
the later frozen email due time. Existing post-acceptance lookup timing remains.
Each enrichment worker claims only its sender's email Tasks; legacy phone Tasks
remain shared. Due rows are atomically claimed with row locks, and startup only
recovers age-qualified stale work in that scope, retaining provider request IDs.
Fresh work and another sender's email are not reclaimed on a restart.

Provider success is checked for one syntactically valid mailbox, not verified
deliverability. A missing, invalid or exhausted result holds only email and
records a reason. Repeated hooks do not repeatedly purchase the same lookup.
An operator-supplied address can subsequently allow an unsent planned delivery to
proceed, without restarting its clock. A failed/unclear email submission itself
is a different condition and is never reset by supplying an address.

Recheck stops after a potentially long lookup before using the result. An early
result cannot send before the frozen due time; a late result enters the existing
sequence under the normal final send checks.

## Fail-Open Behavior

Channels are intentionally independent:

- If Gmail enrichment or send fails, LinkedIn follow-up tasks still run.
- If a LinkedIn follow-up send fails and retries, Gmail tasks can still run.
- If Gmail scheduling itself fails while LinkedIn is processing an accept, the
  exception is logged and swallowed; the LinkedIn path continues unchanged.
- Both lanes stop when the lead replies on LinkedIn or Gmail, gets a meeting,
  is disqualified, or matches suppression.

These are persisted-data checks. Gmail replies are known after context ingestion,
and LinkedIn replies after listener/backfill persistence, not instantaneously
at the remote provider. Preserve submission markers and exact provider receipts:
confirmed-send recovery only heals bookkeeping; ambiguous submission stays held.

Because of this, every LinkedIn and Gmail template must stand alone. Avoid copy
that depends on another channel having succeeded, such as "as I mentioned in my
email" or "following up on my LinkedIn note." Prefer local wording like "quick
follow-up on FedRAMP 20x" or "curious how your team is thinking about 20x vs
Rev 5."

## Templates

LinkedIn copy lives in `linkedin/icp_messages.json`.

Gmail copy lives in `gmail/icp_emails.json`.

The General ICP Messages Sheet is authoring state only. An explicit
`sync_general_icp_messages --apply` imports it into the two checked-in JSON stores.
Campaign creation snapshots a program version; enrollment freezes the manually
assigned ICP and sender; delivery freezes the rendered copy. Version-bound
execution never rereads Sheets or JSON. Invitation mode requires this path.

Only unbound legacy campaigns use `sender_icps` and `resolve_icp()` fallback.
Do not select an audience from role, company size, stage or revenue intent at runtime.

Allowed Gmail placeholders are:

- `{first_name}`
- `{last_name}`
- `{company_name}`
- `{role}`
- `{my_name}`
- `{our_company_name}`
- `{our_website_url}`

`steps()` validates subject/body placeholders against that allowlist before any
email is rendered. `render_for_icp()` also sanitizes unknown company sentinels
such as `Unknown Company` to `your team`.

`{role}` is wording from the reviewed `Lead.role_tag`, through
`linkedin/message_roles.py`: `CFO/Finance` becomes `finance leaders`; missing/blank
becomes `companies`; unknown nonblank tags fail when referenced. Independent drip
uses the same lookup. Neither role edits nor lookup changes rewrite frozen copy.

Keep the desired email subjects/bodies populated for either start mode. Blank
email copy disables that lane; it does not select pre-acceptance email. The wide
General authoring sheet has the existing eight-column complete-sequence schema.
Hidden sender-tab sync remains only the unbound legacy rollback workflow.

Before running a newly pulled Gmail sequence, validate the JSON:

```bash
.venv/bin/python manage.py validate_gmail_templates
```

## Sender Coverage

Only operators with a Gmail mapping in `gmail/auth.py` can schedule Gmail sends.
Operators without a mapping are skipped cleanly.

Missing, blank, or incomplete Gmail copy for a sender/ICP disables only that
Gmail lane. It must not alter LinkedIn state, LinkedIn scheduling, or LinkedIn
send behavior.

Current Gmail templates are populated for Arian, Athena, Leili, Chuka, and Eddy.
Chuka sends through the `eddy_boundera` Gmail account as `eddy@getboundera.com`.

See [implementation and QA plan](connection-first-omnichannel-v1-plan.md) and
[review-gated rollout](connection-first-omnichannel-v1-rollout.md). No production
rollout is implied by this specification or a passing isolated QA run.
