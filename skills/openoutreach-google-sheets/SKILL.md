---
name: openoutreach-google-sheets
description: Read and update Google Sheets through OpenOutreach's existing Google API authentication. Use for requests such as "update XYZ sheet" or "add Xerox to Active Accounts", including the FedRampGPT CRM, Sales Motion and General ICP Messages workbooks. Verify changes through API readback without opening Sheets in a browser unless the user explicitly requests it.
---

# OpenOutreach Google Sheets

Use the repository's existing authenticated Google Sheets API for Sheet reads,
updates, row additions and verification. A Sheet URL or an already-open browser
tab identifies context; it does not select browser automation.

## Route and authenticate

- Work from the OpenOutreach repo root with `.venv/bin/python`.
- Read `linkedin/conf.py` and the relevant helpers in
  `linkedin/notifications/sheets.py` before using them. Resolve credentials from
  `GOOGLE_SHEETS_CREDENTIALS_PATH` and the CRM workbook from `GOOGLE_SHEETS_ID`;
  never print or copy credential contents or commit the credential file.
- For the configured FedRampGPT CRM workbook, use the existing connection:

  ```python
  from linkedin import conf
  from linkedin.notifications import sheets

  spreadsheet = sheets._gspread_client()
  assert spreadsheet.id == conf.GOOGLE_SHEETS_ID
  metadata = spreadsheet.fetch_sheet_metadata()
  ```

- For another explicitly identified workbook, use
  `sheets._gspread_authorized_client().open_by_key(spreadsheet_id)` when that
  existing identity has access. Keep the CRM configuration unchanged.
- Account-specific Sales Motion tabs also require
  `../boundera-sales-motion/SKILL.md` and its canonical workbook/helper.
  General ICP Messages authoring requires `../boundera-icp-messages/SKILL.md`.
  This skill controls authentication and API verification; those skills retain
  their task-specific structure, evidence and review rules.
- Resolve the workbook from its supplied URL/ID, existing configuration or the
  relevant specialized skill. Confirm its live title and ID. If a name remains
  ambiguous, use available read-only API/connector discovery or ask for the
  missing identifier. Do not open the browser to find it.
- Report an actual authentication or access blocker. Do not silently use a
  different account, create a new login, broaden scopes or switch to browser
  editing. Honor an explicitly requested alternative route.

## Make the requested edit

1. Read live metadata, exact tab names, headers and a bounded relevant range.
   Inspect `userEnteredValue`, formulas, validation and native formatting when
   they affect the edit. Check for an existing record before adding one.
2. Ground values in the user's current instructions and relevant current
   evidence. Do not invent a stage, date, stakeholder, commitment or stable ID.
3. Re-read the target immediately before a write. Update only the authorized
   cells through the Sheets API, preserving formulas, dropdowns, chips, notes,
   formatting, filters, hidden identifiers and unrelated operator values.
   For additions, preserve native row structure and place the record after the
   last populated row; do not overwrite gaps in an existing table.
4. Read the written cells back through the API. Compare values/formulas, verify
   validation and relevant formats, and confirm adjacent/source records were
   preserved. API readback is sufficient for routine data updates; do not open
   Sheets for a generic visual-QA requirement. Inspect API formatting metadata
   when layout matters, and describe any visual aspect that remains unverified.
5. Report completion only after successful readback, with the verified workbook
   or tab link. After an uncertain write, reconcile by reading before retrying
   so additions are not duplicated.

## CRM boundaries

- `Active Accounts` is an operator-owned manual ledger. A direct user request
  authorizes its specified edit. The daily Codex review in
  `docs/active-account-next-steps-workflow.md` may update only Next step cell
  values and, as authorized on 2026-10-29, stably sort complete native rows by
  their existing Stage after every review. Stage values remain manual. Preserve
  all protected data and native properties by account identity. Never invoke a
  refresh, conversion, rebuild or publisher to implement this review.
- When adding an account, bind hidden Opportunity/Account IDs only to one
  verified existing CRM identity. An unbound manual row is valid and is included
  in the contextual Next step review. Do not create database identities merely to
  fill hidden cells or change another account's IDs.
- Contextual Actions (formerly Followups) uses `docs/actions-workflow.md`;
  preserve its sheet ID and hidden Followup UUIDs. Never run the retired generated
  Actions publisher or draft queue. People IDs/baselines retain the CRM contract.
  A Sheet edit does not authorize running publishers, writing
  the database, syncing other tabs, importing message programs, changing
  campaigns or sending messages. Existing scheduled handling remains intact.

## Browser boundary

Only open, show or edit Sheets through a browser when the user explicitly asks
for that UI action. Keep routine reads, edits and verification in the API, even
if a generic Google Sheets skill recommends a browser render. Do not add a
browser step just to prove that an API update succeeded.
