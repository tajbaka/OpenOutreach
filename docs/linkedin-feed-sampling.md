# Bounded LinkedIn feed sampling

When `ENABLE_LINKEDIN_FEED_COLLECTOR=true` and `ENABLE_LINKEDIN_DAEMON=true`, the
supervisor runs a fresh sample during the existing `ACTIVE_START_HOUR`,
`ACTIVE_END_HOUR`, `ACTIVE_TIMEZONE`, and `REST_DAYS` schedule. This checkout uses
09:00–17:00 Toronto, Monday–Friday. Sampling respects these hours even if outbound
active-hour enforcement is disabled.

The first eligible check opens one new feed tab. After a successful sample, the
supervisor chooses a random interval of 3,300–3,900 seconds (55–65 minutes), measured
from completion. The exact next time is saved locally and survives restart.
Polling does not redraw the interval. An overdue check runs once when working
hours resume; missed hours/days never produce catch-up samples. Process scheduling
can delay a due check slightly. Other worker restarts or machine downtime can delay
it further.

Each sample chooses its duration once: 165–195 seconds (2 minutes 45 seconds to
3 minutes 15 seconds), counted from the supervisor's reservation and including
process startup, browser attachment and feed loading. A reservation that expired
before the sample process started is held instead of sampled. Time is the
normal stopping condition; there is no per-sample post or scroll count cap. Before
each scroll, including the first, it waits a randomly chosen 3–6 seconds for
reading. It then smoothly moves 80–100% of one viewport and allows one second for
the movement to settle. If the remaining time is shorter than the reading pause,
it waits out the time without another scroll. It reads visible cards only (at most
20 DOM cards per scan), skips cards without an exact link or activity identifier,
and never opens post menus. Already-saved posts are deduplicated
using the existing post and sender-observation identities. The old daily job rows
and their observation links are preserved. New sample observations need no daily job.

Current LinkedIn SDUI cards often omit post links from their DOM attributes. The
collector also reads the exact post binding on the card's existing reaction
component, without clicking or fetching anything. It stays inside that card's
component boundary, excludes comment reaction IDs and rejects conflicting post
identities. The walk stops at either of React's two copies of the card's fiber, and
a binding that differs between the current and previous render counts as a
conflict. That reaction binding outranks header anchors and other attributes;
two different IDs in the same namespace (activity, share or ugcPost) are recorded
as `ambiguous_identity`. Only the card's own timestamp permalink counts as a link
identity. Quoted or embedded post links are never used; a card with only such
links records `unverified_post_link`. Unknown identities are still skipped rather
than guessed. Posts without a visible
caption retain their identity and available image/video metadata; their caption
stays empty. Their duplicate key includes that exact identity, so separate media
posts by one author stay separate. A same-author/same-text match under another
saved identity only records the sighting and never moves the saved row to the
other post. This does not perform image OCR or transcribe videos. A captionless
re-sighting never erases previously saved text. Social-context header lines for
every reaction type (like, celebrate, support, love, insightful, funny) and
comments/reposts are excluded from the author. These extraction rules are shared
with the explicit legacy `collect_linkedin_feed` command.

Every receipt includes `coverage`: distinct `post_cards_seen`,
`post_cards_extracted`, `post_cards_saved`, `non_post_modules`, and
`unsaved_reasons`. Counts cover visible cards at the sample's scans, deduplicate
repeat sightings, and include promoted posts. Recognized account/job recommendation modules
are counted separately because they are not posts. Saved-card counts require a
successful database write, not just extraction. A sample with no resolvable posts
records `no_resolvable_posts`; an error/checkpoint instead holds collection.

There is one browser attachment and one collector-owned tab per sample, with no
automatic recovery attempts. A separate watchdog requests closure of that exact
tab and terminates the sample process if blocked, allowing five seconds after the
chosen duration for normal cleanup. The supervisor provides an additional process
watchdog at 205 seconds. A failed/uncertain cleanup,
checkpoint, redirect, interrupted run, or other error holds collection for review;
there is no hourly retry of a failed run. The main sender and Gmail workers retain
their existing controls. Sampling never queues comments, Likes, or outreach.

Only the process that exclusively creates a reserved run's receipt may collect for
it; a duplicate invocation fails without collecting or rewriting that receipt.
Exclusion is local to the sender checkout. The sampler takes no Postgres session
lock, because a transaction pooler such as Neon's `-pooler` host can leave one on
another pooled session and block later samples.

Checkpoint detection uses redirects, captcha frames and checkpoint forms. Its
phrase check reads only visible page text outside post cards, recommendation
modules, side rails and hidden page data. Posts that discuss "security
verification" or "unusual activity" therefore do not hold collection, while
dialogs, interstitials and browser error pages still do.

State and per-run receipts live under `data/feed-sampling/<account-hash>/` with
owner-only files. Inspect on the worker computer:

```sh
.venv/bin/python manage.py feed_sampling_status
```

After reviewing the failed receipt and the browser/account condition, explicitly
resume the local sampler with:

```sh
.venv/bin/python manage.py feed_sampling_status --resume-after-review
```

This schedules a fresh 55–65 minute wait. It does not reopen a browser or bypass a
LinkedIn challenge. If the supervisor reported a scheduling-state error or was
interrupted, restart the full supervisor after review too.

If the supervisor stops during a sample (crash, kill or machine shutdown), its
saved reservation stays `running`. The first check after the 205-second limit
turns it into a hold and raises into the supervisor's existing "LinkedIn feed
scheduling held" alert, once. Review and resume as above, then restart the full
supervisor; feed scheduling stays paused in that supervisor until it restarts.

## Rollout

1. Install this code on the existing worker computer.
2. Enable `ENABLE_LINKEDIN_FEED_COLLECTOR=true` in that sender checkout's `.env`.
3. Stop and start the **full** `daemon_supervisor.py` process using its usual
   launcher. A sender's database restart flag only replaces children and cannot
   reload this scheduler.
4. Verify the first receipt says `completed`, `target_id` is empty,
   `duration_seconds` is 165–195, and `feed_sampling_status` shows a next time 55–65 minutes
   after completion.

No migration is required. Do not run `collect_linkedin_feed --backfill-days` as
part of rollout. The old `collect_linkedin_feed` command is retained only for
explicit manual legacy work and does not have the sampler's new limits.

Analysis is still a separate saved-post review/apply workflow. This scheduler
does not activate a Codex review automation or publish Slack alerts for saved
posts. The home feed is personalized, so even fresh samples do not guarantee
coverage of every newly published post. Interval variation is scheduling jitter,
not a guarantee against LinkedIn detecting automation.
