# Queued LinkedIn reposts

User decision, 2026-10-29. Arian and Eddy (operator handle `Chuka`) repost a
Boundera company post from their own, already signed-in LinkedIn sessions. Boundera
Social Studio owns approval and scheduling: when an approved repost is due, it
enqueues one row here and later reads the result back. This repo only executes
queued rows for the local operator, once each. It sends no Slack messages for
reposts; the other repo reports outcomes.

Empty commentary uses LinkedIn's plain **Repost**. Non-empty commentary uses
**Repost with your thoughts** with exactly that text.

## Flag and rollout

`ENABLE_LINKEDIN_REPOSTS` defaults to `false`. With it off, the supervisor, daemon
and every existing worker behave exactly as before, and `run_linkedin_repost_once`
refuses to run. The flag only takes effect with `ENABLE_LINKEDIN_DAEMON=true`
(email-only mode never starts a repost child).

To enable reposts on a worker computer:

1. Apply migration `linkedin.0035_linkedinrepost` (an additive `LinkedInRepost`
   table) **before** turning the flag on. Without the table, the supervisor logs
   that the migration is missing and starts no repost child.
2. Set `ENABLE_LINKEDIN_REPOSTS=true` in that checkout's `.env`.
3. Restart the **whole supervisor** on that computer. Environment flags are not
   hot-reloaded, and a database child-restart request cannot load the new
   scheduler. The daemon also opens its loopback CDP port
   (`LISTENER_CDP_PORT`, default 9222) only when it starts with this flag (or the
   realtime listener or feed collector) enabled.
4. Supervise the first live repost for each operator (see Selectors below).

Code preparation does not prove live rollout.

## Commands

All commands print one JSON object on stdout. Errors exit non-zero with a short
message on stderr and change nothing.

### `enqueue_linkedin_repost`

```
manage.py enqueue_linkedin_repost --operator <Arian|Chuka|Eddy> \
    --post-url https://www.linkedin.com/feed/update/urn:li:activity:<digits>/ \
    --external-ref <ref> [--commentary-file <path>]
```

Output: `{"created": true, "external_ref": "...", "id": 12, "operator": "Chuka", "status": "queued"}`

- `--operator` accepts only Arian, Chuka or Eddy (case-insensitive). Eddy resolves
  to `Chuka` through `linkedin/operators.py`. Other operators are refused for now.
- `--post-url` must be exactly the canonical
  `https://www.linkedin.com/feed/update/urn:li:(activity|ugcPost|share):<digits>/`
  form: no query string, no missing trailing slash, no other host.
- `--external-ref`: 1–100 characters of letters, digits, `.`, `_`, `:` or `-`,
  starting with a letter or digit. It is unique.
- `--commentary-file`: UTF-8, at most 3,000 characters. It is stored and typed
  exactly. It must not start or end with whitespace and may contain `\n` line
  breaks but no other control characters (no tab, no `\r`) or byte-order mark.
  Omit the option, or pass an empty file, for a plain repost.
- Idempotent per `external_ref`. The same payload (operator, post URL,
  commentary) returns the existing row in its current status with
  `"created": false`. A different payload is refused and nothing changes. A stored
  payload can never be edited (model `save()` and queryset `update()` both refuse).

### `linkedin_repost_status`

```
manage.py linkedin_repost_status --external-ref <ref>
```

Output: `{"click_attempted_at": null, "completed_at": null, "error_type": null, "external_ref": "...", "result_url": null, "status": "queued"}`

Read-only. Timestamps are ISO 8601 UTC or `null`. A `running` row older than its
child bound (below) is reported as `uncertain`; the stored row is not changed.

### `cancel_linkedin_repost`

```
manage.py cancel_linkedin_repost --external-ref <ref>
```

Output: `{"cancelled": true, "external_ref": "...", "status": "skipped"}`

Only a `queued` row becomes `skipped`. Every other status is refused.

### `run_linkedin_repost_once`

The supervised child. Do not run it by hand while the supervisor has reposts
enabled: the supervisor runs at most one repost child per sender at a time, and
the worker relies on that.

## Statuses

| Status | Meaning |
| --- | --- |
| `queued` | Waiting for this operator's worker. |
| `running` | Claimed by a worker. |
| `sent` | Reposted, with a verified new permalink in `result_url`. A database constraint forbids `sent` without one. |
| `failed` | Stopped before the final click; LinkedIn was not changed. `error_type` names the reason. |
| `uncertain` | The final click was attempted (or the worker was interrupted) and success could not be verified. LinkedIn may have reposted. Never retried. |
| `skipped` | Cancelled while queued. |

`error_type` is an exception class name only (for example
`RepostLoginWallError`, `RepostCommentaryMismatchError`,
`RepostUnverifiedError`, `RepostInterruptedError`), never a raw message or page
text.

## How one run works

1. Resolve this checkout's operator from `LINKEDIN_USERNAME` with
   `resolve_operator`, and require exactly one matching `LinkedInProfile`
   (case-insensitive exact username, the `read_sender_stop` lookup). Refuse if it
   is stopped (`stop_requested`) or inactive, or if the operator is not Arian or
   Chuka.
2. Close any tabs a killed repost child left behind (recorded locally under
   `data/linkedin-reposts/<account hash>/targets.json`; only those exact targets).
3. Mark this operator's interrupted `running` rows `uncertain`
   (`RepostInterruptedError`): a row whose click was attempted, or that is older
   than its bound. They are never retried.
4. If a row is queued, attach to the daemon browser over loopback CDP. If the
   browser is not available, exit and leave the row queued for the next check.
5. Atomically claim this operator's oldest queued row (none while another of its
   rows is still running). A worker only ever claims its own operator's rows.
6. Read the member's own activity page (`/in/me/recent-activity/all/`) as a
   baseline, in its own tab.
7. Open its own tab directly at the post URL. A login, checkpoint or authwall URL,
   a captcha/checkpoint form, or a redirect away from the post fails the row
   before any click.
8. Find exactly one visible Repost control on the post (not in comments, dialogs
   or side rails) that is declared as a menu trigger, and open its menu. A
   control that might repost instantly is never clicked.
9. Plain repost: choose exactly one **Repost** option. With commentary: choose
   exactly one **Repost with your thoughts** option, require one empty editor in
   the composer, type the commentary with `human_type` (line breaks as
   Shift+Enter), and require the editor text to equal it exactly (only
   non-breaking and zero-width spaces are normalized). It is checked again
   right before Post. A mismatch is `failed`.
10. Record `click_attempted_at` in the database, then click the final control
    once. If the record cannot be written, nothing is clicked.
11. Verify: a success toast linking to a post other than the original gives the
    new permalink. Otherwise, if the baseline loaded, the newest activity card
    that was not in the baseline and embeds the original post gives it. No
    verified permalink means `uncertain`, never `sent` with an empty URL. An
    error toast after the click is `uncertain` (`RepostRejectedError`).
12. Close only its own tabs. Leaving the CDP session only disconnects; the daemon
    child owns the browser.

## Bounds and supervision

- The supervisor checks every 120 seconds, and only when no repost child is
  running, whether this sender has a queued row, using a bounded database read
  (5-second connect and statement timeouts). Only then does it start
  `run_linkedin_repost_once`.
- Each row is bounded by an in-process watchdog: 240 seconds plus the slowest
  possible typing (250 ms per commentary character). On expiry it closes the
  owned tabs and exits; the row stays `running`, reads as `uncertain`, and the
  next run marks it `uncertain`.
- The supervisor backstop is the longest row bound plus 30 seconds (1,020
  seconds). It then stops the child and closes its recorded tabs.
- The supervisor stops the repost child as a separate step before a sender
  restart or Git-update restart, when the daemon exits, and at shutdown. A
  stopped child records an attempted click as `uncertain` (after a hard kill,
  such as `terminate()` on Windows, the next run does).
- Reposts are not limited to `ACTIVE_*` working hours: the other repo decides when
  a repost is due.

## Safety rules

- One attempt per row. `uncertain` is never retried automatically; reconcile it on
  LinkedIn before queuing anything new for that post.
- The worker only opens and closes its own tabs, created directly at their URL
  over browser-level CDP (never about:blank then goto, never a page chosen by URL
  or tab order).
- CDP attachment only. Never `AccountSession` or `StandaloneLinkedInSession`, which
  launch browsers and log in.
- This is not a daemon `Task`: running daemons would claim and fail an unknown
  task type. The separate `LinkedInRepost` table is never read by the daemon,
  campaigns, feed like/comment or Slack handlers.
- Logs and stored errors contain row references, statuses and class names only.

## Selectors are unverified

The Repost menu, option, composer, editor, Post button, toast and activity-page
selectors were written on 2026-10-29 without a live LinkedIn check. They use
aria-label, role and visible text with several candidates, and any missing or
ambiguous control fails before the final click. `/in/me/recent-activity/all/`
is assumed to redirect to the signed-in member's activity; if it does not, the
baseline is skipped and only a toast link can prove success. **Supervise the first
live repost for each operator**, both plain and with commentary, and check the
recorded status against LinkedIn.

Known limits: mentions (`@name`) and hashtags can open LinkedIn typeahead that
rewrites the text; the exact-text check then fails the row before posting.
