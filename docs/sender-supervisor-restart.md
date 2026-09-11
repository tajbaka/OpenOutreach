# Per-sender supervisor restart request

Set `LinkedInProfile.restart_requested` to true to request replacement of that
sender's supervisor-managed workers. It defaults false and is available as a
checkbox on the LinkedIn Profiles admin page.

## Requesting a restart

```bash
# Read-only preview. Eddy is also accepted and resolves to Chuka.
.venv/bin/python manage.py request_sender_restart --operator Arian

# Only after the user requests that sender's restart:
.venv/bin/python manage.py request_sender_restart --operator Arian --apply
```

The command requires exactly one profile matching the canonical sender's actual
LinkedIn login identity. Missing or duplicate matches fail closed, including
inactive duplicates. It changes only this Boolean; no campaigns, leads, Tasks,
deliveries, copy or schedules are created or modified. Repeated requests before
consumption coalesce as one pending Boolean, not a queue of restart counts.

## What happens

- At its normal 300-second poll, the running supervisor checks its own exact
  `LINKEDIN_USERNAME` profile. `--poll-seconds` changes the cadence. No fallback
  to another active profile is allowed.
- True restarts that supervisor's LinkedIn daemon and mapped Gmail worker. The
  LinkedIn daemon's listener/enrichment lanes come back with normal startup. A
  running feed collector stops and resumes under normal collection scheduling.
- False does nothing. No code push is needed. `--no-update` still permits this
  DB control; `--once` does not consume requests because it starts no workers.
- A simultaneous code update and Boolean request result in one restart, not two.
- The flag is cleared only after replacement processes have launched. Failed or
  cancelled launches leave it true. A DB outage leaves existing workers alone
  when detected before restart and preserves the request for retry.
- Row locking prevents two consumers acting on the same flag. A writer setting
  true during restart waits for acknowledgement, then leaves a new pending
  request for the next poll. No row or delivery history is deleted.

This does not restart the supervisor process or the computer, activate a new
campaign, bypass hours/limits/stops or force held/uncertain sends to retry. It
reloads the child's campaign snapshot through normal startup. Keep one active
supervisor per sender; this flag is not a distributed worker-ownership system.

## Deployment and verification

The user explicitly authorized merging and pushing the finished code to `main`
and testing the flags after rollout. Implementation commit `ef7aa6fc` is merged
into local `main` for publication. Remote code/schema pickup and full supervisor
bootstrap still need verification. No live flag has been set by implementation
or isolated QA.
Migration `0032_linkedinprofile_restart_requested`
adds the false-default Boolean; it depends on the feature branch's migration 0031.

**On first deployment, manually restart the full supervisor on each selected
machine after installing the schema and reviewed code.** An old supervisor
cannot learn this new polling behavior merely by pulling files and restarting
its children. Do not use an unconsumed flag as a substitute for that bootstrap.

After a separately authorized live request, verify the flag's readback and the
supervisor's `sender_restart` log, replacement process IDs, sender identity,
heartbeat and expected campaign pickup. A false flag proves acknowledgement of
process launch, not successful login or healthy outbound activity.

DB acknowledgement can fail after children have already restarted. The flag then
remains pending and a later poll may restart them again. Existing frozen-delivery,
submission-marker and due-date guards must remain enabled. Poll timing is
approximate; code pulls, migrations and graceful shutdowns take additional time.
The supervisor's control connection uses a five-second connect/statement timeout,
a one-second lock timeout, and TCP keepalive/user-timeout settings where supported
by the host. These are supervisor-process settings, not edits to `.env` or the
workers' DB configuration. A schema lock leaves the flag pending instead of
indefinitely blocking this control query; this is not a hard wall-clock guarantee
for every network failure or for the whole Git/update/restart cycle.

Implementation QA uses only the private socket-only PostgreSQL harness with
stubbed processes/providers, never a live restart. It covers migration defaults,
exact sender scope, flag-only writes, concurrent consumption/new requests,
cancellation, startup failure, DB outages and combined Git/DB restart requests.

Verified combined run: **1,603 tests passed, 1,148 message previews**, with no
live sends, shared-DB access, request flags set or real processes restarted.
See the [QA report](../artifacts/qa/campaigns/20260911T014208799188Z/README.md)
and [source/safety receipt](../artifacts/qa/campaigns/20260911T014208799188Z/run.json).
