# Active Accounts next-step review

This is a Codex contextual review, not a Python decision engine. It uses the
source-reading and identity rules in `docs/actions-workflow.md`, then updates
only the existing `Next step` cell values in manual `Active Accounts`, then
sorts complete account rows by their existing `Stage` on every run, then enforces
an 84-pixel minimum height for each account row without shrinking taller rows.
Stage values remain operator-owned; sorting never changes an account's stage. The daily
cadence is 09:00 America/Toronto on the designated receiving computer, after
Actions in the same runner. One-time runs do not install or enable a timer.

## Read and reason

Read `skills/openoutreach-google-sheets/SKILL.md`, `skills/gmail-read/SKILL.md`,
`skills/gmail-oauth/SKILL.md` and `skills/granola-read/SKILL.md` from this checkout.
Verify the configured CRM workbook and Arian mailbox
using existing auth. Use stored LinkedIn, live Gmail including Sent, full relevant
Granola transcripts, and relevant stored-only Gemini notes. Never run importers,
CRM reconciliation, publishers, workers, or send/draft tools to perform this review.

Read every current manual row, its Notes, owner, stage, main contacts and hidden
IDs. Bound Opportunity/Account IDs are strong identity anchors, not admission
requirements: unbound manual rows are valid. Verify their exact contact addresses
or explicit LinkedIn identity. Do not add a row, invent an identity, merge people
by company/name, or mutate CRM to fill blanks. Hold ambiguous row matches.

For each row, reconstruct the latest dated relationship context, extending back
to the meeting or thread that established the current next step. Fetch subsequent
messages in all verified channels, not just the most recent meeting summary.
Read current Actions as a pointer, not as authoritative or exhaustive evidence.
Distinguish completed work, Arian's outstanding commitments, counterpart-owned
steps, scheduling contingencies, explicit deferrals and optional suggestions.
A different-channel answer or actual delivery can resolve an earlier promise.
A meeting invite, acknowledgement or elapsed time alone cannot.

Write one concise account-level next move (normally one or two sentences).
Use "Await [person / decision]" when the ball is genuinely with the counterpart;
include the actual dependency and the next conditional action where useful.
An account can be awaiting a counterpart while Actions holds a suggested future
review of that dependency. Keep this ledger focused on the account's current
state and next move; the Actions review owns concrete follow-up timing. Do not
copy every Actions row here or write its suggested date into Next step due.
The Actions-only closed-account exclusion does not remove or skip manual ledger
rows in this review. Continue to preserve their identity/stage and update only
Next step where the evidence supports a change.
Respect Closed Won as a customer relationship and Closed Lost as deferred/lost:
do not revive stale sales tasks or invent follow-up dates to fill the cell.
Operator Notes and explicit handled/deferred decisions outrank mechanical aging.
For Stage `Partnership`, follow `docs/partnership-workflow.md`: focus the next
move on actual introductions, enablement, joint work and partner dependencies.
Keep a partner's service work (such as Schellman assessing Boundera) distinct.
Do not infer buyer evaluation, pilot, procurement, close probability or revenue
from a partner sandbox or expected referral volume. The partnership tracker is
a context pointer; verify underlying sources. Partners remain included here and
in People. This review still writes only Next step values, order and height.
When a prior step is demonstrably superseded but completion of the newer step
is unrecorded, a concise verification step is appropriate; explicitly say what
is unknown rather than declaring it missed or complete. When identity or the
proposed next move itself is uncertain, preserve the
existing cell and report the hold in the private receipt. Never blank a cell just
because no new action was discovered. Keep unchanged accurate next steps intact.

## Only one writable column

Resolve the live `Active Accounts` sheet ID and locate the unique exact `Next step`
header (currently E). Preserve all other cell values, formulas, native formatting,
notes, validation and IDs by account, including `Next step due`. Preserve all
rows; the row-order exception is the stage sort below, and the sole row-height
exception is the 84-pixel minimum below. Do not update
stale due dates, stage or Notes incidentally. Skip formula-owned Next step cells.
Do not change a different tab as part of this workflow.

Prepare a private per-row plan with full before-row values and proposed Next step,
source IDs/dates/quotes or transcript turns, fulfillment reasoning and confidence.
Use `crm_refresh_lock()` only for final preflight/write/readback. Re-read the rows
and relevant source activity before writing; if anything affecting the decision
changed, re-review it. Require the full row to match the reviewed before-state;
never trust an old row number after a human insertion/reorder. Use bound IDs or
an exact unique full-row identity; conflicting/duplicate unbound rows are held.

Write only changed Next step cells with literal string values. Do not clear or
rewrite a rectangle covering adjacent columns. Read back the changed cells and
verify every other user-entered value, formula and relevant native property is
unchanged by account identity after the authorized sort, except for the explicit
minimum-height increases. Google Sheets does not
provide a human-edit compare-and-swap here; record and report any detected race,
never overwrite again to force a match.

## Sort by existing Stage after every review

The user authorized this row-order exception on 2026-10-29, including runs with
no Next step changes. Locate the unique exact `Stage` header (currently C).
Use the existing consistent Stage dropdown order: Prospecting, Discovery,
Demo Planning, Evaluation, Sandbox/Pilot, Commercial, Procurement/Legal,
Closed Won, Expansion, Closed Lost, Partnership. Partnership forms a separate
group after the buyer stages. Read the live validation before sorting;
if the configured order changes, use its unambiguous ordered list and record it.
Keep equal-stage rows in their previous relative order. Put unknown/nonblank
stages and then blank stages after recognized stages, preserving their relative
order and reporting unknown labels. Do not rewrite Stage values or infer stages
from conversation evidence for this sort.

Sort all existing account rows, including unbound and closed rows, by moving
complete native rows across the full sheet width, including hidden IDs, Notes,
dates, cell notes, validation, formatting and row heights. Keep the header,
sheet identity, filters, frozen panes and hidden-column settings intact. Do not
sort Stage alone, rebuild the tab, add/delete accounts, or change CRM records.
Keep empty slots outside the account list; hold unusual mixed/merged structures
for review instead of moving unrelated content. Resolve accounts by unique bound
IDs or exact unique full-row identities; never use old row numbers after sorting.

Under `crm_refresh_lock()`, require a fresh matching full before-state before
Next step edits and sorting. Write changed Next step cells before moving rows,
or resolve their current indices afresh. Skip moves when already sorted. API
readback must verify stage order, stable ties, the exact row multiset and each
account's protected cell values/formulas/native properties and expected height
(`max(before_height, 84)` after minimum-height enforcement). Verify
Actions and LinkedIn Pending unchanged by this review. Save before/after order
in the private receipt. A sort-only run is not a fresh contextual review and
must not advance its full-review watermark.

## Minimum account-row height after every review

The user authorized this presentation rule on 2026-11-16: every material
`Active Accounts` entry, including unbound and closed accounts, has a minimum
row height of **84 pixels**. This is a floor, not a fixed height or cap. Preserve
every row already taller than 84 pixels; longer entries may remain taller.
Apply the floor even when no Next step values or row order change, and use it
for new entries when an account addition is separately authorized.

After sorting, resolve each account's current row index and read its native
`rowMetadata.pixelSize`. Under the same `crm_refresh_lock()` and fresh-state
preflight, use Sheets `updateDimensionProperties` on only rows below the floor,
with `fields: "pixelSize"` and `pixelSize: 84`. Do not resize the header, empty
rows or other tabs, and do not change row visibility, cell formatting, wrapping,
values, notes, validation, IDs or widths. Never set all rows to a uniform height
or shrink a taller row. A rerun on compliant rows must require no height writes.

Read back by account identity and verify each height equals
`max(before_height, 84)`, while all other protected properties are preserved.
Record the minimum and per-account before/after heights in the private receipt.
A formatting-only run does not count as a fresh contextual review and must not
advance its full-review watermark. Preview mode records proposed height changes
without writing them.

Save private before/source/decision/readback receipts in a new ignored run
folder under `artifacts/followups/YYYY-MM-DD/`, separate from older receipts.
Report changed accounts, why their next move changed, and evidence/coverage holds.
On scheduled runs, stay quiet for unchanged/non-actionable results and report
meaningful changes or failures. Do not send, draft, or modify CRM/other tabs.
