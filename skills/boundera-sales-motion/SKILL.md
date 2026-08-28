---
name: boundera-sales-motion
description: Create, populate, update, or verify account-specific Boundera sales-motion tabs by duplicating the canonical Template tab in the Sales Motion Google Sheet. Use when the user asks to create, clone, map, organize, or maintain a 15-step sales-motion tracker for an opportunity. Do not use for ordinary sales-message drafting that does not involve this tracker.
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

Tailor the block to the actual participants and meeting stage. Include:

1. A brief welcome and specific introductions for Boundera and new stakeholders.
2. A concise account recap for anyone joining late.
3. The meeting purpose and expected outcome.
4. A small, ordered set of unanswered questions that advances the relevant sales steps.
5. Natural transitions into the demo, sandbox, or working session.
6. A close that confirms testing, ownership, stakeholders, decision process, and the next meeting when relevant.

Do not repeat questions already answered in a preparation email or earlier call. Confirm the answer instead. Keep conditional questions visibly conditional, and avoid making the meeting feel like an interrogation.

### 7. Verify before reporting success

After any account write, run:

```bash
.venv/bin/python skills/boundera-sales-motion/scripts/clone_sales_motion.py "ACCOUNT NAME" --verify-only
```

Also read back every range changed in the current operation. Report success only when verification passes. Link directly to the account tab using its returned sheet ID.

## Stop conditions

Stop without mutating when:

- the canonical `Template` is missing, not first, or fails its structural contract;
- credentials cannot open the workbook;
- a requested account tab already exists and the user asked to create it;
- the account name is invalid for a Google Sheets tab;
- the requested content would require guessing material sales facts.

Preserve partial unknowns as `Open` and explain what information is still needed.
