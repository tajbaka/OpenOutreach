# LinkedIn Follow-Up and Drip Media Plan

**Date:** 2026-08-30

**Branch:** `codex/linkedin-message-media`

**Status:** Implemented, live-QA verified, and merge-ready on the feature branch

## Goal

Add one optional GIF or short MP4 attachment to post-connection LinkedIn
messages in both of the repository's active LinkedIn automation lifecycles:

1. the current `FOLLOW_UP` sequence; and
2. the independent `drip_linkedin` sequence.

The two lifecycles remain separate. They share only media resolution,
validation, upload, and send-outcome machinery. Existing connection, stop,
sender, sequencing, handoff, exact-destination, and uncertain-send behavior
must remain authoritative.

## Success Criteria

- A current post-connection follow-up can send either text only, text plus one
  GIF, or text plus one MP4.
- Any LinkedIn drip step can send the same three forms.
- Connection-request notes remain text only.
- Gmail behavior is unchanged.
- A declared attachment is never silently omitted.
- A human reply or other automation stop detected while a file uploads prevents
  the final Send click.
- A send that may have crossed the Send-click boundary is never automatically
  retried.
- Drip publication freezes the exact media bytes by digest, not merely the
  filename.
- Both supplied QA files send exactly once from Arian to Chuka through the real
  lifecycle handlers.
- Existing text-only follow-up and drip behavior continues to pass its current
  tests.

## Explicit Non-Goals

- Attachments on connection requests.
- Gmail attachments.
- Native LinkedIn voice notes.
- Feed posts, feed video, carousels, documents, images other than GIF, or
  multiple attachments.
- A new media-specific Task type or daemon.
- A second LinkedIn delivery route or Voyager/API fallback.
- Object storage, a media library UI, or automatic video transcoding.
- Hard video-duration validation in the first version.

## Pre-Implementation Code Findings

### Prior follow-up authoring

`linkedin/icp_outbound.py` already supports an ICP-level media registry and
placeholders such as `{demo.gif}` and the older `{add demo.gif}` form. Rendering
returns a `FilledMessage` containing a body and attachment paths.

The current behavior has three limitations:

- missing referenced files log a warning and disappear from the rendered
  message, allowing unintended text-only sends;
- more than one attachment can be parsed even though the handler uses only the
  first; and
- file type, file signature, size, and content identity are not validated.

The checked-in `assets/follow_up/demo.gif` is already used by current campaign
configuration and must continue working.

### Prior follow-up execution

Before this implementation, `linkedin/tasks/follow_up.py` preserved the current
campaign and Deal-based rules, required the connected state, applied sender and
stop checks, rendered the message, and called `send_media_message` when an
attachment was present.

That retired `send_media_message` path used LinkedIn's file input, waited a
fixed three seconds, typed the body, clicked Send, and caught a broad exception.
An exception after the Send click could therefore have produced a delayed
retry, creating a duplicate-send risk for GIFs and videos.

### Prior drip execution

`drip/manifest.py` schema version 1 accepts only `delay_days` and `body` for a
LinkedIn step, so media is currently rejected.

`drip/services/reconciliation.py` freezes rendered subject/body data in a
`DripDelivery`, but no media identity is stored.

`drip/tasks/linkedin.py` already has the safer lifecycle that this feature must
preserve:

- exact frozen destination member URN;
- sender-scoped reservation;
- final transactional stop check immediately before submission;
- `DripDeliveryAttempt` reservation and submission-attempt evidence;
- distinct sent, not-submitted, and unclear outcomes; and
- lane pause/recovery behavior after ambiguous submission.

`linkedin/actions/message.py:send_direct_message_once` implements the strict
one-route, one-click text send used by drip. It is the correct foundation for a
strict media send.

### Persistence and packaging

- `crm.Message.raw` can store media evidence without a CRM Message migration.
- Drip needs frozen media fields on `DripDelivery` and therefore a drip
  migration.
- Docker copies the repository into the image, so checked-in assets are
  available at runtime.
- The project currently has no usable video-duration dependency. The installed
  local `ffprobe` is also not a repository runtime dependency.

## Settled Product and Architecture Decisions

1. Only post-connection LinkedIn messages receive attachments.
2. One message may contain at most one attachment.
3. The supported types are GIF and MP4.
4. The maximum accepted size is 20 MiB.
5. A nonempty text body remains required.
6. `FOLLOW_UP` remains the current-outbound lifecycle.
7. `drip_linkedin` remains the independent drip lifecycle.
8. No new Task type is introduced.
9. Both handlers call the same strict media-upload primitive.
10. LinkedIn's direct compose route and the exact destination URN remain the
    sole delivery route.
11. The final caller-owned stop check runs after upload and typing, immediately
    before the only Send click.
12. Missing or invalid declared media fails closed; text is not sent alone.
13. Pre-submit technical failures may be retried according to the owning
    lifecycle.
14. Post-click ambiguity is `unclear` and is never automatically retried.
15. Drip media content is immutable for a published campaign version.
16. The existing `assets/follow_up/` location remains supported to avoid
    disrupting checked-in current follow-up configuration.
17. “Short video” is a reviewed-content rule in version one. Format and size
    are enforced in code; duration is recorded during QA but not enforced with
    a new heavy runtime dependency.

## Target Authoring Contracts

### Current follow-up JSON

Preserve the existing registry and placeholder design:

```json
{
  "media": ["demo.gif", "fedramp-episode-02.mp4"],
  "messages": [
    "Hey {first_name} — thought you might find this useful.\n\n{demo.gif}",
    "Sharing a quick overview. Curious what you think.\n\n{fedramp-episode-02.mp4}"
  ]
}
```

Validation rules:

- every placeholder must reference an entry in the same ICP media registry;
- exactly zero or one media placeholder may appear in a rendered message;
- every declared media reference must resolve inside an approved repository
  asset root; and
- missing or invalid media raises an expected configuration exception before
  browser mutation.

### Drip manifest schema version 2

Add an optional `media` object to LinkedIn steps only:

```json
{
  "delay_days": 0,
  "body": "Sharing a quick overview. Curious what you think.",
  "media": {
    "type": "video",
    "file": "fedramp-episode-02.mp4"
  }
}
```

Allowed values:

- `type: "gif"` with a `.gif` file; or
- `type: "video"` with an `.mp4` file.

Gmail steps reject `media`. Unknown media keys and multiple media values reject
the entire manifest.

Publishing resolves and validates the file, then writes generated media
metadata into the normalized immutable manifest snapshot:

```json
{
  "type": "video",
  "file": "fedramp-episode-02.mp4",
  "mime_type": "video/mp4",
  "size_bytes": 14443796,
  "sha256": "339335e7a4e2a4e4f1b0155859a22520271c2bf1b9f7e975883f62ff38a3e50b"
}
```

Because the normalized manifest hash includes this generated metadata, changing
the bytes at the same filename creates a different campaign version.

## Implementation Design

### 1. Shared media asset module

Add `linkedin/message_media.py` with:

- `LinkedInMediaKind` for `gif` and `video`;
- an immutable `LinkedInMediaAsset` value carrying the repository-relative
  reference, resolved path, kind, MIME type, size, and SHA-256;
- streaming SHA-256 calculation;
- repository-root and symlink-escape protection;
- nonempty and 20 MiB size validation;
- GIF87a/GIF89a signature validation; and
- MP4 ISO Base Media `ftyp` signature validation.

Expected configuration and validation errors belong in
`linkedin/exceptions.py`. Unexpected filesystem or browser failures continue to
surface normally.

The resolver accepts the existing `assets/follow_up/` root. Temporary live-QA
assets may be copied there without committing them and removed after QA.

### 2. Strict direct-message sender

Extend `send_direct_message_once` to accept an optional validated
`LinkedInMediaAsset`, keeping its current text-only behavior unchanged.

For media sends, the sequence is:

1. validate the exact member URN and the already-resolved asset;
2. navigate to the exact direct compose route;
3. locate the file input inside the intended composer;
4. attach the file;
5. poll for LinkedIn's attachment preview/upload-ready state and enabled Send
   state rather than sleeping for a fixed duration;
6. type the body;
7. re-prove the attachment inside that same sole visible composer;
8. execute the caller-provided final submission callback;
9. re-prove the same attachment after the callback and click Send once; and
10. verify the same confirmation boundary used by the strict text sender.

No API upload or popup-compose fallback is added. After the submission callback
returns successfully, failure to prove that Send did not occur is classified as
`UNCLEAR`.

### 3. Current follow-up integration

Replace only the attachment branch in `linkedin/tasks/follow_up.py`; leave the
current text-only path and current sequence logic intact.

The media branch will:

- resolve and validate the rendered attachment before opening LinkedIn;
- use the exact sender/recipient evidence already required by the handler;
- run final Campaign, Deal, drip-ownership, and shared automation-stop checks
  after the upload and typing complete;
- call the strict sender once;
- persist media evidence after confirmed success; and
- preserve the existing next-step scheduling only after confirmed success.

Outcome handling:

| Outcome | Current follow-up action |
| --- | --- |
| Confirmed sent | Persist Message/ActionLog and advance normally |
| Pre-submit technical failure | Requeue using the existing delayed retry when the business guards still permit it |
| Final stop check blocks | Do not send and do not create another automated message |
| Unclear after submission boundary | Raise an expected uncertain-send exception so the daemon marks the Task failed; alert and do not requeue or advance |
| Process exit after durable submission boundary | On startup, requeue only when the exact media-bearing Message exists so sent-step dedupe can finish bookkeeping; otherwise fail the Task as unclear and block automatic resend |
| Invalid or missing configured asset | Fail the Task before browser mutation; never send text alone |

The old broad-exception `send_media_message` path is retired; there is one
strict media implementation.

### 4. Drip manifest, model, and materialization

Bump the new manifest contract to schema version 2 and update checked-in
fixtures/documentation. There is no published production drip cohort to migrate
silently; validation should fail clearly for an obsolete manifest instead of
guessing.

Add all-or-none frozen media fields to `DripDelivery`:

- `frozen_media_kind`;
- `frozen_media_reference`;
- `frozen_media_mime_type`;
- `frozen_media_size_bytes`; and
- `frozen_media_sha256`.

Model validation requires either all fields or none, and permits them only on a
LinkedIn lane.

`drip/services/reconciliation.py` copies the exact media metadata from the
published campaign snapshot when materializing a delivery. The Task payload
continues to route by delivery ID and operator; it does not become the source of
truth for media.

### 5. Drip LinkedIn integration

Extend the existing reservation value in `drip/tasks/linkedin.py` with frozen
media metadata. Before browser mutation, resolve the file and verify its current
size and SHA-256 against the delivery.

- A matching asset is passed to the shared strict sender.
- A missing or changed frozen asset is a nonretryable configuration hold: pause
  the lane and require review or restoration of the exact bytes.
- The existing final transactional submission callback remains authoritative.
- Existing attempt reservation, submission-attempt stamping, unclear handling,
  stale recovery, exact destination URN, and sender behavior remain unchanged.

No current `Deal.state` mutation and no current `FOLLOW_UP` Task creation is
introduced by drip media.

### 6. Message evidence

On confirmed success, store media evidence in `crm.Message.raw` for both flows:

```json
{
  "media": {
    "type": "video",
    "reference": "fedramp-episode-02.mp4",
    "mime_type": "video/mp4",
    "size_bytes": 14443796,
    "sha256": "339335e7a4e2a4e4f1b0155859a22520271c2bf1b9f7e975883f62ff38a3e50b"
  }
}
```

Extend `linkedin/db/chat.py:save_chat_message` with an optional raw-evidence
argument for current follow-ups. Drip continues creating its own Message through
its existing delivery-success transaction and adds the same media object to its
current raw evidence.

## Failure Semantics

| Failure point | Provider may have received message? | Required result |
| --- | --- | --- |
| File validation | No | Fail closed before navigation |
| Compose navigation | No | Pre-submit failure |
| File attachment/upload | No | Pre-submit failure |
| Final stop callback | No | Stop without Send |
| Send click throws or confirmation is missing | Possibly | Unclear; never auto-retry |
| Confirmation succeeds but CRM persistence fails | Yes | Surface the unexpected error and preserve send evidence where the owning attempt lifecycle allows recovery; never send again merely to repair persistence |

## Automated Verification

### Media unit tests

- valid GIF87a/GIF89a and MP4 `ftyp` files;
- wrong extension/signature combinations;
- missing, empty, oversized, and unreadable files;
- repository traversal and symlink escape;
- deterministic size and SHA-256 metadata; and
- rejection of more than one attachment.

### Current follow-up tests

- existing GIF placeholder behavior remains valid;
- MP4 placeholder rendering works;
- missing declared media fails instead of falling back to text;
- GIF and MP4 use the strict media sender;
- final reply/stop check occurs after upload and before click;
- pre-submit technical failure follows the existing delayed retry;
- a business stop does not retry;
- unclear submission does not retry or advance the sequence; and
- confirmed send advances once and records media evidence.

### Drip tests

- schema version 2 accepts media on LinkedIn and rejects it on Gmail;
- normalized content hash changes when file bytes change;
- delivery materialization freezes all media metadata;
- reservation uses the exact frozen destination and asset identity;
- hash drift or missing bytes pauses without sending;
- upload occurs before the existing final transactional stop check;
- an inbound reply during upload prevents the click;
- GIF and MP4 confirmed sends record one attempt and one Message;
- ambiguous submission retains current unclear/pause behavior; and
- stale-attempt recovery remains unchanged.

### Sender primitive tests

- file input is scoped to the direct-message composer;
- upload readiness precedes body typing/submission callback;
- callback precedes the only Send click;
- callback abort performs no click;
- upload timeout is pre-submit;
- click error or missing confirmation is unclear;
- invalid member URN fails before navigation; and
- no alternate delivery route is invoked.

### Repository verification

Run targeted tests first, then:

```bash
.venv/bin/python manage.py check
.venv/bin/python manage.py makemigrations --check
make test
```

Use the project virtual environment for every command; system Python must not
be substituted.

Update `AGENTS.md`, `ARCHITECTURE.md`, and `drip/campaigns/README.md` with the
final schema, authoring, runtime, and failure behavior.

## Controlled Live QA: Arian to Chuka

The live test uses the real handlers and the existing Arian-to-Chuka LinkedIn
connection/thread. It does not use a generic media uploader or alternate
delivery path.

### Frozen fixtures

GIF:

- source: supplied clipboard GIF;
- existing repository match: `assets/follow_up/demo.gif`;
- format: GIF89a;
- dimensions: 1080 × 856;
- frames: 4;
- size: 489,838 bytes;
- SHA-256:
  `8b4b8e97e7ee1ac08e66aadaf7146fa85d493f1e18646bf6c0ee060db5beec49`.

MP4:

- source: `/Users/admin/Downloads/fedramp-episode-02 (1).mp4`;
- stable QA filename: `fedramp-episode-02.mp4`;
- codecs: H.264 video and MPEG-4 AAC audio;
- dimensions: 1920 × 1080;
- duration: 106.234 seconds;
- size: 14,443,796 bytes;
- SHA-256:
  `339335e7a4e2a4e4f1b0155859a22520271c2bf1b9f7e975883f62ff38a3e50b`.

The GIF already exists in the repository. Copy the MP4 temporarily into the
approved asset root for QA, do not commit it by default, record it in the QA
receipt, and remove the temporary copy after testing. Production use of that
video is a separate explicit decision.

### Live sequence

1. Confirm this checkout is running and no other Arian process owns the same
   persistent browser/session.
2. Verify the browser is authenticated as Arian.
3. Resolve and freeze Chuka's exact member URN from existing trusted evidence.
4. Inspect due Arian Tasks so the controlled run will not accidentally process
   unrelated work.
5. Send the GIF through the real current `FOLLOW_UP` media branch with:

   ```text
   Hey Chuka — thought you’d find this useful.
   ```

6. Confirm the GIF appears once in the Arian/Chuka thread, the Task completes,
   one Message is stored with matching media evidence, and the current sequence
   advances once.
7. Complete the normal current-to-drip handoff for that QA lane.
8. Materialize and send the MP4 through the real `drip_linkedin` handler with:

   ```text
   Hey Chuka — sharing a quick overview. Curious what you think.
   ```

9. Confirm the video appears once, plays in the thread, and has the correct
   delivery, attempt, Task, Message, sender, recipient, digest, and sent time.
10. Wait through one reconciliation pass and confirm no duplicate delivery or
    retry appears.
11. Restore the internal QA campaign/enrollment state and remove the temporary
    MP4 copy while preserving the QA receipt.

Two live sends are sufficient because both lifecycle handlers exercise the
same strict uploader, while automated tests cover the full GIF/MP4 ×
follow-up/drip matrix.

## Implementation Order

1. Add expected media exceptions and the pure media resolver/validator.
2. Add media unit tests.
3. Extend the strict direct-message sender and its tests.
4. Move the current follow-up media branch onto the strict sender.
5. Add drip schema version 2 and publishing validation.
6. Add the DripDelivery migration and materialization fields.
7. Extend the drip LinkedIn reservation and handler.
8. Add shared Message media evidence.
9. Run targeted and full automated verification.
10. Update architecture and operator documentation.
11. Perform the controlled Arian-to-Chuka QA and record the receipt.

## Merge Gate

The branch is ready to merge only when:

- all feature-scoped automated verification passes;
- migration drift is clean;
- text-only current follow-up and drip regressions pass;
- the Arian-to-Chuka GIF and MP4 sends each appear exactly once;
- no uncertain outcome was automatically retried;
- database evidence matches the exact recipient, sender, bytes, and handler;
- temporary QA state/assets are cleaned up; and
- `AGENTS.md` and `ARCHITECTURE.md` describe the shipped behavior.

## Final Verification Result

The merge gate is satisfied on `codex/linkedin-message-media`:

- Arian sent the supplied GIF to Chuka through the real current `FOLLOW_UP`
  handler exactly once, and LinkedIn's persisted conversation data confirmed
  one image render.
- Arian sent the supplied MP4 to Chuka through the real `drip_linkedin`
  handler exactly once, and LinkedIn's persisted conversation data confirmed
  a playable MP4 render with video metadata.
- The configured delay was enforced, and a second reconciliation produced no
  delivery or Task duplicate.
- Exact Message media evidence, Task/delivery state, sender, recipient, and
  digests matched the frozen fixtures.
- The temporary MP4 asset and worktree runtime links were removed; original
  source media and shared runtime data were not changed.
- The final focused media/drip/recovery suite passed `196` tests.
- The repository suite passed `1890` tests. Five failures are unchanged,
  unrelated baselines in invitation withdrawal, connect-pool healing, and
  onboarding. Two additional unchanged tests became time-bound failures when
  the test process crossed UTC midnight while Toronto remained on the prior
  date; they cover ActionLog day reset and CRM action publication, not media.
- Django system checks passed, `makemigrations --check --dry-run` reported no
  drift, and `git diff --check` passed.
- Current media Tasks now durably mark the submission boundary. A daemon
  restart can resume bookkeeping only from an exact media-bearing Message;
  without that evidence it fails closed as unclear and cannot resend.
- The pre-click transaction serializes same-sender sibling Tasks on the Lead,
  and a provider-confirmed send cannot advance state or schedule a successor
  until its exact Message evidence is successfully read back.
- That unresolved state is sender-scoped, blocks sibling current Tasks and
  current-to-drip handoff, and cannot be bypassed through a `not_applicable`
  handoff review.
