# Windows workflow update prompt

Prepare the replacement workflow first; keep it paused until the smoke test and
the separately reviewed old-release handover are complete.

```text
Prepare my Windows RB2B workflow to enroll qualified visitors into a normal
high-priority OpenOutreach campaign, with its existing connection copy and
sequence. Keep existing outreach campaigns, sender hours/limits/pacing, CRM
identities and reply Slack routing unchanged.

Save the current RB2B automation prompt/schedule privately, then pause only
that review automation while preparing its replacement. Retain the daily
09:00 and 15:00 America/Toronto schedule. Do not run the new workflow yet.

Before a main-branch publication or pull, verify the live Windows supervisors
cannot automatically fetch/pull/migrate/restart onto the new code. Prefer an
already-active --no-update launch. Otherwise report the exact current launch
and tracking configuration for a narrowly scoped pin; do not interrupt live
outreach or restart a worker to force a pin. Also verify Vercel production is
pinned to its existing working deployment and Git auto-deployment cannot remove
the current /api/rb2b_webhook receiver. Preserve that receiver and incoming data.
Report any unverified automatic-update path before proceeding.

Use separate review checkouts of tajbaka/OpenOutreach and the private
tajbaka/rb2b-integration. Fetch candidate code there without altering the live
checkout or copying its .env, cookies or runtime data. Record both commit IDs.

Read OpenOutreach docs/campaign-priority.md and the integration README. Replace
the paused review automation's instructions with the integration's
docs/windows-automation-prompt.md. Its job is research, exact identity/history
checks and enrollment into one explicitly approved Arian campaign/audience/
message version. OpenOutreach owns connections and the subsequent sequence;
the helper must not operate LinkedIn, draft a separate note, submit standalone
connection requests or invent new references after a failed/uncertain attempt.
Outcomes go to #rb2b-v2 through callbacks; actual replies keep the existing
replies channel.

Do not invent a campaign ID, create/activate a campaign, enable integration
flags, apply migrations, alter live rows or restore automatic updates. Preserve
all old ready/attempting/sent/uncertain receipts, budget accounting, member URNs
and pending outcome/reply notifications. Their forward handover is not yet
implemented; a passing smoke test does not replace it.

Return the saved paused workflow, both reviewed revisions, missing configuration
and exact publication/cutover blockers. Then wait for the separate smoke-test
prompt. Leave the existing working outreach runtime on its current code.
```
