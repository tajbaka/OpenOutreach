# Human-in-the-loop CRM workflows

OpenOutreach separates deterministic CRM policy from human sales judgment.
Scheduled code gathers context, imports explicit edits, and publishes stable-ID
views. Operators decide what an Opportunity means, review drafts, and send
messages themselves.

## The three workflows

| Workflow | Purpose | Persistent writes |
|---|---|---|
| `refresh_crm` | Import edits, recalculate Actions, publish CRM views | DB and Sheets only with `--apply` |
| `data-sync-workflow.md` | Ingest Calendar/Drive context not covered by the scheduled command | DB context only |
| `generate_followups` | Export canonical Actions and validate draft decisions | Action drafts; never sends |

## 1. Operate the CRM in Sheets

Use `Opportunities` as the editable account/opportunity table. Human-owned
fields include owner, stage/step, contact roles, next action/due date, waiting,
manual pin, commercial values, and won/lost outcome. The next apply imports
valid changes by stable Opportunity ID before publishing system fields.

Use `<Owner> - Followups` for due-now work. An operator may edit Draft, Channel,
Handled, Disposition, Waiting until, and Manual pin. Those fields are imported
by stable Action ID before the derived queue is regenerated.

Do not edit Pipeline cards as a way to change stage. `Pipeline` and `Recovery`
are derived views. Do not manually clear, reorder, or deduplicate People; it is
the durable contact ledger.

Preview any change set first:

```bash
.venv/bin/python manage.py refresh_crm
```

Then apply and verify idempotence:

```bash
.venv/bin/python manage.py refresh_crm --apply
.venv/bin/python manage.py refresh_crm
```

Conflicts and invalid edits are review items, not prompts for the system to
guess.

During the one-time sender-tab rollout, unresolved material rows from the old
Followups tabs are also review items. The refresh does not infer their identity
or discard them: it keeps the original rows in dated `Legacy` tabs and activates
all affected validated replacements in one atomic title swap. Work from
canonical tabs after the swap; use Legacy only for deliberate review or
recovery.

## 2. Refresh external context

The scheduled CRM refresh directly ingests configured Gmail threads,
Gmail-delivered Gemini/Meet notes, and Granola. Granola is primary meeting
context; stored Gemini is secondary.

Two prerequisites remain separate:

- `backfill_messages` keeps post-accept LinkedIn conversations fresh.
- Google Calendar and Drive-only Gemini notes require the interactive
  `docs/data-sync-workflow.md` path.

Context only enriches canonical state. It does not advance a stage, widen
eligibility, or overwrite human-owned fields by itself.

## 3. Draft followups

Export the current canonical queue:

```bash
.venv/bin/python manage.py generate_followups \
  --output artifacts/followups/codex-review.json
```

The drafting agent copies the stable Action/Opportunity/Lead IDs and context
fingerprint exactly, supplies at most one channel draft, and flags ambiguity for
human review. Apply the validated decision file:

```bash
.venv/bin/python manage.py generate_followups \
  --apply-json artifacts/followups/codex-decisions.json
```

The command stores/publishes drafts but never sends them. The operator reviews
the row, opens the correct conversation, sends manually, and records the human
state in Sheets.

The old name-based tab rebuild is retired. It is available only with explicit
`generate_followups --legacy` and must not be scheduled or used against the
canonical workbook as a normal workflow.

## Dependency flow

```text
LinkedIn backfill ─┐
Gmail context ─────┼─> crm.Message / crm.Meeting ─┐
Calendar + Drive ──┤                              │
Granola ───────────┘                              v
                                           refresh_crm
Sheet human edits ────────────────────────────────┤
                                                  ├─> Opportunities
                                                  ├─> Pipeline / Recovery
                                                  └─> owner Followups
                                                           |
                                                           v
                                                  generate_followups
                                                           |
                                                           v
                                                  operator review/send
```

## What none of these workflows do

- Send Gmail or LinkedIn messages automatically.
- Treat `Deal.state=COMPLETED` as Closed Won.
- Use Name as identity or silently merge same-name contacts.
- Auto-close a polite decline as Lost.
- Treat meeting-note text alone as a reason to contact someone.
- Write to the separate Sales Motion workbook.

See `docs/crm-refresh-workflow.md` for field ownership, stage mapping, backup,
recovery, and runner deployment details.
