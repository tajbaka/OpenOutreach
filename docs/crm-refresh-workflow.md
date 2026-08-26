# Canonical CRM v2 refresh workflow

The production CRM is account-first and deliberately small. `People` remains
the complete, growing contact ledger. Sales work happens in exactly two managed
tabs:

| Surface | Purpose | Identity |
|---|---|---|
| `Active Accounts` | One row per currently relevant account/opportunity | Stable Account and Opportunity IDs |
| `Actions` | One owner-filterable queue of current work plus retained handled history | Stable Action, Opportunity, and target Lead IDs |

`Opportunities`, `Pipeline`, `Recovery`, and `<Owner> - Followups` are retired
legacy surfaces. They are not inputs to routine publication. Stage, waiting,
attention, ownership, and next work are fields on `Active Accounts`/`Actions`,
so duplicating them into more tabs only creates clutter.

No CRM command sends Gmail or LinkedIn messages.

## Two-phase production flow

Context ingestion and Sheet publication are separate on purpose.

1. `sync_crm_v2_context --apply` refreshes configured Gmail threads,
   Gmail-delivered Gemini notes, strictly validated corporate email-first
   contacts, and Granola meeting context. It writes DB/context state only.
2. `refresh_crm_v2 --apply --routine` reads stored evidence, imports human
   edits, reconciles Accounts/Opportunities/current Actions, preserves People,
   and atomically publishes the two v2 tabs.

Granola is primary meeting-note context when deterministically matched. Stored
Gemini is secondary. LinkedIn messages come from the daemon/realtime paths and
the separately scheduled `backfill_messages`; the CRM workflow never logs into
LinkedIn. Google Calendar and Drive-only notes use
[`data-sync-workflow.md`](data-sync-workflow.md).

## Admission and action rules

Accounts enter `Active Accounts` from the strongest qualifying evidence:

1. human manual pin, Sales Motion tab, or non-closed human-managed opportunity;
2. a real upcoming meeting or a completed meeting with matched context;
3. a human Gmail inbound/bidirectional thread; or
4. a substantive bidirectional LinkedIn conversation.

One-sided outbound never promotes an account out of People. Exact thread-level
message direction determines `Needs response` versus `Waiting`; unrelated
outbound in another thread cannot hide an inbound reply. An account without a
deterministic owner or target remains visible with attention required but does
not enter an outbound queue.

`Don't send`, disqualification, and company suppression are delivery controls,
not relevance erasers. They suppress outreach to the exact target while
preserving legitimate meeting/email/account history. Closed Won/Lost, owner,
stage, sales-motion step, commercial fields, manual pins, and genuine human
actions remain human-authoritative.

See [`crm-v2-contract.md`](crm-v2-contract.md) for the complete evidence and
field contract.

## Field ownership

Human-owned cells are imported by stable ID through conservative three-way
merges. These include owner, stage, motion step, contact roles, next step/due,
waiting, manual pin, value/probability, closure fields, draft/channel, handled,
and disposition. Concurrent DB and Sheet edits produce a conflict; the system
does not guess.

System-owned cells include stable IDs, resolved account/contact identity,
admission evidence, source timestamps, last meaningful touch, attention, who
owes, and sync revisions. Unknown columns, formulas, comments, formatting, and
operator cells outside managed ranges are preserved.

`People` is never cleared, rebuilt, reordered, pruned, or shrunk. Existing rows
are updated in place and new exact Leads append once. Stable Lead ID is
canonical identity; exact canonical LinkedIn URL is legacy bootstrap identity.

## Configuration

Store values in `.env`; never print or commit them.

| Name | Requirement | Role |
|---|---|---|
| `DATABASE_URL` | Required outside tests | Shared Postgres database; no runtime SQLite fallback |
| `GOOGLE_SHEETS_ID` | Required | CRM workbook write target |
| `GOOGLE_SHEETS_CREDENTIALS_PATH` | Required | Service-account JSON with Editor access |
| `GOOGLE_SHEETS_TAB_NAME` | Optional (`People`) | Durable People tab |
| `SALES_MOTION_VERSIONS_GOOGLE_SHEETS_ID` | Required for Sales Motion pins | Separate read-only workbook; never the CRM target |
| `GRANOLA_API_KEY` | Optional | Primary read-only meeting-note source |
| `GRANOLA_API_BASE` / `GRANOLA_HTTP_TIMEOUT_SECONDS` | Optional | Granola transport configuration |
| `ACTIVE_TIMEZONE` | Optional (`America/Toronto`) | Business date for due/waiting evaluation |

## Safe first cutover

Pause scheduled CRM writers. Take and verify a Postgres snapshot before schema
migration, then install dependencies and apply migrations. The automatic CRM
backup protects Google Sheets only; it is not a database backup.

Build a private preview using the exact inputs intended for apply:

```bash
.venv/bin/python manage.py sync_crm_v2_context --apply

.venv/bin/python manage.py preview_crm_v2 \
  --manual-pin StackArmor \
  --owner-override Ramp=Arian \
  --owner-override StackArmor=Arian \
  --output artifacts/crm-audits/crm-v2-reviewed.json

.venv/bin/python manage.py refresh_crm_v2 \
  --manual-pin StackArmor \
  --owner-override Ramp=Arian \
  --owner-override StackArmor=Arian
```

The preview is a mode-`0600` local artifact and may contain CRM identities; keep
it gitignored/private. The refresh dry-run executes the exact reconciliation and
action mutation path inside a rolled-back DB transaction and performs zero
Sheet writes. Review at least:

- active versus People-only counts and admission reasons;
- action count, unowned/untargeted work, and Don't send state;
- identity/reconciliation issues (must be zero);
- People append/update/error and duplicate-ID counts;
- human imports/conflicts/unkeyed rows;
- planned obsolete-tab count and first-cutover mode; and
- representative critical accounts such as Ramp and StackArmor in the private
  preview, without copying their underlying conversations into logs.

Apply only when the preview and dry-run are accepted and still current:

```bash
.venv/bin/python manage.py refresh_crm_v2 --apply \
  --reviewed-preview artifacts/crm-audits/crm-v2-reviewed.json \
  --manual-pin StackArmor \
  --owner-override Ramp=Arian \
  --owner-override StackArmor=Arian
```

The command recomputes the evidence universe and refuses a stale, public,
structurally invalid, or semantically different preview. Before the first Sheet
write it creates a full private workbook backup under
`artifacts/crm-backups/`. Both v2 tabs are staged and verified before one atomic
title cutover. Exact legacy canonical titles are removed only after DB commit;
unresolved material legacy rows may be retained under explicit archive titles
for review. A DB failure after title activation triggers title compensation.

After cutover, verify routine behavior:

```bash
.venv/bin/python manage.py refresh_crm_v2 \
  --manual-pin StackArmor \
  --owner-override Ramp=Arian \
  --owner-override StackArmor=Arian

.venv/bin/python manage.py refresh_crm_v2 --apply --routine \
  --manual-pin StackArmor \
  --owner-override Ramp=Arian \
  --owner-override StackArmor=Arian
```

Routine mode fails closed unless both `Active Accounts` and `Actions` exist and
all exact legacy canonical titles are absent. Routine publication stages and
verifies both replacements before one atomic swap; it never updates one live
surface without the other.

## Windows scheduled runner

Keep the existing Task Scheduler action path:

```text
scripts\run_sync_sheets.ps1
```

The filename preserves the deployed task identity, but the wrapper now runs:

```text
.venv\Scripts\python.exe manage.py sync_crm_v2_context --apply
.venv\Scripts\python.exe manage.py refresh_crm_v2 --apply --routine --manual-pin StackArmor --owner-override Ramp=Arian --owner-override StackArmor=Arian
```

It logs to `data\logs\crm_v2_task.log`. Each run has a unique run ID and must
record successful completion of both phases followed by:

```text
finished crm_v2_workflow run_id=<id> exit_code=0
```

The Scheduled Task may retain the historical name `OpenOutreach Sync Sheets`.
Locate it by the unchanged action path when machine-specific names differ:

```powershell
$Task = Get-ScheduledTask | Where-Object {
    $_.Actions.Arguments -like "*run_sync_sheets.ps1*"
} | Select-Object -First 1
if (-not $Task) { throw "Task using run_sync_sheets.ps1 was not found" }

Start-ScheduledTask -InputObject $Task
$Deadline = (Get-Date).AddMinutes(30)
do {
    Start-Sleep -Seconds 10
    $Task = Get-ScheduledTask -TaskName $Task.TaskName -TaskPath $Task.TaskPath
} while ($Task.State -eq "Running" -and (Get-Date) -lt $Deadline)
if ($Task.State -eq "Running") { throw "CRM v2 task timed out" }

$TaskInfo = Get-ScheduledTaskInfo -InputObject $Task
if ($TaskInfo.LastTaskResult -ne 0) {
    throw "CRM v2 task failed with result $($TaskInfo.LastTaskResult)"
}
Get-Content -LiteralPath "data\logs\crm_v2_task.log" -Tail 100
```

`notify_sync_sheets_health` intentionally retains its historical command/task
name, but reads the v2 log. It reports healthy only when the newest wrapper run
completed both context and refresh phases with exit code zero.

## Recovery

On any apply failure:

1. Stop the scheduled task and do not repeat apply blindly.
2. Preserve the printed private preview, full workbook backup, and v2 task log
   locally and out of git.
3. Run no-write `refresh_crm_v2` with the same pins/overrides to identify the
   exact conflict, identity issue, invalid human edit, or provider failure.
4. Prefer Google Sheets version history for deliberate workbook restoration.
   Do not delete/recreate tabs or bulk-write the backup into the live workbook.
5. Confirm the database transaction rolled back. If title compensation was
   reported incomplete, inspect the retained `_CRM v2 failed ...` tabs before
   any manual title repair.
6. Rebuild a fresh preview if evidence changed, then repeat dry-run/apply once.

The pre-migration Postgres snapshot is a separate last-resort recovery point.
Restoring it requires stopped writers and a validated target; never restore the
database merely to undo a Sheet-only issue.

## Retired and scoped commands

`refresh_crm` is the legacy multi-surface publisher. It is not scheduled and
refuses to run once either v2 canonical tab exists. Do not bypass that guard.

`sync_sheets` remains available only for a narrow People diagnostic:

```bash
.venv/bin/python manage.py sync_sheets --dry-run
```

It does not decide account admission, synthesize sales state, or publish
`Active Accounts`/`Actions`.
