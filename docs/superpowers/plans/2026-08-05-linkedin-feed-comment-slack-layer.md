# LinkedIn Feed Comment Slack Layer

Date: 2026-08-05

## Objective

Build a human-approved Slack layer for commenting on LinkedIn feed posts.

Target flow:

1. Existing feed collection stores LinkedIn posts seen by sender accounts.
2. Existing feed analysis posts high-signal alerts to the high-signal Slack channel.
3. A Slack operator clicks "Comment on LinkedIn".
4. Slack opens a modal with post context, sender selection, editable comment text, and an AI draft action.
5. Modal submit queues a sender-scoped `feed_comment` task.
6. The matching sender's main daemon opens the exact LinkedIn post and submits the comment from that sender account.
7. Slack updates the original alert with queued, cancelled, sent, or failed status.
8. A durable ledger prevents duplicate public comments on retries.

Live QA target:

`https://www.linkedin.com/feed/update/urn:li:activity:7475978266084802560`

Run live QA with Arian first. Eddy can be shown in Slack UI, but the repo's canonical LinkedIn operator handle is `Chuka`, so the task payload must use `operator="Chuka"` for Eddy/Chuka's daemon.

## Design Constraints

- Preserve the existing Slack interactivity URL: `/api/slack_enrich`.
- Do not turn `api/slack_enrich.py` into nested conditional spaghetti.
- Keep existing manual-reply and enrichment behavior open/closed:
  - Existing action IDs continue routing unchanged.
  - Feed-comment behavior lives in focused new modules.
  - Existing router receives only small registry/delegation changes.
- Comment sending is UI-only at first. No Voyager/API fallback for public comments.
- Public-comment idempotency must fail closed. If a prior same post/operator/comment attempt is sent or uncertain, do not blindly retry.
- The feed collector remains read-only. It records posts and observations; it never posts comments.
- The main sender daemon performs the comment, using the same sender-scoped task ownership model as manual replies.

## Existing Surfaces To Reuse

- `api/slack_enrich.py`
  - Slack signature verification.
  - Existing action dispatcher.
  - Slack `views.open`, `views.update`, `chat.update`, and `response_url` helpers.
  - Raw `psycopg` DB access from Vercel.
  - LLM helper pattern for draft generation.

- `linkedin/notifications/slack.py`
  - Feed high-signal alert rendering.
  - Slack status update helpers can be mirrored or generalized.

- `linkedin.models.Task`
  - Persistent task queue.
  - Sender-scoped claim filtering.
  - Atomic pending-to-running claim behavior that makes pending-only cancel safe.

- `linkedin.models.LinkedInFeedPost`
  - Stores `post_url`, `activity_urn`, author metadata, post text, analysis fields.

- `linkedin.models.LinkedInFeedObservation`
  - Stores which sender account saw each post.
  - Provides sender choices for Slack modal.

- `linkedin/browser/nav.py:human_type`
  - Existing human-paced text input helper.

## Proposed New Modules

- `api/slack_feed_comment.py`
  - Feed-comment Slack action parsing.
  - Modal rendering.
  - AI public-comment draft generation.
  - Raw SQL context fetch.
  - Task enqueue and cancel helpers.

- `linkedin/tasks/feed_comment.py`
  - Daemon task handler.
  - Sender validation.
  - Ledger idempotency checks.
  - Slack sent/failed status updates.

- `linkedin/actions/feed_comment.py`
  - Playwright UI-only LinkedIn comment action.
  - Opens the target post URL/activity URL.
  - Finds comment composer.
  - Human-types comment.
  - Submits and verifies best-effort.

## Data Model

Add `LinkedInFeedComment`.

Suggested fields:

- `post` FK to `LinkedInFeedPost`
- `operator` canonical sender handle, indexed
- `account_username` optional account identifier
- `comment_text`
- `status`: `queued`, `running`, `sent`, `failed`, `uncertain`, `skipped`
- `task` optional FK to `Task`
- `slack_channel_id`
- `slack_message_ts`
- `slack_response_url`
- `slack_user_id`
- `commented_at`
- `error`
- `created_at`
- `updated_at`

Recommended constraints/indexes:

- Index on `(post, operator, created_at)`.
- Index on `(operator, status, created_at)`.
- Do not use a hard unique constraint on comment text until live behavior is proven; enforce idempotency in the handler so uncertain states can be represented explicitly.

## Task Queue Changes

Add `Task.TaskType.FEED_COMMENT = "feed_comment"`.

Payload requirements for pending/running tasks:

- `post_id`
- `operator`
- non-empty `message`

Sender scoping:

- Include `FEED_COMMENT` in linked account scoped task types.
- Add it to `_linked_operator_scope_q` with `payload.operator == operator`.
- Add tests proving Arian cannot claim Chuka/Eddy feed-comment tasks and vice versa.

Priority:

- Treat `feed_comment` like `manual_reply`: human-approved and latency-sensitive.
- Allow it through outside active hours like `manual_reply`, unless explicitly changed before implementation.

## Slack UX

Single feed alert:

- Add "Comment on LinkedIn" button.
- Button value includes `post_id` and available sender context.

Grouped feed alert:

- First version comments only on the primary post selected by existing group logic.
- Avoid multi-post commenting in v1.

Modal:

- Shows author, post excerpt, why it matters, suggested action, post link.
- Shows sender selector if more than one observing sender exists.
- Shows editable multiline comment textbox.
- Includes "Draft comment" action.
- Submit button queues the task.

Queued status:

- Update original Slack alert above the actions area.
- Include cancel button while task is pending.
- Cancel deletes only pending `feed_comment` tasks.

Sent/failed status:

- Daemon updates original Slack alert via `chat.update`.
- Falls back to `response_url` when message coordinates are unavailable.

## AI Draft Prompt

Use a separate public-comment prompt, not the private DM reply prompt.

Rules:

- Public, concise, useful, and non-salesy.
- No hard pitch.
- Avoid meeting asks unless the post explicitly asks for help.
- Do not invent facts.
- Do not overclaim Boundera capabilities.
- Mention Boundera only when contextually useful.
- Prefer a thoughtful practitioner-style comment that can stand publicly.

## LinkedIn UI Comment Action

Action should:

1. Resolve target URL:
   - Prefer `LinkedInFeedPost.post_url`.
   - Fall back to `https://www.linkedin.com/feed/update/<activity_urn>/`.
2. Open the post in the sender daemon's logged-in browser session.
3. Find and click comment UI.
4. Find active comment editor.
5. Use `human_type` for comment text.
6. Submit via the visible Post/Comment button.
7. Verify best-effort:
   - submitted text appears, or
   - composer clears and no error UI appears.
8. On uncertain outcome, mark ledger `uncertain` and tell Slack to verify manually.

No API fallback in v1.

## Idempotency And Retry Policy

Public comments are riskier than DMs because duplicates are visible.

Before sending:

- Check for existing `LinkedInFeedComment` with same `post`, `operator`, and normalized `comment_text`.
- If status is `sent`, skip and update Slack as already sent.
- If status is `running` or `uncertain`, fail closed and ask for manual verification.
- If status is `failed`, retry is allowed only if failure happened before any UI submit attempt. Otherwise mark uncertain.

During send:

- Create/update ledger before browser mutation.
- Mark the point where submit is attempted.
- If an exception occurs after submit attempt, mark `uncertain`, not plain failed.

## Setup Requirements

No new Slack app should be required.

Expected existing Vercel env vars:

- `DATABASE_URL`
- `SLACK_SIGNING_SECRET`
- `SLACK_BOT_TOKEN`
- `LLM_API_KEY`
- `AI_MODEL`
- optional `LLM_API_BASE`

Expected daemon/local env:

- `SLACK_HIGH_SIGNAL_URL` for feed alerts.
- Existing LinkedIn sender daemon env and browser profile setup.

Slack app setup:

- Existing Interactivity & Shortcuts request URL stays pointed at:
  `https://<vercel-project>.vercel.app/api/slack_enrich`
- The Slack app/bot must be in the high-signal channel so delayed `chat.update` status updates work.
- Incoming webhook for `SLACK_HIGH_SIGNAL_URL` must still post to the high-signal channel.

Deployment setup:

- Deploy updated Vercel function.
- Apply DB migration.
- Restart sender daemons for Arian and Chuka/Eddy.

## Tests

Add focused tests for:

- Existing `api/slack_enrich.py` manual reply/enrichment actions still route unchanged.
- Feed-comment action IDs route to delegated feed-comment handlers.
- Feed-comment modal parsing and rendering.
- AI draft action fills modal without losing typed text.
- Feed-comment submit inserts a pending `feed_comment` task.
- Feed-comment cancel deletes only pending tasks.
- Task validation rejects missing `post_id`, `operator`, or message.
- Task scoping prevents wrong sender claim.
- Handler blocks wrong sender.
- Handler skips existing sent ledger.
- Handler fails closed for uncertain prior attempts.
- Feed alert includes comment button and sender metadata.
- Browser action can be unit-tested with mocked Playwright locators.

Recommended test command:

`.venv/bin/python -m pytest tests/test_slack_enrich.py tests/test_feed_analysis.py tests/tasks/test_claim_filter.py tests/tasks/test_feed_comment.py`

Adjust file list based on actual test names added.

## Live QA Gates

Do not run live comment QA until:

- Migration applies cleanly.
- Focused tests pass.
- A dry-run/queued Slack path creates and cancels a task without touching LinkedIn.
- Arian daemon is running updated code.
- The target `LinkedInFeedPost` exists or is seeded for:
  `urn:li:activity:7475978266084802560`.

Live QA pass 1:

- Use Arian only.
- Submit one approved real comment through Slack.
- Verify:
  - task claimed only by Arian daemon
  - comment appears on the LinkedIn post
  - ledger marks sent
  - Slack alert updates to sent

Live QA pass 2, optional:

- Use Slack display label "Eddy".
- Payload operator must be canonical `Chuka`.
- Verify separate ledger row for same post and different operator.

Duplicate safety QA:

- Try same post/operator/comment text again.
- It must not double-comment.
- Slack should report already sent or manual verification needed.

## Non-Goals For V1

- No automatic comment posting from feed analysis without human approval.
- No commenting on every post in a grouped alert.
- No LinkedIn API/Voyager comment fallback.
- No separate Slack app.
- No separate public Slack interactivity endpoint unless the Slack app is intentionally reconfigured later.

## Completion Criteria

The feature is complete when:

- Feed alerts expose a Slack comment path.
- Feed-comment modal supports sender selection, editable text, AI draft, submit, and cancel.
- `feed_comment` tasks are sender-scoped and claimed only by the matching main sender daemon.
- The daemon comments on the exact LinkedIn post through UI automation.
- A durable ledger prevents duplicate public comments.
- Slack status updates work for queued, cancelled, sent, failed, and uncertain outcomes.
- Focused tests pass.
- Live QA succeeds on the target post with Arian.
- Eddy/Chuka live QA is either completed or explicitly deferred after Arian success.
