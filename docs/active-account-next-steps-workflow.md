# Active Accounts next-step review

This is a Codex contextual review, not a Python decision engine. It uses the
source-reading and identity rules in `docs/actions-workflow.md`, then updates
only the existing `Next step` column in manual `Active Accounts`. The daily
cadence is 09:00 America/Toronto on the designated receiving computer, after
Actions in the same runner. One-time runs do not install or enable a timer.

## Read and reason

Read `skills/openoutreach-google-sheets/SKILL.md`, the Gmail read/OAuth skills,
and the Granola read skill. Verify the configured CRM workbook and Arian mailbox
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
Respect Closed Won as a customer relationship and Closed Lost as deferred/lost:
do not revive stale sales tasks or invent follow-up dates to fill the cell.
Operator Notes and explicit handled/deferred decisions outrank mechanical aging.
When a prior step is demonstrably superseded but completion of the newer step
is unrecorded, a concise verification step is appropriate; explicitly say what
is unknown rather than declaring it missed or complete. When identity or the
proposed next move itself is uncertain, preserve the
existing cell and report the hold in the private receipt. Never blank a cell just
because no new action was discovered. Keep unchanged accurate next steps intact.

## Only one writable column

Resolve the live `Active Accounts` sheet ID and locate the unique exact `Next step`
header (currently E). Preserve all other cell values, formulas, native formatting,
notes, validation, rows, order and IDs, including `Next step due`. Do not update
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
unchanged. Google Sheets does not provide a human-edit compare-and-swap here;
record and report any detected race, never overwrite again to force a match.

Save private before/source/decision/readback receipts in a new ignored run
folder under `artifacts/followups/YYYY-MM-DD/`, separate from older receipts.
Report changed accounts, why their next move changed, and evidence/coverage holds.
On scheduled runs, stay quiet for unchanged/non-actionable results and report
meaningful changes or failures. Do not send, draft, or modify CRM/other tabs.
