# Canonical CRM refresh workflow

`manage.py refresh_crm` is the only orchestration entry point for the Google
Sheets CRM. It reads the live workbook, refreshes stored context, imports valid
human edits, recalculates canonical Actions, and publishes the CRM views. It
never sends Gmail or LinkedIn messages.

Omit `--apply` for the default exact dry-run. The initial additive database
migration must be preceded by a reviewed migration plan and verified Postgres
snapshot; the new tables must exist before `refresh_crm` can calculate its
first Sheet plan. After migrating, every deployment that changes CRM policy or
publishing code must run a reviewed `refresh_crm` dry-run against the same
checkout and configuration before any Sheet apply. Routine runs then use the
validated `--apply` workflow under its lock and preservation checks.

## Workbook contract

| Surface | Lifecycle | Source of truth |
|---|---|---|
| `People` | Durable, incremental contact ledger | DB Lead identity plus preserved operator cells |
| `Opportunities` | Durable, editable, stable-ID merge | `crm.Opportunity` plus imported human fields |
| `Pipeline` | Derived stage-as-columns view | Canonical Opportunities |
| `<Owner> - Followups` | Derived due-now Action view | Canonical Opportunities and Actions |
| `Recovery` | Derived older-inactive review view | Canonical action policy |
| `ICP Goals` | Read-only input | Existing worksheet |
| Sales Motion workbook | Read-only and separate | `SALES_MOTION_VERSIONS_GOOGLE_SHEETS_ID` |

`People` is never cleared, rebuilt, reordered, pruned, or shrunk. Existing rows
are updated in place and new Leads are appended once. Stable Lead ID is the
canonical key; exact canonical LinkedIn URL is the legacy bootstrap key. Human
cells, formulas, comments, formatting, and unknown operator-added columns are
outside managed write ranges.

`Opportunities` is one record per account and sales motion, with multiple
linked contacts. `Pipeline`, `Recovery`, and sender Followups are regenerated
only inside managed cells; worksheet identity and unrecognized user content are
preserved. Names are display text and are never identity keys.

The old sender Followups tabs are a one-time rollout input, not an identity
authority. The migration imports a human field only when positive stable
evidence identifies exactly one canonical row. Any unresolved material row is
left untouched, counted in the report, and preserved in the dated `Legacy` tab;
it is never name-matched or guessed. Review-required legacy rows do not block an
otherwise-safe canonical rollout. The refresh first builds and validates every
affected sender replacement, then swaps every old/new title pair in one atomic
Google Sheets batch. A preparation or validation failure before that batch
leaves the complete old sender-tab set active. If temporary replacements were
already created, the failure report names them as inspectable orphan tabs; keep
them until the failure is reconciled, and do not mistake them for live queues.

`Deal.state=COMPLETED` means outreach automation finished. It never means
Closed Won.

## Field ownership

The ownership boundary is enforced by range-limited writes and conservative
three-way merges.

Human-owned, imported Sheet to DB:

- Owner, Stage, and Sales motion step
- Champion Lead ID, Decision Maker Lead ID, and Stakeholder Lead IDs
- Next action and Next action due date
- Manual pin and Waiting until
- Value, Currency, and Probability
- Closed won date, or Closed lost date and reason
- On Followups: Waiting until, Channel, Draft, Handled, Disposition, and
  Manual pin

System-owned, published DB to Sheet:

- Stable Lead, Account, Opportunity, and Action IDs
- Account/contact identity and linked-contact membership
- LinkedIn/Gmail activity and real meeting dates
- Granola/Gemini meeting context and its source
- Stage-entered date, last meaningful activity, source timestamps, and sync
  revision metadata

Derived and safely regenerated:

- Overdue state and Action category
- Inactivity age and Recovery eligibility
- Pipeline position and macro-stage mapping
- Which explicitly owned Actions are due in each sender queue

A blank system value cannot erase a nonblank human-owned value. Invalid edits
are reported and skipped. If both the DB and Sheet changed a human field since
the last published baseline, that row is a conflict; the refresh does not guess
which side wins.

## Sales-motion stages

Stage is stored on the Opportunity, not inferred from a Pipeline card's visual
position.

| Macro stage | 15-step motion |
|---|---:|
| Prospecting | 1 |
| Discovery | 2 |
| Demo Planning | 3-4 |
| Evaluation | 5-6 |
| Sandbox/Pilot | 7-10 |
| Commercial | 11 |
| Procurement/Legal | 12-14 |
| Closed Won | terminal outcome |
| Expansion | 15 |
| Closed Lost | terminal outcome with reason |

## Action policy

Daily sender Followups contain genuine work only: a new inbound response, an
overdue or due-today next action, upcoming-meeting preparation, an unresolved
post-meeting commitment, a missing next action on an active Opportunity, or an
explicit current-action exception.

- 0-21 days since meaningful activity: eligible for a daily queue only when an
  action is genuinely required.
- 22-60 days: Recovery unless an explicit due action, fresh trigger, real
  upcoming meeting, or manual pin overrides age.
- More than 60 days: archive/nurture under the same explicit exceptions.
- Future Waiting Actions remain on Opportunities and disappear from daily
  queues until the configured business date is due.
- Closed Won, Closed Lost, Don't Send, disqualified, non-actionable failed, and
  polite-decline records do not enter daily sender queues. A polite decline is
  never automatically marked Closed Lost.
- Explicit Opportunity owner is authoritative. Fallback owner inference is
  used only when unambiguous; unowned current work remains in Recovery and is
  never duplicated across senders.

## Context sources and prerequisites

Granola is the primary meeting-note source. Stored
`Meeting.gemini_notes_raw` is the secondary fallback. Granola is fetched once
per refresh and matched deterministically by attendee email, exact normalized
identity, or account plus meeting date/time/title. A company word occurring in
note text is not a match. Meeting context enriches an already eligible Action;
it cannot make stale work eligible.

By default, `refresh_crm` also runs `sync_gmail_context`, which uses the repo's
configured Gmail API accounts to persist prospect threads and Gmail-delivered
Gemini/Meet notes. A recoverable Gmail or Granola outage emits a warning and
retains stored context.

The refresh does **not** log into LinkedIn and does **not** query Google
Calendar or Drive directly. Before trusting a queue:

1. Keep `manage.py backfill_messages` on its separate read-only-ingestion
   schedule so later LinkedIn replies reach `crm.Message`.
2. Populate real calendar events and any Drive-only Gemini notes through
   `docs/data-sync-workflow.md` when those sources matter.
3. Treat a successful refresh as a correct view of stored context, not proof
   that an external source was freshly ingested.

Use `--skip-gmail-context` or `--skip-granola` only when deliberately accepting
cached context. Neither flag turns stale data into fresh data.

## Configuration names

Set values in `.env`; never print or commit them:

| Name | Requirement | Default / role |
|---|---|---|
| `DATABASE_URL` | Required outside tests | Shared Postgres database; there is no runtime SQLite fallback |
| `GOOGLE_SHEETS_ID` | Required by either Sheets command | CRM workbook write target |
| `GOOGLE_SHEETS_CREDENTIALS_PATH` | Required by either Sheets command | Service-account JSON with Editor access to the CRM workbook |
| `GOOGLE_SHEETS_TAB_NAME` | Optional | `People` |
| `SALES_MOTION_VERSIONS_GOOGLE_SHEETS_ID` | Required by `refresh_crm --apply` | Separate read-only Sales Motion workbook; never a write target |
| `GRANOLA_API_KEY` | Optional | Enables the primary read-only Granola source |
| `GRANOLA_API_BASE` | Optional | `https://public-api.granola.ai/v1` |
| `GRANOLA_HTTP_TIMEOUT_SECONDS` | Optional | `30` |
| `ACTIVE_TIMEZONE` | Optional | `America/Toronto`; business date for action policy |

A dry-run can inventory the CRM without the guard, but live apply refuses to
run unless the Sales Motion ID is configured and different from
`GOOGLE_SHEETS_ID`. Without an available Granola source, stored Gemini context
remains the fallback.

## Refresh order

One run performs these steps:

1. Acquire a nonblocking CRM refresh lock.
2. Verify the opened workbook is exactly `GOOGLE_SHEETS_ID`, not the Sales
   Motion workbook, and inventory tabs, headers, structures, keys, and
   duplicates.
3. Refresh Gmail/Gemini context and batch-sync/rematch Granola unless skipped.
4. Read the live canonical tabs and import valid human Opportunity/Action edits
   by stable ID.
5. Bootstrap only conservatively eligible Opportunities and recalculate
   Actions through the shared date-bucketed policy.
6. Incrementally publish People and assert that no preexisting row, URL key, or
   column disappeared.
7. Incrementally publish Opportunities and regenerate managed Pipeline and
   Recovery ranges.
8. For the one-time legacy migration, prepare and validate all sender
   replacements, preserve unresolved rows without guessing, and activate every
   old/new title pair in one atomic batch; on later runs, update canonical
   sender Followups in place.
9. Record `WorkflowRun` telemetry with per-surface counts.

`sync_sheets` is intentionally narrower: it plans or publishes only the durable
People ledger. It does not synthesize meeting intent, assign sales stages,
decide eligibility, or rebuild Followups.

## Dry-run and first live migration

Install dependencies with the environment-specific requirements file:

```bash
# Developer checkout
.venv/bin/python -m pip install -r requirements/local.txt

# Runner/production checkout
.venv/bin/python -m pip install -r requirements/production.txt
```

Pause CRM writers for the short migration window. Take a Postgres custom-format
snapshot with a `pg_dump` client compatible with the server's major version,
store it outside git with owner-only permissions, and verify it with
`pg_restore --list`. A provider snapshot is also acceptable when its restore
path has been tested. Record the exact pre-migration commit and database target
without copying credentials into logs.

The automatic `refresh_crm` backup protects Google Sheets only; it is not a
database backup. After the verified database snapshot, inspect and apply the
additive migrations, then generate the exact live no-write Sheet plan:

```bash
.venv/bin/python manage.py migrate --plan
.venv/bin/python manage.py migrate
.venv/bin/python manage.py refresh_crm
```

The dry-run reads live external context and executes the real DB calculation
path inside a rollback-only transaction. It does not persist DB changes, Sheet
writes, Granola watermarks, or `WorkflowRun` rows. Review workbook fingerprint,
inventory, People insert/update counts, Opportunity imports/conflicts/skips,
owner ambiguity, inclusion/exclusion reasons, Recovery/archive counts,
duplicate keys, preserved human fields, and every managed-tab plan.
On the first rollout, inspect `legacy_followup_review_required` and each
owner's `material_skip_reasons`. A nonzero review count means those source rows
will remain in Legacy for deliberate review; it is not itself a publication
blocker. A true top-level `blocked` value or an owner `blocked` result still is
a blocker and must be resolved before apply.

Only after that report is acceptable:

```bash
.venv/bin/python manage.py refresh_crm --apply
.venv/bin/python manage.py refresh_crm
```

The second dry-run is the idempotence check. It should plan no new People
appends, no repeated legacy migration/title swap, and no unexplained canonical
row changes. Expected date-driven Action changes must be attributable to the
single reported evaluation date. Unexpected writes, conflicts, key loss, or
repeated migration work must be investigated before scheduling.

The first apply creates canonical tabs additively. Before structural writes it
creates a timestamped, mode-`0600` JSON backup under the gitignored
`artifacts/crm-backups/` directory. It prepares and validates every canonical
sender replacement before one cohort-wide atomic title batch. Existing sender
tabs are preserved intact under unique dated `Legacy` titles. Unresolved
material rows remain there and in review telemetry; they are not imported by
guess, deleted, or treated as blockers to otherwise-safe canonical activation.

## Windows runner deployment

Run PowerShell from the existing runner checkout. Replace only the checkout
path; the branch for this deployment is `temp`.

```powershell
Set-Location -LiteralPath "C:\path\to\OpenOutreach"

$TrackedChanges = git status --porcelain --untracked-files=no
if ($LASTEXITCODE -ne 0) { throw "git status failed" }
if ($TrackedChanges) { throw "runner checkout has tracked local changes" }

git fetch origin
if ($LASTEXITCODE -ne 0) { throw "git fetch failed" }
git show-ref --verify --quiet refs/heads/temp
if ($LASTEXITCODE -eq 0) {
    git switch temp
} else {
    git switch --track -c temp origin/temp
}
if ($LASTEXITCODE -ne 0) { throw "git switch temp failed" }
git pull --ff-only origin temp
if ($LASTEXITCODE -ne 0) { throw "git pull --ff-only failed" }

if (-not (Test-Path -LiteralPath ".venv\Scripts\python.exe")) {
    py -3.12 -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "venv creation failed" }
}
& ".venv\Scripts\python.exe" -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed" }
& ".venv\Scripts\python.exe" -m pip install -r "requirements\production.txt"
if ($LASTEXITCODE -ne 0) { throw "dependency install failed" }
& ".venv\Scripts\python.exe" manage.py migrate --plan
if ($LASTEXITCODE -ne 0) { throw "migration plan failed" }
& ".venv\Scripts\python.exe" manage.py migrate
if ($LASTEXITCODE -ne 0) { throw "migration apply failed" }
& ".venv\Scripts\python.exe" manage.py refresh_crm
if ($LASTEXITCODE -ne 0) { throw "refresh_crm dry-run failed" }

# Review the JSON report before running this line.
& ".venv\Scripts\python.exe" manage.py refresh_crm --apply
if ($LASTEXITCODE -ne 0) { throw "refresh_crm apply failed" }

# Confirm the applied state is idempotent.
& ".venv\Scripts\python.exe" manage.py refresh_crm
if ($LASTEXITCODE -ne 0) { throw "post-apply dry-run failed" }
```

The existing Task Scheduler action remains
`scripts\run_sync_sheets.ps1`; the wrapper now invokes
`manage.py refresh_crm --apply` and writes
`data\logs\refresh_crm_task.log`. Locate that existing task by its action,
restart it, and inspect its result without assuming a machine-specific task
name:

```powershell
$Task = Get-ScheduledTask | Where-Object {
    $_.Actions.Arguments -like "*run_sync_sheets.ps1*"
} | Select-Object -First 1
if (-not $Task) { throw "Task using run_sync_sheets.ps1 was not found" }

if ($Task.State -eq "Running") {
    Stop-ScheduledTask -InputObject $Task
}
Start-ScheduledTask -InputObject $Task
$Deadline = (Get-Date).AddMinutes(30)
do {
    Start-Sleep -Seconds 10
    $Task = Get-ScheduledTask -TaskName $Task.TaskName -TaskPath $Task.TaskPath
} while ($Task.State -eq "Running" -and (Get-Date) -lt $Deadline)
if ($Task.State -eq "Running") { throw "refresh_crm task timed out" }

$TaskInfo = Get-ScheduledTaskInfo -InputObject $Task
$TaskInfo | Format-List LastRunTime,LastTaskResult,NextRunTime
if ($TaskInfo.LastTaskResult -ne 0) {
    throw "refresh_crm task failed with result $($TaskInfo.LastTaskResult)"
}
Get-Content -LiteralPath "data\logs\refresh_crm_task.log" -Tail 100
```

Exit code `0` and a final `CRM refresh applied and verified` log line are the
success signal. Do not treat the task merely starting as success.

## Backup and recovery

On any apply failure:

1. Stop the scheduled task and do not run another apply blindly.
2. Keep the printed backup file, log, and failed dry-run report local and out
   of git. Never paste their CRM contents into an issue or chat.
   Preserve any reported temporary replacement tabs while diagnosing; the old
   canonical sender titles remain authoritative until an atomic swap succeeds.
3. Run a new no-write `refresh_crm` to identify whether the blocker is a
   conflict, invalid human edit, duplicate key, preservation assertion, or
   transient API failure.
4. Prefer Google Sheets version history to restore the entire workbook to the
   pre-apply point. During first migration, the dated Legacy Followups tabs are
   also immediate recoverable copies.
5. If Drive history is unavailable, the local backup contains each tab's
   displayed values, formulas, worksheet IDs/grid sizes, and workbook schema
   metadata for a deliberate manual reconstruction. There is no automatic
   restore command; do not overwrite a live workbook from JSON without first
   duplicating the failed workbook and validating the reconstruction in the
   copy.
6. After restoration or conflict resolution, rerun dry-run, apply once, and
   rerun dry-run for idempotence.

Database recovery is separate. The apply command's outer transaction rolls
back imported human edits, recalculated Actions, Sheet baselines, Granola
watermarks, and `WorkflowRun` state when publication fails. The pre-migration
Postgres snapshot protects the earlier schema/data point; restoring it is a
deliberate operator action that requires stopped writers and a validated target.
Do not reverse or restore the database merely to undo a Sheet-only problem.

A Sheets API failure does not authorize deleting or recreating canonical tabs.
The legacy title change itself is one atomic batch, so a failed title request
cannot leave only some sender titles swapped. Other remote Sheet requests may
still have succeeded before a later request failed, so retain the pre-apply
backup and use the next dry-run as the reconciliation plan.

## Scoped diagnostics

Use cached meeting context and avoid external refresh calls:

```bash
.venv/bin/python manage.py refresh_crm --skip-gmail-context --skip-granola
```

Apply canonical views without publishing People:

```bash
.venv/bin/python manage.py refresh_crm --apply --skip-people
```

Inspect only the narrow People publisher:

```bash
.venv/bin/python manage.py sync_sheets --dry-run
```

The `--skip-people` apply is a repair tool, not the normal scheduled path.
