# Contextual CRM review prompts

Use an authorized OpenOutreach checkout with its existing database, Google Sheets,
Arian Gmail and Granola credentials. Use the checkout-relative virtualenv
(`.venv/Scripts/python.exe` on Windows; `.venv/bin/python` elsewhere).
Credentials, transcripts and private receipts never belong in Git.

Read the canonical skills directly from this checkout before either review:
`skills/openoutreach-google-sheets/SKILL.md`, `skills/gmail-read/SKILL.md`,
`skills/gmail-oauth/SKILL.md` and `skills/granola-read/SKILL.md`.
These files travel with Git; a separate personal skill installation is not
required to read them. Keep one maintained copy in the repo. Any personal
discovery entry should link to the repo folder rather than duplicate it.
If a required file is missing, safely update the checkout before recreating it.

## Actions

> Read `docs/actions-workflow.md` and run one complete contextual Actions review.
> Review stored LinkedIn, live Arian Gmail including Sent, and Granola meetings
> with full transcripts; use relevant stored Gemini notes only as dated supporting
> evidence. Discover the last 14 days, extend over a missed interval, and recheck
> every open item, every manual Active Account, and older relevant meeting
> commitments regardless of age. Before creating or retaining rows, exclude
> Closed Won and Closed Lost companies across all action types. Determine this
> from explicit user direction, recorded stages and clear dated LinkedIn/email/
> meeting context, including companies outside Active Accounts or with stale
> stages. Verify the account and relationship, check later reopening evidence,
> and record the closure basis. Archive excluded rows with their Notes/IDs;
> exclusion does not mean their obligations were fulfilled. Do not regenerate
> customer-support or nurture actions for closed accounts or change account/CRM
> stages. Hold ambiguous closure matches for review.
> Include both work Arian owes and justified
> relationship follow-ups inside or outside Active Accounts: requested material
> already delivered, stakeholder/proposal reviews, agreed reconnects, and evaluation
> check-ins. Require a concrete engagement signal, outcome, last actual touch and
> timing rationale; ordinary unanswered cold outreach is not an action. Honor
> promised windows, absence and booked meetings. Clearly label any recommended
> review date as suggested, keep it stable between runs, and never turn a completed
> delivery or decline into an outstanding obligation. Avoid repeated nudges and
> preserve operator deferrals. Retain named-contact redirects and accepted direct
> outreach steps even if the referrer declines an introduction; verify the target
> and prior outreach without asking the referrer again. Write concise Next action, Status, Next review,
> Last touch and Context cells, retaining detailed evidence privately/in hidden cells.
> Rank and sort whole rows by contextual priority, weighing verified deal size,
> current evidence-backed stage, urgency/blockers, buyer engagement, partner
> potential and actionability. Treat missing value as unknown and distinguish
> quotes/options from committed revenue; never infer value from company size.
> Keep future dates/statuses intact. Save the rank and rationale in hidden
> Evidence and a managed Context cell note, preserving operator notes and stable
> order for ties. Highest priority does not automatically mean contact today.
> Join verified identities and reconcile specific requests chronologically across
> all channels. Update only the existing Actions tab (formerly Followups), retain
> its native sheet ID, hidden Followup IDs, operator Notes and formatting, and
> include readable saved ICP labels or clearly marked suggestions. Save exact
> completion evidence before removing resolved rows. Recheck live rows/sources,
> take the publisher lock for final writes, and verify by API readback. Report
> meaningful changes, newly actionable follow-ups and incomplete coverage. No sends, drafts, CRM mutations,
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
