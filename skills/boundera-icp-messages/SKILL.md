---
name: boundera-icp-messages
description: Create, revise, or validate Boundera campaign copy in the OpenOutreach General ICP Messages Google Sheet, including the wide connection, LinkedIn follow-up, and Gmail sequence and its explicit import into checked-in JSON. Use only for structured General ICP Messages authoring or review. Do not use for one-off sales replies, scheduling, opportunity strategy, campaign activation, or sending messages.
---

# Boundera ICP Messages

## Scope

Use this skill only for structured copy in the exact `General ICP Messages`
Google Sheet tab. The tab is mutable authoring state and is never read by
runtime code. Reviewed rows are explicitly imported into `shared_programs` in
`linkedin/icp_messages.json` and `gmail/icp_emails.json`. Campaign creation
then snapshots the selected current JSON program as an immutable SQL
`MessageProgramVersion`; each Deal freezes its audience and operator before any
delivery is materialized.

The hidden legacy sender-specific tabs and sender JSON stores remain
transitional rollback sources for campaigns with no version binding. Do not
author new campaign routes there and never copy a bound campaign back to
sender JSON.

Use `boundera-sales` for an individual reply, scheduling note, recap,
objection, or other ready-to-send message outside this structured Sheet. Use
`boundera-sales-motion` for opportunity tracking. This skill never activates a
campaign and never sends outreach.

## Sources

Locate the OpenOutreach repo before working. For a General Sheet request:

1. Read `references/icp-personas.md` for the human-maintained persona priority
   and attention angle. This file contains strategy, not deployed copy.
2. Read the live `General ICP Messages` tab before drafting. Human edits may
   not have been imported yet.
3. Read `linkedin/general_icp_review_status.json` for operator-only review
   state. A field is current only when its stored `content_sha256` matches the
   live Sheet cell. This ledger is not deployed copy or product evidence, and
   campaign runtime never reads it.
4. Inspect the target Program Key in the checked-in `shared_programs` sections
   of both JSON stores when comparing authoring state to deployable copy.
5. Consult legacy sender JSON only when reviewing an explicitly unbound legacy
   campaign or comparing historical voice. JSON is not product evidence and
   is not a fallback for a version-bound Deal.
6. Read the shared references under `skills/boundera-sales/references/`:
   - `copy-patterns.md` for measurable channel budgets;
   - `fedramp-20x.md` for 20x claims; and
   - `fedramp-rev5.md` when the copy mentions Rev5, SSP, SAP, SAR, POA&M,
     legacy ConMon, Ready, In Process, migration, or a 20x-versus-Rev5
     comparison.
7. Browse current official FedRAMP sources before using a date, deadline,
   current program status, class/path availability, mandatory/optional claim,
   or exact quotation.
8. For any named Boundera capability, locate the current FedRampGPT product
   repo and verify the exact behavior in relevant product documentation,
   implementation, routes or services, and tests before using it in Sheet
   copy.

## Program, Audience, and Sender Selection

- Preserve an existing row's exact `ICP` label unless the user requests a
  structural change. The importer derives the stable audience identity from it.
- Use one audience for each combination that truly needs different copy. A
  role can have multiple stage-specific audiences when its incentive changes
  across 20x Initial, legacy Ready/conversion, Rev5 authorized maintenance,
  remediation, or another verified Marketplace state.
- Never infer FedRAMP stage, budget pressure, buying authority, urgency, or
  technical ownership from a title alone.
- General rows are shared copy. Keep sender-specific legacy variants out of
  this tab.
- Do not create duplicate shared and sender-specific variants accidentally.
  Variants within one route must use distinct stable `Variant Key` values.

## Sheet Workflow

1. Inspect the target program, audience, and all of its existing channel rows.
   Identify the exact route before proposing edits.
2. Identify the intended persona, verified campaign segment, channel, cadence,
   and one desired response for each message. If the persona is not represented
   in `icp-personas.md`, use confirmed user context and propose a persona
   reference addition separately; do not invent buyer facts.
3. Draft each row as a standalone message. LinkedIn and Gmail lanes can run
   independently, so an email must not rely on the recipient having seen a
   particular LinkedIn follow-up, or vice versa.
4. Apply the shared copy budgets and one-primary-CTA rule. Connection notes
   should establish relevance without a calendar link or product monologue.
   Do not use Unicode em dashes (`—`) or en dashes (`–`) in authored
   message copy. Treat both as the "AI dashes" to remove: rewrite the sentence
   with ordinary punctuation or split it into two sentences. ASCII hyphens may
   remain in normal compound words, and the `→` delimiter in the ICP identity
   label must remain unchanged.
5. Use only these placeholders: `{first_name}`, `{last_name}`,
   `{company_name}`, `{my_name}`, `{our_company_name}`, `{our_website_url}`,
   and `{role}`. `{role}` uses the shared wording lookup in
   `linkedin/message_roles.py` for the lead's saved `role_tag` (for example,
   `CFO/Finance` becomes `finance leaders`); a blank tag becomes `companies`.
   It never selects an ICP or infers a role from a title. Inspect that lookup
   before using the token, and check the rendered phrase and blank fallback
   in context. This does not authorize adding role placeholders to other copy.
6. Use the exact eight-column wide schema, in this order:
   - `ICP`
   - `Connect Message`
   - `Followup Message 1`
   - `Email Subject 1`
   - `Email Body 1`
   - `Followup Message 2`
   - `Email Subject 2`
   - `Email Body 2`
7. `ICP` is the human-readable leftmost identity and should use the exact shape
   `Role | Company Size | FedRAMP Stage | Revenue Intent`, for example
   `CRO | Enterprise | Rev5 Authorized → 20x | Direct agency`. Revenue Intent
   must be exactly `Direct agency`, `CSP ecosystem`, `Both`, or `Unclear`.
   Confidence and research evidence stay in the lead data and never create
   additional message rows. The importer derives the fixed program identity,
   intent-specific audience key, role persona, and segment from this label.
8. Every row requires a Connect Message. Email Subject N and Email Body N must
   be filled or blank as a pair. The importer expands the wide row into fixed
   primary routes and canonical delays; do not add visible technical columns.
9. When the user authorizes a Sheet edit, update only the intended rows or
   cells, then read them back and run the strict parser. Preserve other rows,
   formatting, filters, and unknown structure.
10. After the readback succeeds, update only those exact fields in
    `linkedin/general_icp_review_status.json`. Key entries by the complete ICP
    label, never by Sheet row number. Record an exact `last_modified_at` for a
    live edit; use `last_modified_date` only when backfilling a known date whose
    time cannot be verified. A user-approved unchanged cell is
    `reviewed_unchanged` and retains any earlier modification date.

## Review Ledger Workflow

The review ledger is mandatory working state for this skill. Do not add review
dates or technical metadata to the eight-column Sheet, and do not manually
invent or copy a hash into the ledger.

At the start of every General ICP Messages task, inspect the live Sheet against
the checked-in ledger:

```bash
.venv/bin/python manage.py review_general_icp_messages
```

Use the result to distinguish:

- `current`: reviewed content whose stored hash still matches the live cell;
- `stale`: a previously reviewed cell whose live content has changed;
- `missing`: a tracked ICP label that no longer exists in the Sheet; and
- `untracked`: content with no recorded review decision yet.

After every authorized live Sheet edit, read the changed cells back, strict-
parse the complete tab, and immediately record each changed field. The command
uses the current timestamp for both review and modification when `--changed` is
present and no historical timestamp is supplied:

```bash
.venv/bin/python manage.py review_general_icp_messages \
  --icp 'Founder/CEO | Small | 20x Initial Implementation | Direct agency' \
  --field connect_message \
  --reviewed-by Arian \
  --changed \
  --apply
```

For a complete role, size, and stage cohort whose four revenue-intent rows were
all changed together, use `--cohort` instead of listing the four exact ICP
labels. Repeat `--field` when more than one message cell changed. Supported
ledger field keys map to Sheet columns as follows:

| Ledger field | Sheet column |
| --- | --- |
| `connect_message` | `Connect Message` |
| `followup_message_1` | `Followup Message 1` |
| `email_subject_1` | `Email Subject 1` |
| `email_body_1` | `Email Body 1` |
| `followup_message_2` | `Followup Message 2` |
| `email_subject_2` | `Email Subject 2` |
| `email_body_2` | `Email Body 2` |

When the user explicitly approves existing copy without changing it, omit
`--changed`. This records `reviewed_unchanged` and preserves any previously
known modification time:

```bash
.venv/bin/python manage.py review_general_icp_messages \
  --icp 'Founder/CEO | Small | 20x Initial Implementation | Direct agency' \
  --field connect_message \
  --reviewed-by Arian \
  --apply
```

Use `--modified-at` only for a known exact ISO 8601 edit timestamp. Use
`--modified-date YYYY-MM-DD` only for an honest historical date-only backfill.
After any ledger write, rerun the inspection command and require every touched
field to be current with no stale or missing result. Report ledger updates
separately from Sheet edits and campaign JSON imports.

## Product Grounding

For a high-level description that does not require a named capability, use a
deliberately conservative baseline such as:

> Boundera helps cloud providers keep FedRAMP security evidence, validation,
> and remediation work current and easier to review.

This is not a capability inventory. Before mentioning VDR, trust-center
sharing, Security Decision Records, JSON or schema exports, connectors, KSI
evaluation, assessor workflows, automation cadence, or class/path coverage,
inspect the current FedRampGPT implementation and tests for that specific
claim.

- A matching string, dormant code path, Sheet cell, campaign message, old
  note, or marketing statement is not sufficient evidence by itself.
- Published campaign copy proves what is configured to send, not that a
  product claim is still accurate.
- Use `helps`, `supports`, or `makes it easier to` unless verified behavior
  completes the stated workflow end to end.
- Never say Boundera grants Certification or an ATO, replaces FedRAMP, an
  agency, or an independent assessor, or makes the entire process fully
  automated.
- Never imply support for every cloud, tool, connector, class, path, artifact,
  or workflow; invent customer outcomes; or use `all-in-one`, `one-click
  FedRAMP`, or `FedRAMP approved` without precise current evidence.
- If current product evidence is unavailable or ambiguous, omit the named
  capability or describe only the narrower verified behavior.

## Safety and Publication

- A Sheet edit changes only mutable draft state. It does not change JSON, a
  running campaign, an existing Deal enrollment, or a frozen delivery.
- Preview the exact Sheet-to-JSON import first:

  ```bash
  .venv/bin/python manage.py sync_general_icp_messages
  ```

- Import only when the user explicitly asks to import the reviewed Sheet copy:

  ```bash
  .venv/bin/python manage.py sync_general_icp_messages --apply
  ```

- The import updates only `shared_programs` in the two existing JSON files. It
  never creates or binds a Campaign. Campaign creation with a selected
  `message_program_key` snapshots the current JSON into an immutable SQL
  version; later Sheet or JSON changes do not mutate that campaign.
- Never mutate a published version. A copy change creates a new version;
  existing Deal enrollments stay on their frozen version.
- Never run `sync_icp_messages --pull` or `--push` for the General tab. Those
  commands manage only hidden legacy sender tabs and `sender_icps`.
- Never create, approve, reschedule, accelerate, or send a Gmail or LinkedIn
  Task from this skill.

## Validation

Before completing a Sheet write or JSON import, verify:

- the worksheet title and all eight wide headers are exact;
- ICP uses `Role | Company Size | FedRAMP Stage | Revenue Intent` and Connect
  Message is valid;
- audience metadata is consistent across its rows;
- audience and sender identities are unique and deterministic;
- Gmail subject/body pairing holds;
- placeholders are supported and render without sentinel text;
- each message fits its channel budget or has a documented reason not to;
- each message has one purpose and one primary CTA;
- authored message cells contain no Unicode em dashes or en dashes;
- product claims pass current FedRampGPT verification and FedRAMP claims pass
  the applicable official-source references; and
- a read-after-write produces the expected canonical content hash. When JSON
  import is requested, reload both JSON stores and verify the same hash; and
- every newly reviewed field is current in the internal review ledger, while
  stale hashes are reported rather than treated as reviewed.

After JSON import or campaign snapshotting, run focused message-program,
delivery, Gmail, LinkedIn, and management-command tests before reporting
completion. If the database is unavailable, do not claim a campaign snapshot
was created or activated.

## Output Contract

- For review-only work, return proposed rows grouped by Program Key and ICP.
- For an applied Sheet edit, report exact rows/cells changed, validation counts,
  the canonical hash, and the internal ledger fields recorded.
- State separately whether the mutable Sheet was edited, JSON was imported, an
  immutable SQL snapshot was created, and a Campaign was activated.
- Never imply that a Sheet edit, JSON import, or SQL snapshot sent outreach.

## Reference

- `references/icp-personas.md`: concise human-maintained persona priorities and
  attention angles; never deployed message copy.
