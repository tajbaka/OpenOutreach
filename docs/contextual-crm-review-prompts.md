# Contextual CRM review prompts

Use an authorized OpenOutreach checkout with its existing database, Google Sheets,
Arian Gmail and Granola credentials. Use the checkout-relative virtualenv
(`.venv/Scripts/python.exe` on Windows; `.venv/bin/python` elsewhere).
Credentials, transcripts and private receipts never belong in Git.

## Actions

> Read `docs/actions-workflow.md` and run one complete contextual Actions review.
> Review stored LinkedIn, live Arian Gmail including Sent, and Granola meetings
> with full transcripts; use relevant stored Gemini notes only as dated supporting
> evidence. Discover the last 14 days, extend over a missed interval, and recheck
> every open obligation plus older relevant meeting commitments regardless of age.
> Join verified identities and reconcile specific requests chronologically across
> all channels. Update only the existing Actions tab (formerly Followups), retain
> its native sheet ID, hidden Followup IDs, operator Notes and formatting, and
> include readable saved ICP labels or clearly marked suggestions. Save exact
> completion evidence before removing resolved rows. Recheck live rows/sources,
> take the publisher lock for final writes, and verify by API readback. Report
> meaningful changes and incomplete coverage. No sends, drafts, CRM mutations,
> other publishers, workers or schedule changes.

## Active Accounts Next step

> Read `docs/active-account-next-steps-workflow.md` and run one complete review of
> every existing manual Active Accounts row, including unbound rows. Use the
> latest dated LinkedIn, Gmail and full meeting context to identify the current
> account-level next move or who we are waiting for. Update only changed cells in
> the unique Next step column. Preserve rows, IDs, all other columns (including
> due dates), Notes, formulas and formatting. Preserve uncertain cells and report
> holds. Save source-backed decisions and verify all changes by API readback.
> No sends, drafts, CRM mutations, other publishers, workers or schedule changes.

Add **Preview only; no Sheet writes** to either request for a preview.

## Daily receiving-computer setup

The requested cadence is daily at **09:00 America/Toronto** on the other computer.
The Mac timer stays paused. Pull this change before the next Windows Sheet run:
`run_sync_sheets.ps1` now runs context, People and LinkedIn Pending, without the
retired generated Actions publisher. Its health check covers those three phases,
not the independent Codex reviews. `refresh_crm_v2`, including replacement mode,
refuses the contextual Actions schema; never bypass that refusal.

On the receiving computer, update its existing contextual-review automation
rather than create a duplicate. Use both prompts above sequentially (Actions,
then Next step) in one daily Codex run, with quiet unchanged results. Verify the
saved schedule, timezone, project/checkout and connections. If no runner exists,
create it there only when asked to enable it. Pulling Git or running these prompts
once does not install a local schedule. Never enable this Mac as a second runner.

The older `generate_followups` draft queue and `docs/codex-followup-automation.md`
are retired for this workbook. They do not perform either contextual review.
