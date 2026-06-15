# Gmail Fallback Sequence Plan

## Goal

After a lead completes the full LinkedIn follow-up sequence with no reply, move
that lead into a Gmail sequence. If the lead replies on LinkedIn or Gmail at any
point, stop both automation lanes.

This is a post-LinkedIn fallback, not a Slack-triggered enrichment workflow.
BetterContact email enrichment is background plumbing for Gmail sequencing.

Gmail is implemented as a modular top-level `gmail/` package, not as more
LinkedIn daemon logic. The LinkedIn side only emits an explicit handoff task
after its final no-reply step. Everything after that belongs to the
Gmail/enrichment lane.

## Core Rules

- LinkedIn remains the first channel.
- Gmail starts only after the final LinkedIn follow-up step completes with no
  reply or meeting.
- If `Lead.email` exists, the Gmail lane can enqueue step 0.
- If `Lead.email` is missing, enqueue email enrichment first.
- BetterContact email enrichment must be email-only:
  - `enrich_email_address=true`
  - `enrich_phone_number=false`
- Phone enrichment remains unchanged and Slack-triggered.
- Any inbound LinkedIn or Gmail reply stops both LinkedIn and Gmail automation.
- A meeting, suppression match, disqualified lead, or non-active Deal state also
  stops automation.
- Gmail tasks must never share the LinkedIn browser quota or LinkedIn
  `daemon-send:` idempotency namespace.
- Gmail should not require the LinkedIn Playwright browser, LinkedIn active-hour
  loop, LinkedIn ActionLog quota, or LinkedIn task handlers.

## Existing Foundation

- `Lead.email` already exists and is mirrored to Sheets.
- `crm.Message` already stores both LinkedIn and Gmail messages.
- `linkedin.notifications.gmail_threads.classify_ball_on_court(lead)` reads the
  merged LinkedIn + Gmail timeline.
- LinkedIn follow-up already has multi-step sequencing, active-hours scheduling,
  per-step idempotency, and DB-local stop checks. The current stop helper is
  `linkedin.tasks.follow_up._sequence_stop_reason`; the Gmail work should
  extract/reuse that behavior rather than duplicate channel-specific checks.
- Active-hours scheduling should reuse the existing
  `linkedin.tasks.follow_up._delay_seconds_to_active_due` helper unless Gmail
  gets its own schedule.
- BetterContact is already integrated for phone enrichment, but the current
  provider submits `enrich_email_address=false`.

Implemented Gmail foundation:

- Gmail API OAuth credentials/token storage in `data/gmail/`.
- Gmail send/search client in `gmail/client.py`.
- Gmail `sendAs.list` alias verification before send.
- Gmail reply sync before send by searching threads for the lead email and
  persisting them through `linkedin.notifications.gmail_threads`.

## Proposed Task Types

Add these task types:

- `enrich_email`
- `gmail_follow_up`

`enrich_email` is handled by the HTTP-only enrichment worker. It should not touch
the browser. `gmail_follow_up` is handled by the Gmail sender lane.

The current lifecycle shape is intentionally simple: the main daemon starts a
browserless `gmail.worker.GmailWorker` background thread alongside the
enrichment worker. The LinkedIn outbound loop excludes `gmail_follow_up`, so the
browser lane never claims Gmail sends. A future separate process is still
possible, but it is not required for the current volume or safety model.

## Handoff From LinkedIn

At the final successful LinkedIn follow-up step:

1. Run the shared stop check.
2. If stopped, do nothing.
3. If Gmail fallback is disabled, do nothing.
4. If `Lead.email` exists, enqueue `gmail_follow_up` step 0.
5. If `Lead.email` is blank, enqueue `enrich_email`.

This should happen explicitly in the final-step success path. Do not infer Gmail
eligibility later from `Deal.state=COMPLETED`, because `COMPLETED` currently
means LinkedIn automation finished, not all outreach finished.

This handoff is the only LinkedIn code path that should know Gmail exists. It
should be a small helper call that creates a durable task. The Gmail worker owns
email enrichment, Gmail send attempts, Gmail sequence state, and Gmail retry
behavior.

Expected LinkedIn edit:

```python
maybe_handoff_to_gmail(deal=deal, operator=our_operator)
```

That call belongs in the final LinkedIn follow-up success path. The helper should
live in the Gmail module. `linkedin/tasks/follow_up.py` should not contain Gmail
send logic, Gmail template logic, BetterContact email parsing, or Gmail retry
logic.

## Shared Stop Check

Both LinkedIn and Gmail send paths should call one shared DB-local helper before
sending:

- lead is disqualified
- suppression matches
- meeting exists
- inbound LinkedIn message exists
- inbound Gmail message exists
- Deal is not in the expected active state for that lane

This helper must not read Google Sheets. Sheets remains a sync target, not a send
path dependency.

## Email Enrichment

`enrich_email` should:

1. Load the lead.
2. Skip if the lead is missing or disqualified.
3. Skip if `Lead.email` already exists.
4. Skip if BetterContact already gave a definitive email result for this lead.
5. Submit BetterContact with email-only enrichment.
6. Poll until completion or timeout.
7. On found email:
   - save normalized email to `Lead.email`
   - record BetterContact as tried
   - enqueue `gmail_follow_up` step 0
8. On not found:
   - record BetterContact as tried
   - do not enqueue Gmail
9. On API failure:
   - do not record tried
   - allow retry

Add a separate email-attempt tracking field rather than reusing
`phone_providers_tried`.

This is not just flipping BetterContact's existing boolean. The current
BetterContact parser is phone-only and reads `contact_phone_number`. Email
enrichment needs a separate parse path for BetterContact's email field, separate
FOUND/NOT_FOUND semantics, and worker claim logic that includes `enrich_email`
while keeping the main outbound task loop from claiming it.

## Gmail Sending

`gmail_follow_up` should:

- send from the configured Gmail account or alias for the operator
- persist outbound messages as:
  - `source=gmail`
  - `direction=outbound`
  - `sender=<operator or sender email>`
- use a distinct idempotency namespace:
  - `gmail-send:<operator>:<lead_id>:<sequence_name>:step-<index>:<timestamp>`
- dedupe per `(operator, lead, sequence_name, step_index)`
- schedule the next step only after successful persistence
- retry failed sends without double-sending

Gmail quotas and pacing should be separate from LinkedIn quotas.

The Gmail worker should be browserless. It should use Gmail APIs or another
Gmail-specific send mechanism, not the LinkedIn Playwright session.

## Gmail API And Auth

The Gmail worker needs:

- Google OAuth client configuration.
- Refresh-token storage for each Gmail account key:
  - `arian_boundera`
  - `eddy_boundera`
- Gmail API client for sending messages.
- Gmail API client for `users.settings.sendAs.list`.
- A startup or pre-send validation step that proves each configured `send_as`
  alias exists and is usable for the selected credential.
- Clear failure behavior when credentials are missing, expired, or missing the
  requested alias.

No send should fall back to the Gmail account's default address.

## Gmail Accounts And Send-As Aliases

Gmail sender selection must be explicit. The worker should not inspect available
Gmail credentials and guess which alias belongs to which operator.

Initial mapping:

```json
{
  "Arian": {
    "gmail_account": "arian_boundera",
    "send_as": "ariant@boundera.io"
  },
  "Athena": {
    "gmail_account": "eddy_boundera",
    "send_as": "athena@boundera.io"
  },
  "Leili": {
    "gmail_account": "arian_boundera",
    "send_as": "leili@boundera.io"
  },
  "Eddy": {
    "gmail_account": "eddy_boundera",
    "send_as": "eddy@boundera.io"
  }
}
```

`arian_boundera` is one Gmail credential/token that has the Arian and Leili
`send_as` aliases configured in Gmail. `eddy_boundera` is Eddy's separate Gmail
credential/token and carries the Eddy and Athena aliases.

Before sending, the Gmail worker should:

1. Resolve the operator through the explicit mapping.
2. Load the mapped Gmail credential.
3. Call Gmail `sendAs.list`.
4. Verify the mapped `send_as` alias exists and is usable.
5. Send with that alias as the `From` address.
6. Fail loudly if the alias is missing instead of falling back to the mailbox
   default address.

## Modularity Boundary

Keep the boundary strict:

- LinkedIn daemon responsibility:
  - finish LinkedIn follow-up sequence
  - run the shared stop check
  - enqueue one Gmail handoff task when eligible
- Gmail worker responsibility:
  - decide whether email enrichment is needed
  - run BetterContact email enrichment
  - send Gmail steps
  - schedule Gmail next steps
  - persist Gmail messages
  - stop on any LinkedIn/Gmail reply
- Shared modules:
  - `crm.Message`
  - `Lead.email`
  - suppression checks
  - meeting checks
  - shared stop helper
  - task table

Avoid putting Gmail send logic, Gmail templates, BetterContact email state, or
Gmail retry behavior inside `linkedin/tasks/follow_up.py`.

The one allowed `follow_up.py` change is the final-step handoff call into a Gmail
module.

## Gmail Sequence Config

Email templates live in `gmail/icp_emails.json`, separate from
`linkedin/icp_messages.json`. Add email templates by sender and ICP. Each ICP
maps directly to the ordered email sequence:

```json
{
  "Athena": {
    "CSPs": [
      {
        "delay_days": 0,
        "subject_variants": ["Subject A", "Subject B"],
        "body_variants": ["Body A", "Body B"]
      },
      {
        "delay_days": 4,
        "subject_variants": ["Second subject"],
        "body_variants": ["Second body"]
      }
    ]
  }
}
```

Variant selection should be stable by lead id, as LinkedIn templates are today.
`sequence_name` remains in task payloads and idempotency keys for compatibility,
but it is not a JSON nesting level.

## Rollout Phases

### Phase 1: Handoff Skeleton And Worker Boundary

- Add `ENABLE_GMAIL_SEQUENCE=false`.
- Add `enrich_email` and `gmail_follow_up` task types.
- Add the post-final-LinkedIn handoff helper.
- Keep Gmail sending disabled.
- Add a Gmail worker module/process skeleton that claims only Gmail/enrich-email
  tasks. It can be wired into `daemon_supervisor.py`, but should not depend on
  the LinkedIn browser session.
- Add tests proving final LinkedIn success enqueues the correct next task or no
  task when stopped.

### Phase 2: BetterContact Email Enrichment

- Add BetterContact email-only mode.
- Add `enrich_email` handler.
- Save found email to `Lead.email`.
- Enqueue Gmail step 0 only after email is found.
- Track email provider attempts to avoid repeat billing.

### Phase 3: Gmail API Auth And Reply Sync

- Add Gmail OAuth credential/token support.
- Add configured mailbox/alias validation using `sendAs.list`.
- Add automatic Gmail reply sync into `crm.Message`.
- Ensure reply sync runs frequently enough that stop checks are trustworthy
  before any real Gmail sequence sends.
- Slack-notify inbound Gmail replies if desired, but the hard requirement is
  persistence into `crm.Message`.

### Phase 4: Gmail Send Plumbing

- Add Gmail sender abstraction.
- Persist outbound Gmail messages to `crm.Message`.
- Add idempotency and retry behavior.
- Keep Gmail sequence disabled by default until QA.

### Phase 5: Gmail Multi-Step Sequences

- Add Gmail sequence template loading.
- Add per-step scheduling into active hours/rest days.
- Stop on any reply across LinkedIn or Gmail.
- Mark Gmail lane completed only after final Gmail step.

### Phase 6: Gmail Slack Reply Actions

- Optional: support "Reply from Slack" for Gmail, using the same durable queue
  pattern as LinkedIn manual replies.

## QA Checklist

- Final LinkedIn step with an existing email enqueues `gmail_follow_up` step 0.
- Final LinkedIn step with no email enqueues `enrich_email`.
- Any inbound LinkedIn message prevents Gmail handoff.
- Any inbound Gmail message prevents Gmail handoff.
- A Gmail reply stops pending LinkedIn follow-up steps.
- A LinkedIn reply stops pending Gmail follow-up steps.
- BetterContact found email saves `Lead.email` and enqueues Gmail step 0.
- BetterContact not found does not enqueue Gmail.
- BetterContact API failure is retryable and does not mark the provider tried.
- Gmail reply sync persists inbound Gmail before any pending Gmail next step
  sends.
- Gmail send retry cannot double-send the same step.
- Gmail sequence tasks do not affect LinkedIn quotas.

## Open Decisions

- Whether Gmail should use the same active-hours window as LinkedIn or a separate
  Gmail-specific schedule.
- Whether email enrichment should post ops Slack notifications for not-found and
  API-failure outcomes.
- Whether Gmail fallback should be enabled per campaign, per operator, or
  globally.
- If a LinkedIn operator has no Gmail mapping entry, whether the handoff should
  skip quietly with an audit reason or fail loudly.
