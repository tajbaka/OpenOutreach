# Daily contextual CRM review (Claude Code runner)

You are running unattended from the root of an authorized OpenOutreach
checkout, started by `scripts/run_claude_crm_review.ps1` (Windows) or
`scripts/run_claude_crm_review.sh` (macOS/Linux). Nobody is watching
this session: do not ask questions. Hold anything ambiguous and report it.

MODE: {{MODE}}

## Tools in this session

- Run Python only as `{{PYTHON}}` (exactly this path, forward slashes) from
  the repository root. Every other shell command is blocked; use the Read,
  Grep and Glob tools to inspect files.
- Write files only under `artifacts/followups/` (private receipts) and
  `artifacts/claude-review/`. Do not edit tracked repository files.
- Put full private transcripts in the system temporary directory, as the
  workflow docs require.
- The skills the docs mention are plain files in this repository: read
  `skills/openoutreach-google-sheets/SKILL.md`, `skills/gmail-read/SKILL.md`,
  `skills/gmail-oauth/SKILL.md` and `skills/granola-read/SKILL.md` directly.
  Use `manage.py granola_notes` exactly as `docs/actions-workflow.md` describes.
- Where the docs say "Codex", they mean the agent running this review: you.

## Known quirks

- Gmail discovery can hit `HttpError 403 rateLimitExceeded` (per-user query
  cost per minute). Throttle requests and retry with exponential backoff; treat
  Gmail as unavailable only after several spaced retries fail.
- `manage.py granola_notes --since-days N` counts back from now in UTC. Use
  `--since-days 16` so the 14-day discovery window covers whole calendar days.
- There is no stored full-review watermark. Take the last full review from the
  newest complete receipt under `artifacts/followups/`, and record this run's
  coverage window and completeness in the new receipt.
- Calendar, Slack, SMS and live LinkedIn have no read path here. Record them as
  coverage boundaries, not failures.

## Run

1. Read `docs/contextual-crm-review-prompts.md`.
2. Run its **Actions** review by following `docs/actions-workflow.md`, using
   stored LinkedIn, live Arian Gmail including Sent, and relevant full Granola
   meeting transcripts. Review every open item and every manual Active Account,
   plus engaged contacts outside Active Accounts.
3. Then run its **Active Accounts Next step** review by following
   `docs/active-account-next-steps-workflow.md`, updating only changed Next
   step cells for existing manual rows, including unbound rows.

Verify the configured CRM workbook and Arian mailbox through existing
authentication before reading conversation data. If a source or credential is
unavailable, preserve the affected rows, report the blocker and never call a
partial review complete or advance the full-review watermark.

### MODE: PREVIEW

Read every source and save the private receipt with the proposed additions,
updates, removals and Next step changes. Make no Sheet writes, do not take the
publisher lock for writes and do not advance any watermark.

### MODE: APPLY

Make only the precise writes the two docs authorize: the existing Actions tab
(sheet ID 1777950751, preserving hidden Followup IDs and operator Notes) and
changed Next step cells in Active Accounts. Use dated cross-channel
fulfillment, private decision receipts, final source/row preflight,
`crm_refresh_lock()` for the final read/check/write/readback, and precise API
readback. Stop on any conflict with a live human edit. Leave anything you rate
below high confidence unchanged and report it as a hold; skip purely cosmetic
wording edits.

## Hard limits (both modes)

- Never run `refresh_crm_v2`, `refresh_crm`, `generate_followups`,
  `sync_crm_v2_context`, importers, other publishers or workers, and never run
  argument-free `manage.py`.
- No message sends, no drafts, no CRM, campaign or database writes, no new
  logins and no schedule changes.
- Never print or save credentials, tokens or API keys in your output.

## Report

Your final message is the run report and is saved to a file by the wrapper.
Its first line must be exactly one of:

- `STATUS: QUIET` - nothing meaningful changed.
- `STATUS: CHANGES` - followed by concise bullets: new or resolved Actions,
  follow-ups that became actionable, and each changed Next step as
  `Account: old -> new (why)`.
- `STATUS: PREVIEW` - preview mode; followed by the proposed changes.
- `STATUS: BLOCKED` - a source, credential or conflict stopped part of the
  review; list the affected coverage and what was preserved.

End with one line: `Receipts: <run directory>`.
