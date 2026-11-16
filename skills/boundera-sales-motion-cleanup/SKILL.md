---
name: boundera-sales-motion-cleanup
description: "Clean up Sales Motions using FedRampGPT's recorded stages: hide verified Closed Won/Closed Lost tabs, keep Partnership tabs visible, and move them to the far left after Template. Use for sales-motion cleanup, closed-account removal from view, or partnership tab ordering. Preserves tab contents and IDs; does not change CRM stages or run the Actions review."
---

# Boundera Sales Motion Cleanup

Keep the visible Sales Motions workbook focused on open opportunities. The
user's removal preference is **hide the existing tab in the current workbook**
(2026-11-16). Preserve its native sheet ID, contents and formatting. Do not delete,
clear, rename, move to another workbook, or mark its tasks complete.

The user also wants **Partnership** account tabs retained visibly and grouped at
the far left, immediately after `Template` (2026-11-16). This supersedes the earlier
same-day transition that hid the historical partner tabs. Run this stable tab sort on every
cleanup, including runs with no newly closed accounts. Partnership relationships
use their own referral/enablement tracker; existing Sales Motion tabs may remain
as history. This cleanup changes visibility/order only, not their buyer framework
or partner-tracker contents.

## Scope and access

- A request to create/edit this skill, inspect candidates, or preview cleanup is
  read-only against Google Sheets. A request to run/apply the cleanup authorizes
  hiding eligible closed tabs and keeping verified Partnership tabs visible and
  sorted; do not ask
  again for the same authorization. An explicit request to sort Partnership tabs
  authorizes their visible grouping without requiring a closed-account cleanup.
- Use the repository's existing Google authentication and Sheets API. Read
  [OpenOutreach Google Sheets](../openoutreach-google-sheets/SKILL.md) and the
  scope/protection rules in
  [Boundera Sales Motion](../boundera-sales-motion/SKILL.md).
- Work from the OpenOutreach checkout with `.venv/bin/python`. Resolve credentials
  from `linkedin.conf.GOOGLE_SHEETS_CREDENTIALS_PATH`; never print their contents.
  Use `sheets._gspread_client()` for the configured CRM workbook and
  `sheets._gspread_authorized_client().open_by_key(...)` for Sales Motions, from
  `linkedin.notifications.sheets`.
- Confirm both live workbook titles and IDs:
  - **FedRampGPT:** `1eT1S3iNmj3fKrZWrZ7iHauCkkS98GXnO-kruDa-JvBk`;
    the configured `GOOGLE_SHEETS_ID` must identify this workbook.
  - **Sales Motions:** `15di85z9AWwXPoShg1MNgezjcIMV4OivRihDFpikPLaQ`.
  If configuration differs, resolve the discrepancy without changing it or
  silently selecting another workbook/account.
- Never change `Template`; it must remain visible and first. `Ramp` remains
  protected under the existing sales-motion skill unless the user explicitly
  names it for cleanup. Preserve their native identities as well as their names.
  Other tabs moving left may shift Ramp's numeric index; that incidental shift
  is allowed, while its contents, visibility and relative order among the
  remaining tabs stay unchanged.
- FedRampGPT is a read-only source for this workflow. Do not edit/sort its ledger,
  alter stages, run publishers or CRM reviews, change campaigns, send messages,
  or create a schedule. Only verified Partnership tabs may be automatically
  unhidden under this preference; reopened buyer tabs require separate direction.

## Identify eligible accounts

Read fresh metadata for both workbooks, including hidden tabs. Discover the live
`Active Accounts` headers and read all populated rows, including Notes, Next step,
contacts and hidden Opportunity/Account IDs. Current headers are `Company`,
`Owner`, `Stage`, `Main point of contact`, `Next step`, `Next step due`, `Notes`,
`Opportunity ID`, and `Account ID`; locate them by name rather than fixed letters.
Missing or duplicate required identity/stage headers are a blocker.

Use the operator's recorded **Closed Won** and **Closed Lost** Stage values as
the closure signal. Trim whitespace and compare case-insensitively; do not expand
the closed-stage set to Failed, Disqualified, Deferred, Nurture, or blank values.
There is currently no separate Closed Accounts source tab; discover the live
schema rather than assuming such a tab exists. If the operator has since moved
the ledger, verify the authoritative replacement before using it.

Read **Partnership** from the same live `Stage` column, with the same whitespace
and case normalization. It is an ongoing relationship category, not a closed
buyer stage. Do not classify accounts as Partnership from company type, names,
referral counts, or prose alone. The separate Partnerships tracker's developing,
active or dormant labels do not replace the Active Accounts Stage. Keep partner
history without creating/rebuilding tabs or changing CRM, People, Actions,
referral records, or assessor work.

- An absent Active Accounts row, silence, old dates, completed tracker tasks,
  or FedRAMP Marketplace status does not establish closure.
- Notes describing a loss while Stage remains open are a **stage/context
  conflict**, not automatic permission to hide or change Stage. Report the
  discrepancy. Likewise hold a closed row with explicit newer reopening or
  expansion context. Ordinary historical sales notes do not overrule a clear
  current closed stage.
- Inspect all rows for the same verified account. If another open opportunity
  exists, keep an account-wide tracker visible. A tab proven to belong solely
  to the closed opportunity may qualify; uncertain scope is held.
- Read recorded Sheet context for this cleanup. Broader Gmail, Granola, LinkedIn,
  database or Marketplace investigation is unnecessary unless separately
  requested to resolve a specific ambiguity; do not infer or update CRM stages.

## Match the exact sales-motion tab

Inventory native sheet IDs, titles, order and hidden flags. Use stable
Account/Opportunity IDs when recorded on both sides, or a direct Sales Motions
workbook URL with an exact `gid` in the source row. Verify the pointed-to tab's
title and account context as well; a stale link or conflicting ID is held.

Many valid tracker tabs have no CRM IDs. For those, require an exact unique
company/tab-title match (case and whitespace normalization only), its matching
`<Company> Sales Motion` title in A1, and corroborating account context such as a
shared named contact, exact company domain, or specific opportunity details.
Inspect the account-specific fields rather than the generic Template prose.
Unbound ledger rows remain eligible when this identity is verified.

Do not use fuzzy matching, strip legal suffixes, equate subsidiaries, or select a
tab using company/name similarity alone. An alias is valid only with explicit
Sheet evidence linking the same entity. Hold multiple possible tabs, conflicting
IDs, shared trackers and unresolved account-versus-opportunity scope. Skip
non-account/framework tabs. Apply these same identity checks to Partnership
matches. A closed or Partnership account with no matching tab is a no-op; do not
create a Sales Motion merely because the account has a partner relationship.

Build a private plan under ignored `outputs/sales-motion-cleanup/<run>/` with:

- the source row, exact recorded stage, IDs, relevant context and source URL;
- target workbook ID, native sheet ID, title and prior hidden flag;
- identity evidence, eligibility decision and any conflict/protection reason;
- the exact change `hidden: false -> true`, or already-hidden/no-match/held.
- any Partnership visibility restoration `hidden: true -> false`.
- the verified Partnership sheet IDs and complete before/desired tab order.

Deduplicate by native target sheet ID. Do not save live account data in the skill
or commit receipts. For a preview, report the plan and stop before writing.

## Sort Partnership tabs

Keep `Template` at index 0. Build the desired full native tab order as:

1. Template.
2. Verified Partnership tabs, in their existing relative tab order.
3. Every remaining tab, in its existing relative tab order.

Include hidden tabs in the inventory and ordering calculation. Keep verified
Partnership tabs visible: restore `hidden: false` when needed, unless a newer
explicit user instruction says to keep a specific tab hidden. Preserve all
other visibility flags except reviewed closed-account hides. Do not
move a protected or ambiguous tab into the Partnership group. If multiple source
rows disagree about whether a tab represents a partnership or buyer opportunity,
resolve its scope or hold it instead of classifying by company alone.

Use native `updateSheetProperties` with an exact sheet ID and `fields: "index"`.
Walk the verified partners in existing order, assigning indices 1, 2, and so on.
Maintain a simulated current ID list after every request. Each necessary move
is leftward; skip tabs already at their destination. The API shifts intervening
tabs automatically. Do not send index changes for every unrelated tab, sort
alphabetically, or sort the entire workbook by CRM pipeline stage. If no verified
Partnership tabs exist or their order already matches, the sort is a no-op.

## Apply and verify

For an authorized cleanup, complete the plan before any mutation. Snapshot the
target tabs' native data and properties through the API: user-entered values and
formulas, notes, rich text, validation, formats, merges, conditional formatting,
dimensions, hidden rows/columns and other existing sheet metadata. Capture the
workbook's tab identities, order, visibility and retained-tab contents/properties
for preservation checks. Use `fetch_sheet_metadata` with `includeGridData=true`,
chunking requested ranges if needed; a values-only export is not a full snapshot.

Immediately before writing, re-read the relevant source rows and tab state.
Re-resolve rows by identity, not old row number. If eligibility, identity or tab
content changed, re-review that candidate. A source row reorder alone is not a
changed account: compare the relevant values/formulas and notes by identity,
then refresh row links. Skip redundant hides and index moves. Confirm the
protected tabs and at least one visible sheet remain.

Submit only native `updateSheetProperties` requests to **Sales Motions**, each
with an explicit sheet ID and the narrow `hidden` or `index` field mask. The
hide requests are:

```python
requests = [
    {
        "updateSheetProperties": {
            "properties": {"sheetId": sheet_id, "hidden": True},
            "fields": "hidden",
        }
    }
    for sheet_id in verified_visible_sheet_ids
]
```

Append any verified Partnership visibility restorations with `fields: "hidden"`
and `hidden: False`, plus the index requests described above. Do not call
the template-cloning helper or rebuild a tracker. Do not use `deleteSheet`, clear
cells, reorder tabs beyond the stable Partnership grouping, or write to a whole
properties mask.

After appending the index requests and verifying the complete authorized plan,
submit once with `sales_motions.batch_update({"requests": requests})` if the
request list is nonempty.

Read back through the API and verify:

- Each selected closed-account sheet ID still exists with the same title and is
  hidden. Each verified Partnership tab is visible unless explicitly excepted.
- Tab IDs and all preserved content/native properties match the before state;
  only planned hidden flags and indices changed. Compare native content by sheet
  ID, not array position. Ignore recalculated effective values when comparing
  formulas; preserve user-entered formulas exactly.
- Full native tab order equals the plan: Template first, verified Partnership
  tabs next in stable order, and all remaining tabs in their original relative
  order. All visibility flags outside the reviewed visibility plan are unchanged.
- Template remains visible and first; Ramp and every unrelated tab retain their
  prior visibility unless Ramp was explicitly authorized in this run.
- The source closure/identity evidence still matches. If concurrent edits are
  detected, report them and stop further writes; do not overwrite user changes
  to force verification to pass. Sheets has no human-edit compare-and-swap here.
- Repeating candidate discovery proposes no additional hide, Partnership
  visibility restoration or move for the tabs just handled. Already-hidden or
  already-visible/ordered tabs are successful
  no-ops, never duplicate work.

After an uncertain API response, read the actual hidden flags and full tab order
before considering another request. Recompute moves from observed state rather
than replaying old index requests. Record confirmed, not-applied and unresolved
outcomes separately.
Save the plan and verification receipt. Report the hidden accounts with their
tab links, the visible Partnership group/order, already-hidden/no-match counts, and
material held cases. Distinguish tabs hidden from tabs moved; report a no-op
honestly. No eligible visibility or order changes is a valid completed cleanup.
