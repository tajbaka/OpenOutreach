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

Keep one block, ordered as the meeting should actually run:

1. **Welcome:** greet returning and new attendees naturally.
2. **Introductions:** name each Boundera attendee and their real role in one sentence.
3. **Context:** give new stakeholders the shortest useful account recap.
4. **Purpose:** explain why the session is tailored and what should be decided or validated.
5. **Questions:** ask only the highest-leverage unknowns, in conversational order.
6. **Working transitions:** place workflow questions where the relevant product or process appears.
7. **Close:** confirm reactions, testing scope, owners, missing stakeholders, decision path, and a dated next step when appropriate.

Prefer roughly five to eight real questions for a normal meeting. A longer list is acceptable only when many are explicitly conditional or embedded naturally during the working session. Do not re-ask preparation-email answers; confirm them. Do not force stakeholder, decision, or procurement questions before enough value and context have been established.

## Verification standard

A completed operation is valid only if:

- `Template` remains first and unchanged;
- the target account tab exists exactly once;
- the protected structure and canonical wording match `Template`;
- all 85 task statuses use an allowed dropdown value;
- account-specific writes appear only in the editable fields;
- no literal `[Account]` placeholder remains;
- every range changed by the operation has been read back;
- existing unrelated account tabs are unchanged.
