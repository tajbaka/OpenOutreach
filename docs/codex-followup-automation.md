# Codex Followup Automation

This is the single runbook for producing the operator Followups tabs with Codex.
It replaces the old Claude followup workflow for the parts now implemented in
this repo.

## Goal

Refresh context, draft manual followups, and write:

- `Arian - Followups`
- `Leili - Followups`
- `Athena - Followups`
- `Chuka - Followups`

The workflow never sends messages. Operators still review, copy, send, and flip
the Sent toggles in Google Sheets.

## Full Run

From `/Users/admin/Desktop/Projects/OpenOutreach`:

```bash
.venv/bin/python manage.py generate_followups \
  --sync-gmail-context \
  --sync-sheets \
  --output artifacts/followups/codex-review.json
```

Then Codex reads:

```text
artifacts/followups/codex-review.json
```

Codex writes:

```text
artifacts/followups/codex-decisions.json
```

Then apply:

```bash
.venv/bin/python manage.py generate_followups \
  --apply-json artifacts/followups/codex-decisions.json
```

## What Export Does

`generate_followups --sync-gmail-context --sync-sheets --output ...` runs:

1. `sync_gmail_context`
   - Pulls prospect Gmail threads into `crm.Message`.
   - Pulls Gmail-delivered Gemini/Meet note emails into `crm.Meeting`.
   - Writes `WorkflowRun(name="data-sync")`.

2. `sync_sheets`
   - Updates the main `People` tab from DB state/context.

3. Followup queue export
   - Reads `crm.Message`, including LinkedIn and Gmail.
   - Reads `crm.Meeting.gemini_notes_raw`.
   - Reads People-tab Outreach status.
   - Reads existing Followups tabs to preserve already-sent rows.
   - Reads `ICP Goals`.
   - Exports only leads whose conversation, meeting, CRM, Sheet status,
     eligibility bucket, or ICP goal changed since the last successful apply.
   - Retains the last applied Codex decision for unchanged leads.
   - Writes `artifacts/followups/codex-review.json`.

The review JSON includes `maintenance_required`. When `candidates` is empty
but `maintenance_required` is true, write `{ "rows": [] }` and run the apply
command so sent, disqualified, or otherwise ineligible rows are removed. When
both are empty/false, stop successfully without applying.

`--full-review` is an operator escape hatch that exports every eligible lead.
Normal daily automation must remain incremental.

## Codex Drafting Instructions

Read the review JSON and produce only valid JSON at
`artifacts/followups/codex-decisions.json`.

Output shape:

```json
{
  "rows": [
    {
      "lead_id": 123,
      "operator": "Arian",
      "status": "Replied",
      "state": "Ball on us",
      "role": "CSP",
      "priority": "HIGH",
      "convo": "One or two sentence relationship summary.",
      "draft_email": "",
      "draft_linkedin": "Short LinkedIn draft here."
    }
  ]
}
```

Allowed `state` values:

- `Ball on us`
- `Cold thread`
- `Ball on them`

Allowed `priority` values:

- `HIGH`
- `MEDIUM-HIGH`
- `MEDIUM`
- `LOW`
- `HOLD`

Use the `sheet_row` object in each candidate as the starting point, but write
the fields above in snake_case.

## Drafting Rules

- Do not draft active-in-flight rows. Leave both draft fields blank and use
  `state: "Ball on them"`.
- No apology openers: no “sorry for the delay”, “my fault”, or “apologies”.
- No em dashes.
- Keep LinkedIn drafts short, usually 3-5 sentences.
- Email drafts can be slightly longer, but only populate `draft_email` when the
  candidate has real Gmail engagement.
- For post-meeting rows, use `meeting.gemini_notes`. If meeting context is empty,
  write a conservative draft or set priority `HOLD`.
- For stale/archive posture, do not pretend the thread is warm. Reopen naturally
  or leave blank with `HOLD`.
- Use `icp_goal.goal` as strategic direction, not verbatim copy.
- Do not mention features that are not supported by the product context in the
  review JSON.
- Never include instructions to the operator inside the draft body.

## Apply Behavior

Apply mode validates the Codex JSON and calls `write_followups()`.

It:

- rebuilds only operator tabs represented in the decisions JSON,
- preserves rows where either Sent toggle was already `Yes`,
- writes dropdowns/sections/links through the existing Sheets helper,
- records `WorkflowRun(name="followup")`,
- does not send LinkedIn or Gmail messages.

## Useful Variants

Run for one operator:

```bash
.venv/bin/python manage.py generate_followups \
  --operator Arian \
  --sync-gmail-context \
  --sync-sheets \
  --output artifacts/followups/codex-review.json
```

Debug small export:

```bash
.venv/bin/python manage.py generate_followups \
  --limit 10 \
  --output artifacts/followups/codex-review.json
```

Export without visibility-only active rows:

```bash
.venv/bin/python manage.py generate_followups \
  --no-active \
  --output artifacts/followups/codex-review.json
```

Apply without recording a WorkflowRun:

```bash
.venv/bin/python manage.py generate_followups \
  --apply-json artifacts/followups/codex-decisions.json \
  --no-record-workflow
```

## Scheduling Notes

A scheduled Codex automation should run this sequence:

1. Run the export command.
2. Read `artifacts/followups/codex-review.json`.
3. Generate `artifacts/followups/codex-decisions.json`.
4. Run the apply command.
5. Report counts and any warnings from the review JSON.

Recommended cadence: daily on business mornings, or hourly only if operators
need very fast Gmail/LinkedIn reply triage. Hourly is safe because message and
meeting persistence is idempotent, but it may consume more Gmail API quota.

Do not schedule automatic message sending from this workflow.
