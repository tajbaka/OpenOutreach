# Human-in-the-loop CRM workflows

OpenOutreach gathers evidence and publishes a concise work surface. Humans own
sales judgment, high-level pipeline movement, relationship roles, message
review, and sending.

## The four workflows

| Workflow | Purpose | Persistent writes |
|---|---|---|
| `sync_crm_v2_context` | Refresh Gmail/Gemini, validated email-first contacts, and Granola | DB/context only with `--apply` |
| `refresh_crm_v2` | Reconcile evidence/actions and publish `Active Accounts` + `Actions` | DB and Sheets only with `--apply` |
| `sync_trello_pipeline` | Project curated deals to Trello and import human list moves | DB and Trello only with `--apply` |
| `generate_followups` | Export current Actions and validate draft decisions | Action drafts; never sends |

## 1. Work from the CRM radar and queue

Use `Active Accounts` as the broad relationship radar. It shows one account row
with owner, stage projection, attention, admission reason, last meaningful
touch, who owes, next action, due date, and key contacts. `Radar only` means the
account has no curated pipeline card. Human-owned Sheet fields are imported by
stable Opportunity ID; names are display text, not row identity, and `Stage`
is read-only in Sheets.

Use the single `Actions` tab for work. Filter it by Owner rather than switching
between sender tabs. Edit only the human-owned fields such as Draft, Channel,
Handled, Disposition, Waiting until, or explicit next-step state. The next
routine apply imports valid edits by stable Action ID.

Important meeting/Gmail or authoritative work can appear as `Unassigned`.
Assign an owner there rather than allowing the reminder to disappear. Unowned
LinkedIn-only work remains off the queue, and an account-level review has no
draft/channel until an exact contact is chosen.

`People` remains the complete contact/prospecting ledger. Do not clear, reorder,
or deduplicate it manually. One-sided outbound and weak LinkedIn activity
belong there without appearing in Active Accounts.

`Opportunities`, `Pipeline`, `Recovery`, and sender Followups are retired. Do
not recreate or operate from them.

Preview current DB/Sheet effects with the deployment's persistent inputs:

```bash
.venv/bin/python manage.py refresh_crm_v2 \
  --manual-pin StackArmor \
  --owner-override Ramp=Arian \
  --owner-override StackArmor=Arian
```

After cutover, publish through routine mode:

```bash
.venv/bin/python manage.py refresh_crm_v2 --apply --routine \
  --manual-pin StackArmor \
  --owner-override Ramp=Arian \
  --owner-override StackArmor=Arian
```

Conflicts, invalid edits, ambiguous identities, missing owners, and missing
targets are review items—not permission for the system to guess.

## 2. Move curated deals in Trello

Trello is the only human high-level stage surface. Only nonblank pipeline
stages receive cards; the system may initially promote a blank stage to
`Potential / Triage` only for authoritative account state or a completed
meeting plus real human Gmail. It never advances a deal beyond triage.

Move mapped cards among the exact lists below:

1. `Potential / Triage`
2. `Discovery`
3. `Demo / Evaluation`
4. `Pilot / Validation`
5. `Commercial / Procurement`
6. `Nurture / Later`
7. `Closed Won`
8. `Closed Lost`

Review before applying:

```bash
.venv/bin/python manage.py sync_trello_pipeline
.venv/bin/python manage.py sync_trello_pipeline --apply
```

Do not rename/add lists, manually edit managed card titles/descriptions, remove
the stable UUID footer, archive mapped cards, or create cards by hand. The sync
uses IDs, never company names, and fails closed on drift or ambiguity. Trello
Free does not provide Custom Fields, so the card description is intentionally a
small system-owned projection rather than a second CRM record. Card comments
are untouched and are the safe place for human Trello notes.

## 3. Refresh external context

The scheduled context phase directly ingests configured Gmail threads,
Gmail-delivered Gemini/Meet notes, strictly validated email-first contacts, and
Granola. Granola is primary meeting context; stored Gemini is secondary.

```bash
.venv/bin/python manage.py sync_crm_v2_context --apply
```

Two prerequisites remain separate:

- `backfill_messages` keeps later LinkedIn conversations fresh.
- Google Calendar and Drive-only Gemini notes use
  [`data-sync-workflow.md`](data-sync-workflow.md).

Context can admit an account only under the v2 evidence rules. It never advances
a human stage, overwrites human fields, or sends a message.

## 4. Draft followups

Export the current canonical queue:

```bash
.venv/bin/python manage.py generate_followups \
  --output artifacts/followups/codex-review.json
```

The drafting agent copies the stable Action/Opportunity/Lead IDs and context
fingerprint exactly, supplies at most one channel draft, and flags ambiguity.
Apply the validated decision file:

```bash
.venv/bin/python manage.py generate_followups \
  --apply-json artifacts/followups/codex-decisions.json
```

Valid drafts persist and are republished through routine CRM v2 unless
`--no-publish` is supplied. No send API is called. The operator filters Actions,
opens the exact conversation, reviews/sends manually, then records handled,
disposition, or waiting state.

The old name-based rebuild exists only behind explicit
`generate_followups --legacy` for deliberate recovery. It is not a production
or scheduled workflow.

## Dependency flow

```text
LinkedIn backfill ─┐
Gmail/Gemini ──────┼─> crm.Message / crm.Meeting ─┐
Calendar + Drive ──┤                              │
Granola ───────────┘                              v
                                      sync_crm_v2_context
                                                   |
Sheet human edits ─────────────────────────────────v
                                          refresh_crm_v2
                                                   ├─> People
                                                   ├─> Active Accounts
                                                   ├─> Actions ──> generate_followups
                                                   │                  |
                                                   │                  v
                                                   │        operator review/send
                                                   │
                                                   └─> eligible pipeline stage
                                                                  |
                                                                  v
                                                        sync_trello_pipeline
                                                                  └─> Trello lists
```

## What none of these workflows do

- Send Gmail or LinkedIn messages automatically.
- Treat `Deal.state=COMPLETED` as Closed Won.
- Use Name as identity or silently merge same-name contacts.
- Auto-close a polite decline as Lost.
- Auto-advance a Trello pipeline card after initial triage.
- Treat note text alone as permission to contact someone.
- Write to the separate Sales Motion workbook.

See [`crm-refresh-workflow.md`](crm-refresh-workflow.md) for first-cutover,
scheduling, backup, and recovery details.
