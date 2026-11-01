# Campaign priority: clean implementation baseline

The user wants an extensible campaign feature: when eligible leads are added to
a higher-priority campaign, its due connections receive the next available
connection slots ahead of lower-priority campaigns. RB2B is one external source
of those leads, not a special kind of outbound worker.

## Intended behavior

- Existing campaigns keep their behavior with no opt-in or configuration change.
- Default and equal priorities retain the prior scheduling order. Priority only
  chooses between otherwise eligible, due campaign connections for one sender.
- Sender ownership, active hours, rest days, daily/weekly/total limits, pacing,
  restrictions, follow-ups, replies and maintenance fairness remain intact.
- Leads use normal campaign enrollment, connection copy and optional sequences.
  Preserve frozen enrollment/message versions and the existing reply-stop rules.
- External integrations own intake, research, qualification and ICP selection.
  Generic, idempotent enrollment must retain the existing identity, suppression,
  conversation, customer/opportunity and conflicting-enrollment safeguards.
- Optional generic completion callbacks can report actual invitation outcomes
  to the integration, which formats Slack notifications. Durable callback retries
  must never retry outreach. Actual replies keep the existing replies channel.
- No second standalone connection queue, request table or duplicate sequence engine.

## Local reset performed

On `codex/campaign-priority-clean-start`, the uncommitted connection-only refactor
was saved in a named Git stash and verified private source archives. The changes
from the following commits were reversed locally, with the exceptions below:

- `090ab5c5`: the RB2B-specific worker queue.
- `178ba433`: the guarded IAB/direct-CRM RB2B pipeline.
- `ca9b8e93`: RB2B intake into the discovery inbox.

This did not rewrite Git history or change the remote branch. The external
`rb2b-integration` project was also archived and remains an uncommitted prototype;
its existing submit/adopt API targets the abandoned connection-only design and
must not be treated as the new campaign contract.

Preserved deliberately:

- `bd363a8d`, the unrelated LinkedIn reply-modal lead context improvement.
- UTC normalization in `linkedin/actions/conversations.py`.
- Already-published migrations 0038/0039, byte-for-byte.
- Passive historical `RB2BVisitor`/`RB2BNotification` schemas, legacy task choices
  and the heartbeat compatibility field. Their old execution paths are removed;
  legacy tasks are excluded from claims and scheduler wakeups, without mutation.
- Persisted `connect_restricted_on` and its generic restriction enforcement.
- All existing campaign copy, campaigns, contacts, send receipts and live settings.
  No live database was accessed or changed during this preparation.

The private recovery manifest and source archives are referenced by
`artifacts/qa/claude-rb2b-review/clean-start-latest.txt`. The uncommitted refactor's
stash is `fe019b14c062baa312ded1c335fbbfeb47ae045f`; retain it until the replacement
has been independently reviewed. The unfinished generic migration 0040 is in
that backup, not part of this clean baseline.

## Release boundary

This is an implementation starting point, **not deployable as-is**. Windows was
already running the previous RB2B release. Before any separately authorized
rollout, design and independently verify the old-worker drain and receipt
handover, including no-resend identities, reserved/attempting/uncertain/sent work,
old daily/weekly/total budget accounting, reply lookup and pending notifications.
Do not delete historical schemas or erase old work to make a migration pass.

The user subsequently authorized committing and pushing the replacement.
Publication must first avoid automatic cutover of Windows and the existing
Vercel receiver; see `campaign-priority.md`. That authorization does not complete
the forward handover or authorize live migrations, worker restarts,
campaign/sender changes or test sends.
Use credential-free isolated tests only. PostgreSQL initdb previously failed on
this Mac due to shared-memory exhaustion; SQLite diagnostics and skipped tests
are not PostgreSQL release verification. Do not change system settings or kill
unrelated processes to work around it.
