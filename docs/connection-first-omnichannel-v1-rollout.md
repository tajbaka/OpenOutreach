# Connection-first v1: review and rollout packet

This is a no-send handoff on `codex/connection-first-omnichannel-v1`, based on
`b7ea7952`. The user subsequently authorized pushing the finished code and
testing the sender restart flags. Feature-branch publication is the current
release step; main/live rollout is held for coordination of the one-time full
supervisor bootstrap described below. This restart-control test does not
authorize creating or activating a new omnichannel campaign. This document is
an operating reference, not independent authorization for live actions.

## Review inputs and disabled pilot template

Ask: **Should this campaign's email sequence start before or after LinkedIn
acceptance?** Record `invitation_sent` or `post_acceptance`; never infer a default.
Review the exact sender's `User.username` and matching active LinkedIn profile,
consenting recipients and their history/stops, immutable program version, explicit
ICP/role assignments, rendered messages, delays, sender window and staggered cap.

The current first-email delay is about 20 minutes. The proposed 24-hour pilot
delay is not approved configuration. No copy or delays changed in implementation;
any change requires reviewed publication before enrollment, not editing frozen work.

`examples/connection-first-pilot.template.json` deliberately contains
`REVIEW_REQUIRED` values. Replace them with the user's explicit choices; import
rejects the unfinished template. Its empty seeds are intentional, not an approved
recipient manifest. After review, save a private exact definition and preview:

```bash
.venv/bin/python manage.py import_campaign artifacts/qa/<reviewed-pilot>.json --dry-run
```

Require the preview's owner, mode, program and `status=disabled` to match the
review. New invitation-mode imports default disabled; specify it explicitly.
Import without `--dry-run` publishes/writes and requires separate authority.
Import alone does not enroll leads or create Tasks.

Preserve Campaigns 44/45 and their existing sender ownership, schedules, copy
and statuses. Never reuse historical pilot apply/launch scripts for this feature;
they are bound to different IDs, cohorts and activation assumptions.

## Code review and isolated QA

1. Review the feature-branch diff, including untracked files, before committing.
2. Run `.venv/bin/python scripts/qa_campaigns.py`: private socket-only PostgreSQL,
   blocked providers, no real credentials or shared database.
3. Keep `README.md`, `case_results.csv`, `message_previews.csv`, `report.json` and
   `run.json`. The receipt fingerprints the tested source and imported JSON; both
   must stay unchanged during the run. Re-run after code changes.
4. Inspect scenario coverage, not just totals. All ICP/sender variants are tested
   offline; the live pilot covers behavior, not one recipient per ICP.
5. Record the final run and working-tree fingerprint in the implementation plan.
6. Inspect recovery evidence: sender/campaign-scoped `invitation-gmail-recovery`
   workflow runs expose scanned/scheduled counts and the rotating cursor. A
   completed failed-address lookup is a hold, not a successful enqueue. Email
   enrichment claims must stay sender-scoped and fresh running lookups must
   survive another daemon's startup.

## Migration and deployment gate

Only after user approval:

1. Choose a controlled rollout window and verify no same-sender competing worker.
   With approval, stop the selected sender's supervisor before replacing code,
   so running workers cannot import a mixed version or use the new field before
   migration. Leave unrelated workers untouched.
2. Deploy the exact reviewed code/dependencies to the sender machines. Review
   `linkedin.0031_campaign_gmail_start_mode`; isolated migration QA verifies
   existing post-acceptance values and frozen work survive the schema change.
   This branch additionally includes `0032_linkedinprofile_restart_requested`;
   review its false-default control field and [supervisor bootstrap](sender-supervisor-restart.md).
3. Apply the approved shared-DB migration separately from activation. The column
   must exist before new code runs; daemon auto-migration is not substitute approval.
4. Create the exact disabled pilot, explicitly enroll only approved recipients
   through the existing frozen-version workflow, then verify every render and
   date before activation. Verify Gmail flag, account/auth, separate Gmail and
   enrichment worker pickup, and reply-ingestion freshness on each machine.
   Enrichment row claims prevent concurrent duplicate execution, but do not
   guarantee exactly-once provider billing if a process dies after submitting a
   lookup and before saving its request ID. Review that gap before retrying an
   uncertain lookup; stale recovery also assumes an age-qualified Task is no
   longer actively running.
5. Activate only the approved pilot and restart only its existing sender worker
   under the normal supervisor if needed. No competing local daemon or implicit
   auto-update/merge from this branch.

## Live proof and monitoring

Follow section 7 of the [implementation plan](connection-first-omnichannel-v1-plan.md):
email while unaccepted, post-acceptance control, acceptance without restart,
LinkedIn and Gmail reply stops, and correctly timed second messages. Use real
consenting, initially unconnected recipients. Do not duplicate one person or
remove/re-add connections to manufacture evidence.

Record exact campaign/Deal/enrollment/delivery/Task IDs, frozen copy, expected and
observed times, provider/thread receipts and recipient confirmation. Verify lookup
pickup separately. Heartbeats prove liveness only; stale reply ingestion is a
stop-for-review condition. Inbox placement is observed, not guaranteed by QA.

Review each pilot address before enrollment and monitor its sender mailbox for
bounces as well as replies. Syntactic validation and a provider's FOUND result
do not verify deliverability. An inactive mailbox/hard bounce is a stop-for-review
event: with authority, pause the affected pilot, record the bad address through
the existing suppression process and review any replacement before retrying.
This feature does not add automatic bounce classification or automatic resend.

Normal timing may require several days. Shorter internal-QA delays need separate
approval and do not prove production cadence. Mark unobserved branches pending.
Use the app's recurring monitor only when requested.

## Rollback / incident hold

With explicit authority to pause the affected pilot:

1. Disable the exact new Campaign and independently read back its status. Do not
   merely change `gmail_start_mode`; edits are refused after enrollment and frozen
   Tasks retain their identity regardless.
2. Enqueue/final-send checks require active status. Verify outstanding Tasks cannot
   submit; do not delete them, reset dates or rewrite copy.
3. A provider submission may already have happened. Pause its sender when
   authorized and reconcile the actual thread/receipt; never automatically retry
   an ambiguous send or claim a pause recalls a submitted message.
4. If rolling back code, keep this pilot disabled and preserve the additive
   column/history. Do not reverse-migrate away the mode or run old code against
   an active invitation-start campaign. Leave other campaigns unchanged.
5. Record incident evidence and require reviewed remediation before resuming.
