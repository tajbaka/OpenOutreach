# Sales Motion Sheet Format Contract

Use this contract when creating, populating, updating, or verifying a sales-motion tab.

## Protected native structure

The canonical `Template` is the authority for wording and layout. A valid native copy currently has:

- 15 numbered sales steps;
- 85 lettered task rows such as `1a`, `1b`, and `15e`;
- 15 `Account status` context blocks;
- 15 `Operating guidance` blocks;
- one blank 78-pixel spacer after every operating-guidance block;
- 49 merged ranges;
- four status conditional-format rules;
- columns A:F sized to 90, 220, 130, 390, 100, and 480 pixels;
- a 680-pixel-high merged next-call block in row 5;
- task-status dropdowns with `Open`, `Planned`, `Optional`, and `Complete`.

Native duplication is required because copying values alone loses these properties.

## Sheet topology

- `A1:F1`: sales-motion title.
- `A2:F2`: working note explaining how the account tab should be maintained.
- `A4:B4`: `NEXT CALL — QUESTIONS IN ORDER`.
- `A5:F5`: one merged call-preparation block.
- Row 7: `15-STEP SALES MOTION TRACKER`.
- Row 8 headers:
  - A: `Item`
  - B: `Step / block`
  - C: `Type`
  - D: `Task / context`
  - E: `Status`
  - F: `Account-specific detail`

Each of the 15 sections contains, in order:

1. A numbered step heading merged across B:F.
2. An `Account status` / `Context` row with D:F merged.
3. An `Operating guidance` / `Guidance` row with D:F merged.
4. A blank 78-pixel spacer row.
5. The step's lettered task rows.

## Protected versus editable content

Keep unchanged:

- all 15 step names and their order;
- every lettered task ID and task description;
- all operating-guidance wording;
- all row order, spacing, merges, dimensions, colors, dropdowns, and conditional formats;
- all column headers.

Account-specific edits belong only in:

- A1 and A2;
- the merged call block at A5;
- the D:F merged cell on each `Account status` row;
- column E on task rows;
- column F on task rows.

Within A5, native rich-text emphasis and textual indentation may distinguish main questions, scenario labels, and listening notes. Preserve its base font, size, colors, wrapping, merge, and row height.

Do not insert separate question rows beneath tasks or scatter questions through the tracker. The account-status blocks show what is known and missing; the single call block turns the most important unknowns into an ordered conversation.

## Account-status writing rules

For each step, summarize:

- confirmed progress;
- people involved and their demonstrated roles;
- evidence source or date when useful;
- the most important unresolved gap;
- the immediate next action, if one is committed.

Use compact prose or bullets. Do not fill space for its own sake. Label an inference as an inference and leave unsupported items open.

## Task-detail writing rules

Use column F to record concrete account evidence or the reason for a status. Good details include:

- who performed or owns the task;
- what was learned or agreed;
- date or source of confirmation;
- what remains to be done;
- why a task is `Optional`.

Do not restate the generic task description.

## Next-call block

Keep one block containing only:

1. **Confirm context:** one concise, evidence-backed sentence, spoken after introductions and placed immediately before the questions.
2. **Bold numbered topic-and-question lines:** `1. SHORT TOPIC TITLE — “Spoken question?”`, with a short uppercase title and the whole line bold; cover the highest-leverage unknowns in conversational order.
3. **Indented scenario bullets:** two em spaces (`U+2003`) before `• `; one conditional branch per line, with a bold scenario label through its colon and regular quoted follow-up text.
4. **Deeper-indented listening/positioning notes:** three em spaces before a regular-weight `Listen →` note; concise signals and what they imply for the pitch.

This Xerox-style hierarchy is the default for new or revised account call blocks. Read [discovery-question-design.md](discovery-question-design.md) for the example and native emphasis rules. Separate question groups with blank lines. Use literal indentation and native rich text, not visible Markdown markers. Do not include introductions, opening/setup, how-to-use instructions, a separate purpose/recap, meeting metadata, pacing, preparation checklists, or a closing script. Relevant logistics and preparation belong in existing account-status or task-detail fields. Replace inherited Template A5 prose when populating an account; leave Template itself unchanged.

Prefer roughly five to eight main questions. Select conditional follow-ups from the buyer’s answers; do not ask every branch. Confirm preparation-email answers rather than repeating them. Put the next-participant and dated next-action prompt in the final group when appropriate; do not force approval, procurement, budget, or signature questions forward.

## Verification standard

A completed operation is valid only if:

- `Template` remains first and unchanged;
- the target account tab exists exactly once;
- the protected structure and canonical wording match `Template`;
- all 85 task statuses use an allowed dropdown value;
- account-specific writes appear only in the editable fields;
- no literal `[Account]` placeholder remains;
- every range changed by the operation has been read back;
- when A5 is created or revised, it has the context-only lead-in, numbered uppercase topic titles followed by an em dash and the spoken question, consistent scenario/listening indentation, and native rich-text emphasis matching the hierarchy above; read back the text and rich-text runs and check visual fit;
- existing unrelated account tabs are unchanged.
