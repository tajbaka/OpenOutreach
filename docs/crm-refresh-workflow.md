# Canonical CRM v2 refresh workflow

The production CRM is account-first and deliberately small. The three working
tabs have intentionally different ownership:

| Surface | Purpose | Identity |
|---|---|---|
| `Active Accounts` | Operator-owned Attio-style company/stage ledger; never automation-written | Hidden Account and Opportunity IDs when bound |
| `Actions` | Generated owner-filterable queue of current work plus retained handled history | Stable Action, Opportunity, and target Lead IDs |
| `People` | Generated contacts only for companies listed in Active Accounts; Notes/Priority/custom columns are manual | Canonical LinkedIn URL, then unique email fallback |

`Opportunities`, `Pipeline`, `Recovery`, and `<Owner> - Followups` are retired
legacy surfaces. They are not publication targets. Active Accounts is the
manual pipeline tracker; Actions is the programmatic safety net.

No CRM command sends Gmail or LinkedIn messages.

## Two-phase production flow

Context ingestion and Sheet publication are separate on purpose.

1. `sync_crm_v2_context --apply` refreshes configured Gmail threads,
   strictly validated corporate email-first contacts, and Granola meeting
   context. It writes DB/context state only. Scheduled ingestion skips Gemini
   note-email fetching; previously stored Gemini notes remain available to CRM.
   The standalone `sync_gmail_context` command retains manual note import support.
2. `refresh_crm_v2 --apply --routine` reads stored evidence plus the exact
   bound Opportunity scope and Owner/Stage edits from manual Active Accounts, reconciles
   Accounts/Opportunities/current Actions, and atomically publishes only
   Actions without reading or writing People.
3. `sync_active_account_people --apply` reads the manual Active Accounts rows,
   selects exact company matches plus explicit Account-ID Opportunity contacts,
   and atomically rebuilds People. It removes contacts when their company leaves
   Active Accounts and never sends or creates outreach.
4. `sync_linkedin_pending --apply` reads persisted LinkedIn messages and
   incrementally reconciles the separate `LinkedIn Pending` tab. It considers
   only the last 14 days, exact unambiguous conversation ownership, and
   deterministic questions/requests/interest; later replies and aged-out items
   are deleted, changed items update in place, and new items append.

Meeting-note context is ordered by recording time when deterministically
matched. Granola wins only a same-recording tie; a newer stored Gemini note
wins over an older Granola note. LinkedIn messages come from the daemon/realtime paths and
the separately scheduled `backfill_messages`; the CRM workflow never logs into
LinkedIn. Google Calendar and Drive-only notes use
[`data-sync-workflow.md`](data-sync-workflow.md).

`Accepted — Awaiting Reply` and `Upcoming Meetings` are retired workbook views.
Neither is created, read, or written by the canonical scheduled workflow.
Historical accepted-connection projection code remains standalone only and must
not be scheduled. Meeting evidence used by Actions and People comes from the
database, not an `Upcoming Meetings` Sheet tab.

Regression QA uses a disposable socket-only PostgreSQL instance, with external
network, browser, and credential access blocked:

```bash
.venv/bin/python scripts/qa_campaigns.py --suite accepted-connections
```

## Admission and action rules

Accounts enter `Active Accounts` from the strongest qualifying evidence:

1. human manual pin, Sales Motion tab, or non-closed human-managed opportunity;
2. a real upcoming meeting or a completed meeting with matched context;
3. a human Gmail inbound/bidirectional thread; or
4. a substantive bidirectional LinkedIn conversation.

One-sided outbound remains contact-only in Postgres. Exact thread-level
message direction determines `Needs response` versus `Waiting`; unrelated
outbound in another thread cannot hide an inbound reply. An account without a
deterministic owner or target remains visible with attention required but does
not enter an outbound queue.

A complete, matched Granola or Gemini `MeetingNote` linked to the exact
Opportunity can create one account-level post-meeting Action without a Calendar
`Meeting` row when its action-item text explicitly assigns work to the resolved
Opportunity owner. Unassigned bullets and customer-owned bullets are ignored.
The exact note UUID keys the durable Action: later unrelated outbound email does
not clear it, while marking that Action handled/completed does.

Database disqualification and company suppression are delivery controls, not
relevance erasers. They suppress outreach while preserving legitimate
meeting/email/account history. Legacy People `Don't send` cells are ignored. Closed Won/Lost, owner,
stage, sales-motion step, commercial fields, manual pins, and genuine human
actions remain human-authoritative.

See [`crm-v2-contract.md`](crm-v2-contract.md) for the complete evidence and
field contract.

## Field ownership

`Active Accounts` is entirely operator-owned. Bound rows contribute only Owner
and Stage to Postgres by exact hidden Opportunity ID, and those exact IDs are
the Actions inclusion boundary. Company, Main point of
contact, Next step, Next step due, and Notes stay Sheet-only, and unbound rows
cannot authorize Actions. The refresh never modifies a cell, row, format,
filter, validation rule, or ordering on this tab.

`Actions` retains conservative three-way merge for Waiting until, Channel,
Draft, Handled, and Disposition. Concurrent DB and Sheet edits produce a
conflict; the system does not guess. Replaceable generated tasks outside the
Active Accounts scope are cancelled. Human-authored database tasks outside the
scope are preserved but omitted from the tab. Its stable IDs and generated
evidence are system-owned.

`People` is generated by `sync_active_account_people`. Active Accounts defines
the exact company/account boundary, then contacts require human inbound Gmail,
calendar/Granola participation, an exact Main point of contact name, or a
deliberately curated OpportunityContact. Exact company name alone is
insufficient. The view updates contact/profile/sync fields; preserves Notes,
Priority, and unrecognized operator columns by visible identity; and omits AI
Notes, Lead ID, Outreach status, and Stage. The
one-time `--reset` option deliberately discards existing rows/manual values.
`preview_crm_v2`, `refresh_crm_v2`, and follow-up drafting still never read it.

## Configuration

Store values in `.env`; never print or commit them.

| Name | Requirement | Role |
|---|---|---|
| `DATABASE_URL` | Required outside tests | Shared Postgres database; no runtime SQLite fallback |
| `GOOGLE_SHEETS_ID` | Required | CRM workbook write target |
| `GOOGLE_SHEETS_CREDENTIALS_PATH` | Required | Service-account JSON with Editor access |
| `GOOGLE_SHEETS_TAB_NAME` | Legacy only (`People`) | Deprecated People export tab |
| `SALES_MOTION_VERSIONS_GOOGLE_SHEETS_ID` | Required for Sales Motion pins | Separate read-only workbook; never the CRM target |
| `GRANOLA_API_KEY` | Optional | Primary read-only meeting-note source |
| `GRANOLA_API_BASE` / `GRANOLA_HTTP_TIMEOUT_SECONDS` | Optional | Granola transport configuration |
| `ACTIVE_TIMEZONE` | Optional (`America/Toronto`) | Business date for due/waiting evaluation |

## Manual ledger conversion and routine publication

`configure_active_accounts` is the only command authorized to reshape Active
Accounts. It is explicit and unscheduled. Preview first; apply takes a private
full-workbook backup and converts the old generated projection once:

```bash
.venv/bin/python manage.py configure_active_accounts
.venv/bin/python manage.py configure_active_accounts --apply
```

The daily workflow then uses:

```bash
.venv/bin/python manage.py refresh_crm_v2 \
  --manual-pin StackArmor \
  --owner-override Ramp=Arian \
  --owner-override StackArmor=Arian

.venv/bin/python manage.py refresh_crm_v2 --apply --routine \
  --manual-pin StackArmor \
  --owner-override Ramp=Arian \
  --owner-override StackArmor=Arian

.venv/bin/python manage.py sync_active_account_people --apply
.venv/bin/python manage.py sync_linkedin_pending --apply
```

Routine mode fails closed unless both `Active Accounts` and `Actions` exist and
all exact legacy canonical titles are absent. It reads the exact Opportunity
scope plus Owner/Stage from bound manual rows, stages and verifies only in-scope
Actions, then swaps only Actions. Active
Accounts is never part of the write batch.

Routine refreshes normally import valid human edits and fail closed on invalid
or stale edits. `--replace-sheet-state` applies only to Actions: it discards
candidate Actions edits and stages a clean queue, while still reading stable-ID
Owner/Stage changes from Active Accounts. It does not inspect `People`.

Every Actions publication orders rows by `Why now` urgency before due date.
The staged tab groups contiguous urgency cohorts with subtle row bands and a
stronger divider at each cohort start; `Why now` cells receive a stronger
matching fill, and due-today/overdue date cells are emphasized. Formatting and
row moves operate on the generated Actions staging copy before verified atomic
replacement. Active Accounts remains fully manual and is never reformatted or
reordered by this workflow.

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
completed context, refresh, People, and LinkedIn Pending phases with exit code zero.
For a midnight Toronto pipeline, schedule the health command at 03:00 daily with
`--expected-run-hour 0`. Without this option the freshness window still starts at
09:00 for compatibility with older schedules. Health summaries use the regular
`SLACK_WEBHOOK_URL`, not the high-signal feed channel.

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

`sync_sheets` remains available only as a deprecated People diagnostic/export:

```bash
.venv/bin/python manage.py sync_sheets --dry-run
```

It does not decide account admission, synthesize sales state, or publish
`Active Accounts`/`Actions`.
