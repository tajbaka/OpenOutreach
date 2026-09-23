# Human-in-the-loop CRM workflows

OpenOutreach gathers evidence and publishes a concise work surface. Humans own
sales judgment, account stage, relationship roles, message review, and sending.

## The five workflows

| Workflow | Purpose | Persistent writes |
|---|---|---|
| `sync_crm_v2_context` | Refresh Gmail/Gemini, validated email-first contacts, and Granola | DB/context only with `--apply` |
| `refresh_crm_v2` | Read manual Owner/Stage, reconcile evidence/actions, and publish `Actions` | DB and generated Sheets only with `--apply` |
| `sync_active_account_people` | Rebuild contacts scoped by manual Active Accounts | Generated People rows with `--apply` |
| `sync_linkedin_pending` | Incrementally reconcile recent LinkedIn messages waiting on a reply | Generated `LinkedIn Pending` rows with `--apply`; never sends |
| `generate_followups` | Export current Actions and validate draft decisions | Action drafts; never sends |

## 1. Work from the two CRM tabs

Use `Active Accounts` as the manual company ledger. Its visible columns are
Company, Owner, Stage, Main point of contact, Next step, Next step due, and
Notes. Add, remove, sort, and annotate rows freely. The daily workflow never
writes this tab. For rows carrying a hidden Opportunity ID, only Owner and Stage
are read into the database; unbound rows remain Sheet-only and names are never
used as identity. Those exact Opportunity IDs also define which accounts may
appear in Actions; People is not used as a second gate.

Use the single `Actions` tab for work. Filter it by Owner rather than switching
between sender tabs. Edit only the human-owned fields such as Draft, Channel,
Handled, Disposition, Waiting until, or explicit next-step state. The next
routine apply imports valid edits by stable Action ID.

System-generated Actions are intentionally narrow: real calendar meetings and
high-intent Gmail replies/threads only, and only for Opportunities bound in
Active Accounts. LinkedIn messages remain stored context
and may support Active Account admission, but they are not inspected for Action
creation, targeting, or refresh. Ordinary courtesy Gmail replies likewise do
not create Actions. Explicit human-authored current actions remain authoritative
in Postgres, but out-of-scope ones are not published to Actions.

`People` is a generated contact view whose inclusion boundary is Active
Accounts. Add a company to set its exact account boundary; within it, the daily
People phase includes only contacts backed by inbound Gmail, calendar/Granola
participation, an exact Main point of contact name, or a deliberately curated
OpportunityContact. Company-name-only cold prospects are excluded. Remove the
company and those contacts disappear. Edit Notes, Priority, or custom columns
manually; the workflow carries them forward. AI Notes, Lead ID, Outreach status,
and Stage are intentionally absent.

`LinkedIn Pending` is a separate generated inbox queue, not an Actions source.
It shows only stored LinkedIn conversations from the last 14 days with an
unanswered deterministic question, request, scheduling signal, or interest
signal. Acknowledgements, polite declines, automated text, blank/ambiguous
threads, and non-actionable statements are excluded. A later persisted human
outbound removes the row; a later actionable inbound reopens the same stable
conversation row. Do not add manual rows or formulas because the publisher
owns every field and incrementally appends, updates, and deletes by hidden ID.

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
Granola. Granola is primary meeting context; stored Gemini is secondary. This
phase writes database context only and never publishes `People`, `Active
Accounts`, `Actions`, or any other Sheet.

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
Active Accounts exact Opportunity scope + edits ──v
                                          refresh_crm_v2
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

The old all-Leads `sync_sheets` publisher is legacy-only and must not be
scheduled; the daily workflow uses `sync_active_account_people --apply`.
The same scheduled wrapper then runs `sync_linkedin_pending --apply`.

See [`crm-refresh-workflow.md`](crm-refresh-workflow.md) for first-cutover,
scheduling, backup, and recovery details.
