---
name: boundera-sales-motion
description: Create, populate, update, or verify account-specific Boundera sales-motion tabs and prepare stage-appropriate discovery questions for those opportunities. Use for 15-step opportunity tracking, call preparation, stakeholder mapping, and current FedRAMP Marketplace grounding for software-vendor and CSP accounts. Do not use for ordinary sales-message drafting.
---

# Boundera Sales Motion

Maintain one consistent account tracker without flattening, rebuilding, or casually rewriting the framework.

## Scope and authority

- If the user asks to discuss, review, or plan a motion, stay read-only.
- A direct request to create or update an account tab authorizes that specific Sheet mutation; it does not authorize changes to other tabs or source systems.
- Never modify `Template`. Never modify `Ramp` unless the user explicitly names it.
- Never overwrite an existing account tab. Inspect it and update it only when requested.
- Treat the live `Template` tab as the source of truth. Do not reconstruct the tracker from memory, Markdown, or copied cell values.

## Canonical workbook

- Spreadsheet ID: `15di85z9AWwXPoShg1MNgezjcIMV4OivRihDFpikPLaQ`
- Template tab: `Template`, which must remain first.
- OpenOutreach credentials: `secrets/sheets-service-account.json`
- Python runtime: `.venv/bin/python`

Read [references/format-contract.md](references/format-contract.md) before creating, populating, or structurally verifying a tab. It defines the protected framework, status meanings, account-specific fields, and next-call block.

Read [references/discovery-question-design.md](references/discovery-question-design.md) whenever preparing, revising, or discussing call questions. It defines how to group a small set of main questions with selective follow-ups, listening signals, value-proposition implications, and stakeholder discovery without turning the conversation into a qualification script.

For conversation-level reasoning, read the repo's concise [sales-motion summary](../../docs/sales-motion-summary.md). When exact task IDs or operating guidance matter, read the [detailed 15-step framework](../../docs/sales-motion-framework.md). The timestamped [video transcript reconstruction](../../docs/sales-motion-video-transcript.md) explains how the source conversation maps to the framework.

## Workflow

### 1. Determine the requested operation

- **Create:** make a new account tab from `Template`.
- **Populate/update:** add evidence-backed account context, statuses, task details, or the next-call plan to an existing tab.
- **Verify:** inspect structure and content without changing the Sheet.
- **Discuss:** reason about the sales motion without changing the Sheet.

Do not turn a discussion into a Sheet write.

### 2. Ground the account before populating it

Use the newest available evidence in this order:

1. The user's current message and pasted material.
2. Actual prospect conversations, emails, and meeting notes the user placed in scope and that are accessible.
3. OpenOutreach lead, deal, message, and local-note records.
4. Existing account-tab content.

Separate confirmed facts, reasonable interpretations, and open questions. Do not mark a task `Complete` from an inference. Do not invent stakeholders, authority, pain, timing, procurement, or next steps.

When the user asks for context retrieval from OpenOutreach, Gmail, Gemini, or another source, use only the relevant accessible source. A missing connector or record is an unknown, not evidence that the event did not happen.

#### Ground software vendors in the current FedRAMP Marketplace

When the account is a cloud software vendor or CSP, check the live, official
[FedRAMP Marketplace](https://www.fedramp.gov/marketplace/products/) before
writing discovery questions or updating its motion. This lookup is not a
default buyer-status check for a 3PAO/assessor, advisor, channel partner, or
other partnership account.

- Search by both company and product name. A company can have multiple cloud
  service offerings; do not transfer one offering's status to another.
- Capture the exact offering name, package ID, phase, status, certification
  type, path, class, certified-since date, and authorization count when shown.
- Treat the live Marketplace as current evidence and record the product-page
  URL with the supporting context. If no exact match exists, preserve the
  status as unknown rather than inferring it from company copy or an old note.
- Put already-known Marketplace facts into the Confirm context line or
  account-status fields. Replace questions that merely ask for those facts
  with confirmation and scoping questions.
- For an already-certified Rev5 offering, do not ask as though the vendor is
  beginning FedRAMP or choosing its first path and class. Ask whether the
  evaluation concerns ongoing operation of that package, another offering,
  future 20x work, or some combination; then explore the relevant operational
  burden at the opportunity's current step.
- Marketplace status does not prove which product is in scope, current pain,
  urgency, budget, assessor, procurement path, stakeholder authority, or
  Boundera fit. Discover those separately and at the appropriate sales step.

### 3. Create safely by native duplication

Run a read-only preflight first:

```bash
.venv/bin/python skills/boundera-sales-motion/scripts/clone_sales_motion.py "ACCOUNT NAME" --dry-run
```

If preflight passes and the user asked to create the tab, run:

```bash
.venv/bin/python skills/boundera-sales-motion/scripts/clone_sales_motion.py "ACCOUNT NAME"
```

The helper must duplicate the native sheet, append the account tab after existing tabs, replace the account placeholder, and preserve merges, row heights, column widths, dropdowns, and conditional formatting.

If the account tab already exists, stop. Do not rename, replace, delete, or create a numbered duplicate.

### 4. Populate only the account layer

Preserve all step headings, task labels, operating guidance, spacer rows, merges, formatting, dropdowns, and conditional-format rules.

Update only:

- the account title and working note;
- the single next-call block at the top;
- each step's `Account status` context;
- task `Status` cells;
- task `Account-specific detail` cells.

Use batched, range-precise writes. Read back the edited ranges immediately after writing. Do not add row-by-row discovery-question sections; keep the ordered call plan in the one merged block at the top.

### 5. Apply evidence-based status semantics

- `Open`: not completed or not yet known.
- `Planned`: explicitly scheduled, committed, or selected as a next action.
- `Optional`: intentionally unnecessary for this opportunity; state why in the account detail.
- `Complete`: confirmed by evidence already in the account history.

Past opportunities that can no longer follow a task are not automatically `Complete`. Use `Optional` with a concise reason when the task is no longer applicable.

### 6. Build the next-call block strategically

Start with **Confirm context:** followed by one concise, evidence-backed sentence to say after the spoken introductions. Place the numbered questions immediately after that line. The block contains only this context confirmation and the grouped questions; omit welcome/intro scripts, setup, how-to-use instructions, separate purpose or recap sections, meeting metadata, pacing, preparation checklists, and closing scripts. Keep relevant logistics and preparation obligations in the account-status or task-detail fields instead.

Use the Xerox-style hierarchy in [discovery-question-design.md](references/discovery-question-design.md) as the default for new or revised next-call blocks: a bold main line in the form `1. SHORT TOPIC TITLE — “Spoken question?”`, separate indented scenario bullets with bold labels and regular follow-up text, then a deeper-indented, regular-weight `Listen →` note. Use a short uppercase topic title that makes each group easy to scan, and separate groups with a blank line. Preserve the native block dimensions and base formatting; apply native rich text within A5. This is a reusable layout preference, not a requirement to copy Xerox’s questions or sales stage, and does not authorize rewriting other account tabs.

Tailor the questions to the actual participants and sales step. Ask a main question, listen, then select one or two branches for evidence, current process, friction/consequence, ownership, or desired outcome. Do not re-ask preparation-email answers. Put the appropriate next-participant and next-action question in the final group without pulling approval, procurement, or signature questions forward.

### 7. Verify before reporting success

After any account write, run:

```bash
.venv/bin/python skills/boundera-sales-motion/scripts/clone_sales_motion.py "ACCOUNT NAME" --verify-only
```

The helper verifies structure and statuses, not A5 typography. When A5 changes, also read back its text and rich-text runs and check the topic/question hierarchy, indentation, and visual fit against the format contract. Read back every other range changed in the current operation. Report success only when verification passes. Link directly to the account tab using its returned sheet ID.

## Stop conditions

Stop without mutating when:

- the canonical `Template` is missing, not first, or fails its structural contract;
- credentials cannot open the workbook;
- a requested account tab already exists and the user asked to create it;
- the account name is invalid for a Google Sheets tab;
- the requested content would require guessing material sales facts.

Preserve partial unknowns as `Open` and explain what information is still needed.
