# Google and meeting-context ingestion workflow

This workflow populates the database context consumed by the canonical CRM. It
does not assign sales stages, decide action eligibility, or write directly to
Google Sheets. `manage.py sync_crm_v2_context` is the scheduled context phase;
`manage.py refresh_crm_v2` is the separate canonical reconciler/publisher.

## Source responsibilities

| Source | Ingestion path | Stored result | CRM role |
|---|---|---|---|
| Gmail prospect threads | `sync_crm_v2_context` via `sync_gmail_context` | `crm.Message(source=gmail)` | Primary communication evidence |
| Gmail-delivered Gemini/Meet notes | `sync_crm_v2_context` via `sync_gmail_context` | `crm.Meeting.gemini_notes_raw` | Secondary meeting context |
| Google Calendar events | Interactive connector workflow | `crm.Meeting` | Real meeting dates/attendees |
| Drive-only Gemini notes | Interactive connector workflow | `crm.Meeting.gemini_notes_raw` | Secondary meeting context |
| Granola notes | `sync_crm_v2_context` batch sync | `crm.MeetingNote` and match state | Primary meeting context |
| LinkedIn messages | `backfill_messages` / daemon listeners | `crm.Message(source=linkedin)` | Communication timeline |

Granola is primary when a deterministic match exists. Stored Gemini content is
the fallback. Meeting context is attached only after an Action is otherwise
eligible; a note cannot revive an old relationship by itself.

## What the scheduled context phase does and does not refresh

By default, one `sync_crm_v2_context --apply`:

1. uses the configured Gmail API accounts to ingest prospect threads and
   Gmail-delivered Gemini/Meet note emails;
2. batch-fetches incremental Granola notes and rematches cached notes;
3. creates only strictly validated corporate email-first Leads from private
   discovery state and relinks their exact Gmail threads when needed.

It does not publish Sheets, log into LinkedIn, query Google Calendar, or search
Google Drive. A green context run proves those configured sources were handled
safely; it does not prove separate LinkedIn/Calendar/Drive inputs are fresh.

Before relying on queue freshness:

- keep `backfill_messages` on its separate schedule for LinkedIn replies;
- run the Calendar/Drive connector steps below after important meetings when
  Gmail-delivered notes are insufficient; and
- inspect Gmail/Granola warnings in the refresh report instead of assuming a
  fallback is current; and
- run `refresh_crm_v2` separately to reconcile and publish stored evidence.

## Direct Gmail ingestion

This command is the lower-level Gmail/Gemini portion used by
`sync_crm_v2_context`:

```bash
.venv/bin/python manage.py sync_gmail_context --dry-run
.venv/bin/python manage.py sync_gmail_context
```

It resolves each mailbox and its Send-As aliases through the Gmail API, so
message direction is based on the actual connected account. Every Lead with an
exact email address is considered even when it has no Deal or is marked
`disqualified`: those values suppress automated outreach, not relationship
history. Known addresses are searched at most 40 at a time. A truncated
500-hit OR search is recursively split so one noisy address cannot hide the
others; a still-truncated single-address search is bounded at 2,000 hits and
reported. Across known and discovery lanes, at most 80 unique threads are
fetched per mailbox/run. Deferred work rotates on later apply runs using an
opaque per-thread version checkpoint. From/To/Cc/Bcc headers provide identity
per message, Draft/Scheduled mail is excluded, and strong automatic-reply and
list/bulk signals are removed. Gmail category labels alone do not discard an
exact known human contact.

The default run also performs a newest-first, bounded 90-day scan (at most 500
message hits and 500 unique thread candidates) for email-first relationships.
The shared 80-thread fetch cap still applies. Discovery and all dry-runs use
Gmail metadata rather than decoding message bodies. It returns
only unknown external participants with both a human inbound and an exact
outbound recipient match in the same thread. These structured candidates use
only RFC display name, exact email/domain, timestamps, and opaque thread ID;
they are review input and do not auto-create a Lead, Deal, Task, or send. The
company is never inferred from subject/body text. Use
`--skip-unmapped-discovery` for a known-contact-only diagnostic run.
Programmatic shadow/review consumers call `sync_gmail_threads(..., dry_run=True)`
and read `GmailContextSyncResult.unmapped_external_participants`; each item has
`account_key`, `email`, `display_name`, `domain`, `last_inbound_at`,
`latest_thread_id`, and `thread_count`. The management command intentionally
prints only the count. A successful default apply stores the rotating checkpoint
and still-recent structured candidates atomically in private mode-0600
`data/gmail/<account>-context-state.json`; dry-runs do not update that state.

The command namespaces mailbox-local Gmail message/thread IDs before upsert,
resolves outbound owner from the exact Send-As alias, and records aggregate
`WorkflowRun(name="data-sync")` telemetry. Console output is aggregate-only and
does not print mailbox addresses, note subjects, or bodies. Google API request
logging is suppressed because search URLs contain lead emails. It never sends email.

Useful scopes are available for diagnosis:

```bash
.venv/bin/python manage.py sync_gmail_context --operator Arian --dry-run
.venv/bin/python manage.py sync_gmail_context --skip-notes --dry-run
.venv/bin/python manage.py sync_gmail_context --skip-threads --dry-run
.venv/bin/python manage.py sync_gmail_context --skip-unmapped-discovery --dry-run
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

After DB ingestion, preview and apply the canonical publisher with the
deployment's persistent pins/owner overrides. On an already-cut-over workbook:

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

If Gmail/Gemini/Granola also needs refreshing, run
`sync_crm_v2_context --apply` first. Context is intentionally not hidden inside
the Sheet publisher.

Do not call `SheetIndex.upsert_row()` to advance People status/stage or compose
AI Notes. Human sales fields belong on `Active Accounts`/`Actions` and are
imported by stable ID. System meeting context is published from the DB; account
admission and queue placement come from the v2 evidence/action policy.

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
- Granola/Gmail availability is reported by `sync_crm_v2_context`. Calendar and
  Drive staleness must be explicit because those connectors are not called by
  the scheduled command.
- No ingestion path in this document authorizes outbound Gmail or LinkedIn
  sends.

## Out of scope

- Sales-stage decisions and action eligibility: `refresh_crm_v2`.
- People/Active Accounts/Actions publication: `refresh_crm_v2`.
- Draft generation: `generate_followups` and
  `docs/followup-generation-workflow.md`.
- Message sending: always an operator action outside this workflow.
