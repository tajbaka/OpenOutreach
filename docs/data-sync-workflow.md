# Google and meeting-context ingestion workflow

This workflow populates the database context consumed by the canonical CRM. It
does not assign sales stages, decide followup eligibility, or write directly to
Google Sheets. `manage.py refresh_crm` remains the only CRM orchestrator and
publisher.

## Source responsibilities

| Source | Ingestion path | Stored result | CRM role |
|---|---|---|---|
| Gmail prospect threads | `sync_gmail_context` / default `refresh_crm` | `crm.Message(source=gmail)` | Communication timeline |
| Gmail-delivered Gemini/Meet notes | `sync_gmail_context` / default `refresh_crm` | `crm.Meeting.gemini_notes_raw` | Secondary meeting context |
| Google Calendar events | Interactive connector workflow | `crm.Meeting` | Real meeting dates/attendees |
| Drive-only Gemini notes | Interactive connector workflow | `crm.Meeting.gemini_notes_raw` | Secondary meeting context |
| Granola notes | Default `refresh_crm` batch sync | `crm.MeetingNote` and match state | Primary meeting context |
| LinkedIn messages | `backfill_messages` / daemon listeners | `crm.Message(source=linkedin)` | Communication timeline |

Granola is primary when a deterministic match exists. Stored Gemini content is
the fallback. Meeting context is attached only after an Action is otherwise
eligible; a note cannot revive an old relationship by itself.

## What `refresh_crm` does and does not refresh

By default, one CRM refresh:

1. uses the configured Gmail API accounts to ingest prospect threads and
   Gmail-delivered Gemini/Meet note emails;
2. batch-fetches incremental Granola notes and rematches cached notes;
3. imports Sheet human edits, recalculates Actions, and publishes the CRM.

It does not log into LinkedIn, query Google Calendar, or search Google Drive.
A green CRM run proves the stored state was processed safely; it does not prove
those external sources were freshly ingested.

Before relying on queue freshness:

- keep `backfill_messages` on its separate schedule for LinkedIn replies;
- run the Calendar/Drive connector steps below after important meetings when
  Gmail-delivered notes are insufficient; and
- inspect Gmail/Granola warnings in the refresh report instead of assuming a
  fallback is current.

## Direct Gmail ingestion

This command is the Gmail/Gemini portion used by `refresh_crm`:

```bash
.venv/bin/python manage.py sync_gmail_context --dry-run
.venv/bin/python manage.py sync_gmail_context
```

It resolves each mailbox and its Send-As aliases through the Gmail API, so
message direction is based on the actual connected account. It upserts by
stable Gmail message identity and records aggregate `WorkflowRun(name="data-sync")`
telemetry. It never sends email.

Useful scopes are available for diagnosis:

```bash
.venv/bin/python manage.py sync_gmail_context --operator Arian --dry-run
.venv/bin/python manage.py sync_gmail_context --skip-notes --dry-run
.venv/bin/python manage.py sync_gmail_context --skip-threads --dry-run
```

Do not use `--skip-threads` and `--skip-notes` together. A dry-run fetches and
matches but does not persist Message, Meeting, or WorkflowRun rows.

## Interactive Calendar/Drive ingestion

Use this section only when the stored Calendar or Drive-only Gemini context may
be stale. The connector account identity must be established before any DB
write.

### 1. Verify the connected Google identity

Read a small Calendar window and resolve the event organizer marked as the
connected account. Map that address through `linkedin.operators.resolve_operator`
and compare it with the intended operator. Stop on a mismatch. Do not attach
another operator's meetings under an override merely to make the run proceed.

Record only the canonical operator handle in logs; do not print OAuth tokens or
full event/note bodies.

### 2. Fetch a bounded Calendar window

Default to the last 90 days plus the next 30 days, primary calendar only. Keep
events with a real external attendee and exclude internal/team-only recurring
noise. Preserve the provider event ID, title, start/end, organizer, attendees,
and response state.

Match attendees to Leads in this order:

1. exact normalized attendee email;
2. exact canonical LinkedIn/contact identity when independently available;
3. account/company plus meeting date/time/title.

Do not match by display name alone. Same-name contacts must remain distinct.
Ambiguous or unmatched attendees are reported for manual resolution and are not
silently attached to a Lead.

Persist each matched Lead's event slice with the existing helper:

```python
from linkedin.notifications.calendar_events import persist_calendar_events

created = persist_calendar_events(lead=lead, events=events_for_lead)
```

The helper upserts by calendar source and external event ID. Repeating the same
payload is idempotent.

### 3. Fetch Drive Gemini notes only when needed

Search a bounded modified-time window and match a Gemini document to an
existing Meeting by event title and date/time. Read the full note only after
that match is deterministic, then persist it:

```python
from linkedin.notifications.calendar_events import persist_gemini_notes

changed = persist_gemini_notes(
    meeting=meeting,
    doc_id=drive_file_id,
    doc_title=drive_file_title,
    raw_text=drive_doc_text,
)
```

Never associate a note because an account word occurs somewhere in its body.
If title/date matching produces more than one candidate, leave it unmatched.
The raw Gemini text is stored as secondary context; do not pre-summarize it into
a human-owned Sheet field.

### 4. Publish through the canonical CRM

After DB ingestion, preview and apply the one canonical publisher:

```bash
.venv/bin/python manage.py refresh_crm --skip-gmail-context
.venv/bin/python manage.py refresh_crm --apply --skip-gmail-context
```

Omit `--skip-gmail-context` if the normal Gmail refresh should run as well.
Granola remains enabled unless `--skip-granola` is explicitly supplied.

Do not call `SheetIndex.upsert_row()` to advance People status/stage or compose
AI Notes. Human sales fields belong on Opportunities and are imported by
stable ID. System meeting context is published from the DB. Pipeline and queue
placement are derived by the shared policy.

## LinkedIn prerequisite

`crm.Message` can become stale after the daemon's first connection-acceptance
snapshot. Keep the read-only ingestion command separate from CRM refresh:

```bash
.venv/bin/python manage.py backfill_messages --dry-run
.venv/bin/python manage.py backfill_messages
```

The command may authenticate to LinkedIn and persist newly observed messages,
but it does not send a message. Configure each required sender account and
monitor its `WorkflowRun` freshness. A stale LinkedIn store can make a daily
queue omit a new inbound or propose an obsolete next action.

## Safety and data handling

- Never commit Calendar/Gmail/Drive payloads, CRM exports, or note text.
- Use `/tmp` or another ignored local directory for bounded connector payloads
  and delete them through the normal host cleanup process.
- Store provider external IDs so re-ingestion remains idempotent.
- Report ambiguous matches; never resolve them by Name or loose transcript
  search.
- Granola/Gmail outages are recoverable warnings during CRM refresh. Calendar
  and Drive staleness must be made explicit because those connectors are not
  called by the scheduled command.
- No ingestion path in this document authorizes outbound Gmail or LinkedIn
  sends.

## Out of scope

- Sales-stage decisions and action eligibility: `refresh_crm`.
- People/Opportunities/Pipeline/Followups/Recovery publication:
  `refresh_crm`.
- Draft generation: `generate_followups` and
  `docs/followup-generation-workflow.md`.
- Message sending: always an operator action outside this workflow.
