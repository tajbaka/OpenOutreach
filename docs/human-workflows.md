# Human-in-the-loop CRM workflows

OpenOutreach gathers evidence and publishes a concise work surface. Humans own
sales judgment, account stage, relationship roles, message review, and sending.

## The three workflows

| Workflow | Purpose | Persistent writes |
|---|---|---|
| `sync_crm_v2_context` | Refresh Gmail/Gemini, validated email-first contacts, and Granola | DB/context only with `--apply` |
| `refresh_crm_v2` | Reconcile evidence/actions and publish `Active Accounts` + `Actions` | DB and Sheets only with `--apply` |
| `generate_followups` | Export current Actions and validate draft decisions | Action drafts; never sends |

## 1. Work from the two CRM tabs

Use `Active Accounts` to understand and manage legitimate opportunities. It
shows one account row with owner, stage, attention, admission reason, last
meaningful touch, who owes, next action, due date, and key contacts. Human-owned
fields are imported by stable Opportunity ID; names are display text, not row
identity.

Use the single `Actions` tab for work. Filter it by Owner rather than switching
between sender tabs. Edit only the human-owned fields such as Draft, Channel,
Handled, Disposition, Waiting until, or explicit next-step state. The next
routine apply imports valid edits by stable Action ID.

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

## 2. Refresh external context

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

## 3. Draft followups

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
                                                   └─> Actions
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
- Treat note text alone as permission to contact someone.
- Write to the separate Sales Motion workbook.

See [`crm-refresh-workflow.md`](crm-refresh-workflow.md) for first-cutover,
scheduling, backup, and recovery details.
