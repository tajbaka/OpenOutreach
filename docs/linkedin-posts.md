# Queued personal LinkedIn posts

User decision, 2026-11-02. Arian and Eddy (operator handle `Chuka`) publish an
approved post from their own, already signed-in LinkedIn profile: text, one
image, or a document (a carousel PDF with its title). This is a post of their
own, not a repost. Boundera Social Studio owns approval and scheduling: when the
user schedules an approved post "as Arian" or "as Eddy", its dashboard writes one
queued row here with `scheduled_for`, may move that time or cancel the row while
it is still queued, and reads the result back (see "Social Studio's dashboard"
below). This repo only executes due queued rows for the local operator, once
each. Each finished row (sent with its link, failed with nothing posted, or
uncertain, including interrupted rows) sends one message to the feed Slack
channel (`SLACK_HIGH_SIGNAL_URL`, `notify_post_outcome`), as reposts do. Social
Studio sends no Slack for these posts.

It mirrors the repost feature (docs/linkedin-reposts.md) with its own table,
worker, flag and tab receipts; a post child and a repost child never drive the
same sender's browser at once.

## Rollout

There is no setting (user decision, 2026-11-02): the post worker runs whenever
the LinkedIn daemon does (`ENABLE_LINKEDIN_DAEMON=true`), like the repost
worker, and acts only on rows Social Studio's dashboard queues. On a worker
computer:

1. The supervisor runs `manage.py migrate` when it pulls updates, which applies
   `linkedin.0042_linkedinpost` (an additive `LinkedInPost` table). Without it
   the supervisor logs once that the table is missing and starts no post child.
2. Restart the **whole supervisor** once after the update that brings this code:
   the supervisor's own scheduler is not reloaded by a pull. The daemon always
   opens its loopback CDP port (`LISTENER_CDP_PORT`, default 9222) for the worker.
3. **Watch the first live post for each operator and each kind (text, image,
   document)**; see "Live verification scope" below.

Code preparation does not prove live rollout.

## Commands

All commands print one JSON object on stdout. Errors exit non-zero with a short
message on stderr and change nothing.

- `enqueue_linkedin_post --operator <Arian|Chuka|Eddy> --external-ref <ref>
  [--text-file <utf8>] [--media-kind image|document --media-url <sanity file>
  --media-sha256 <hex> --media-bytes <n> --media-name <file> [--document-title <t>]]
  [--post-at <iso>]`: for manual use and checks; Social Studio inserts rows
  itself. Same reference and payload returns the existing row; a different
  payload is refused.
- `linkedin_post_status --external-ref <ref>`: read-only; a `running` row past its
  bound reads as `uncertain`.
- `cancel_linkedin_post --external-ref <ref>`: a `queued` row becomes `skipped`;
  any other status is refused.
- `retry_linkedin_post --external-ref <ref>`: after a bug is fixed, queues the
  **same row** again, due at once, so Social Studio keeps following it. Only a
  `failed` row whose final click was never attempted qualifies, at most twice
  (`MAX_POST_RETRIES`). Never insert a new row for the same post.
- `run_linkedin_post_once`: the supervised child. Do not run it by hand while the
  supervisor has posts enabled.

## Statuses

| Status | Meaning |
| --- | --- |
| `queued` | Waiting for this operator's worker. |
| `running` | Claimed by a worker. |
| `sent` | Posted, with a verified permalink in `result_url`. A database constraint forbids `sent` without one. |
| `failed` | Stopped before the final click; LinkedIn was not changed. `error_type` names the reason. |
| `uncertain` | The final click was attempted (or the worker was interrupted) and success could not be verified. LinkedIn may have posted. Never retried. |
| `skipped` | Cancelled while queued. |

`error_type` is an exception class name only (for example
`PostMediaMismatchError`, `PostAttachmentError`, `PostCommentaryMismatchError`,
`PostUnverifiedError`, `PostInterruptedError`, `PostInvalidRowError`), never a
raw message or page text.

## How one run works

1. Resolve this checkout's operator exactly as reposts do (one matching, active,
   not stopped `LinkedInProfile`; Arian or Chuka only).
2. Close any tabs a killed post child left behind (recorded under
   `data/linkedin-posts/<account hash>/targets.json`, separate from the repost
   receipt).
3. Mark this operator's interrupted `running` rows `uncertain`
   (`PostInterruptedError`). They are never retried.
4. If a row is due, attach to the daemon browser over loopback CDP; if it is not
   available, exit and leave the row queued.
5. Atomically claim this operator's earliest due queued row (none while another of
   its posts, or one of its reposts, is running).
6. Re-validate the row as written (text rules, media fields as a unit, a
   `https://cdn.sanity.io/files/` address, document title). An invalid row is
   `failed` (`PostInvalidRowError`) before LinkedIn is opened.
7. With media, download the file to a private path (`data/linkedin-posts/<hash>/media/`,
   mode 0600) and keep it only if its SHA-256 and size match the row
   (`PostMediaMismatchError` / `PostMediaUnavailableError` otherwise, before any
   click). The copy is deleted after the run.
8. Read the member's own activity page as a baseline, then open the feed in its
   own tab. A login, checkpoint or authwall URL or a captcha form fails the row.
9. Click exactly one **Start a post** control; require exactly one empty composer
   (a restored draft, text or attachment, fails the row).
10. Image: the composer's single **Add media** control opens the file picker,
    which receives exactly the verified file; LinkedIn's **Next**/**Done** steps
    are followed until the composer shows the image preview. Document: the
    composer's single **Add a document** control (or the one behind **More**),
    **Choose file** with the verified PDF, the title typed exactly into the single
    title field (a prefilled title is replaced; the field must then equal the
    approved title), then **Done**, until the composer shows the document.
11. Type the text with `human_type` (line breaks as Shift+Enter) and require the
    editor text to equal it exactly; it is checked again, with the attachment
    still present, right before Post.
12. Record `click_attempted_at`, then click the single enabled **Post** once.
13. Verify: a success toast linking to a post gives the permalink. Otherwise the
    one new activity card (absent from the baseline) whose text contains the
    post's opening (or, without text, the document title) gives it. No verified
    permalink means `uncertain`. An error toast after the click is `uncertain`
    (`PostRejectedError`).

## Bounds and supervision

- The supervisor checks every 120 seconds, and only when neither a post nor a
  repost child is running, whether this sender has a due queued post (bounded
  database read). Only then does it start `run_linkedin_post_once`.
- Each row is bounded by an in-process watchdog: 360 seconds (feed, upload and
  document processing) plus the slowest possible typing. The supervisor backstop
  is the longest row bound plus 30 seconds.
- The supervisor stops the post child as a separate step before a sender or
  Git-update restart, when the daemon exits, and at shutdown.

## Social Studio's dashboard

Boundera Social Studio's hosted dashboard uses the same database access as for
reposts and touches only the `linkedin_linkedinpost` table:

- When the user schedules a post "as Arian" or "as Eddy", it inserts one row:
  `external_ref` (a new reference per scheduled version), `operator` (`Arian` or
  `Chuka`), the exact `commentary`, `media_kind` (`''`, `image` or `document`),
  `media_url` (the exact Sanity file copy), `media_sha256`, `media_bytes`,
  `media_name`, `document_title` (documents only), `status='queued'`,
  `scheduled_for`, `result_url=''`, `error_type=''`, `created_at` and
  `updated_at`. Every optional column has a database default, and `retry_count`
  defaults to 0.
- It may change `scheduled_for` of a row that is still `queued`, and cancel a
  `queued` row (`skipped`). It never changes a row in any other status and never
  edits the payload (a database-level guard refuses payload changes through the
  model).
- It reads `status`, `result_url`, `error_type`, `started_at` and `completed_at`
  back to record the outcome.

Database constraints: valid status; `sent` needs `result_url`; `media_kind` is
`''`, `image` or `document`; a row has text or media; media rows carry
`media_url`, `media_sha256` and `media_name`; document rows carry a title.
`tests/test_linkedin_posts.py` inserts rows with exactly these columns.

## Safety rules

- One attempt per row; `uncertain` is never retried automatically. Reconcile it
  on LinkedIn before queuing anything new for that post.
- Only the verified file is ever given to LinkedIn's file picker, and only from
  Sanity's file CDN.
- The worker only opens and closes its own tabs, over browser-level CDP; never
  `AccountSession` or `StandaloneLinkedInSession`.
- Not a daemon `Task`; the `LinkedInPost` table is never read by the daemon,
  campaigns or feed handlers.
- Logs and stored errors contain row references, statuses and class names only.

## Live verification scope

None of the composer, media or document selectors have been checked against
live LinkedIn (written 2026-11-02). They use aria-label, role and visible text,
and any missing or ambiguous control fails the row before the final click.
**Watch the first live post for each operator and each kind (text, image,
document)** and compare the recorded status with LinkedIn; record the confirmed
control path here afterwards. Known limits: mentions and hashtags can open
LinkedIn typeahead that rewrites text (the exact-text check then fails the row),
and an image-only post with no text has no activity-page fallback proof, so it
is `sent` only with a toast link.
