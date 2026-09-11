# Sender emergency stop

`LinkedInProfile.stop_requested` is a persistent, sender-scoped emergency-stop
latch, false by default (migration 0033). It is separate from the one-shot
`restart_requested` flag. The user authorized merging the implementation from
`codex/sender-emergency-stop` into `main` and pushing it. Publication alone
does not verify remote deployment, the migration or a controlled live stop.

## Operator commands

All commands preview by default. These examples are instructions, not permission
to stop an unrelated sender or change campaign membership.

```bash
.venv/bin/python manage.py request_sender_stop --operator Arian
.venv/bin/python manage.py request_sender_stop --operator Arian --apply
# Eddy resolves to the canonical Chuka sender.
.venv/bin/python manage.py request_sender_stop --operator Eddy --apply
# Clearing the latch does not launch or automatically resume anything.
.venv/bin/python manage.py request_sender_stop --operator Arian --clear --apply
```

Arming stop also cancels that profile's pending restart. Restart commands and
the restart consumer refuse a latched stop. The admin checkboxes use locked,
intentional field updates and preserve concurrent changes from stale forms.
The command reports a request, not proof that the remote machine stopped.

## Runtime behavior

The supervisor checks the exact local `LINKEDIN_USERNAME` before launching any
workers. A missing/ambiguous identity or unavailable initial stop state blocks
startup. A latched stop remains set after shutdown and blocks subsequent starts.
Recovery requires explicitly clearing it and deliberately starting the supervisor.

An independent watchdog checks approximately every 15 seconds. It does not wait
behind the five-minute Git/restart poll. Every subprocess launched by the
supervisor, including LinkedIn, mapped Gmail, feed collection, Git, dependency
installation and migration commands, is registered behind a shared launch gate.
Once stopped, that gate refuses new processes, including crash-recovery launches.

Shutdown captures owned descendant processes and terminates those process trees,
then escalates surviving processes after a bounded grace period. Ownership uses
registered process handles and creation identities, never a global name match.
It does not close unrelated browser windows, another sender on another machine,
or independent tools outside this supervisor. Already orphaned descendants that
were never observed cannot be safely rediscovered by guessing names or PIDs.

The stop control needs a reachable DB. A transient runtime DB outage is reported
and retried; it is not evidence that the flag is false or a guaranteed shutdown.
Unexpected watchdog/control failures stop owned work and surface the error.
Polling, DB timeouts, row-lock contention and process shutdown add latency; this
is not instantaneous or an operating-system-wide kill switch. A message already
submitted to LinkedIn/Gmail cannot be recalled. Interrupted sends must retain
the existing ambiguous-send holds; never reset delivery state just to resume.

## Slack

- A completed database restart trigger posts to the normal ops channel, the
  same destination as Git-pull alerts. It reports acknowledgement and process
  launch, not successful authentication or sending health.
- Emergency stop posts to the replies channel (`SLACK_REPLIES_WEBHOOK_URL`),
  as explicitly selected by the user. If unavailable or delivery fails, the
  existing ops channel is the fallback. No new webhook or channel is created.
- Owned work is stopped before the emergency notification is attempted, so
  Slack availability does not delay the shutdown. An unsuccessful shutdown is
  described as requiring verification, not successful termination.

## Deployment and verification

Install migration 0033 and restart the full supervisor once on each selected
machine to load the new watchdog. Git's child-only reload cannot update the
running supervisor interpreter. Do not claim a new safety control is available
on a remote laptop just because a code push or daemon heartbeat exists.

First verify with isolated PostgreSQL tests and mocked processes/providers.
Then use a separately authorized controlled live stop: observe the flag,
supervisor console, actual owned-process exit and replies-channel notification.
The persistent stop flag alone does not acknowledge completion. Do not infer
successful shutdown solely from a stale heartbeat, which could have other causes.

Verification completed September 11 UTC on the uncommitted feature branch:
`artifacts/qa/campaigns/20260911T025222371756Z/` records 1,704 passing tests
and 1,148 message previews, with unchanged message inputs/source fingerprint,
no production DB/provider access, and its private PostgreSQL cluster removed.
`artifacts/qa/sender-stop-os-smoke-2026-09-11/result.json` records a synthetic
local macOS process-tree stop: three owned processes terminated, an unregistered
sentinel preserved, post-stop launch refused, and one callback after shutdown.
That smoke used a local fake flag and 0.1-second polling, not the production
15-second cadence. Neither check proves migration deployment, remote Windows
shutdown, actual Slack delivery, or either laptop loading the new supervisor.

The main-release reconciliation also preserves concurrent listener fix
`ac6ec839`. The combined source in merge `43cc4029` passed **1,729 tests and
1,148 message previews** under `artifacts/qa/campaigns/20260911T030401607352Z/`.
Source fingerprint `0793d3404feab7497dd1f00c81b2963c3eb71f13d34b7815b8061051e0a47410`
and both approved JSON inputs were unchanged; the private cluster was removed.
No shared-DB migration, live flag change, campaign creation or remote restart
was performed by that verification. An old remote supervisor may automatically
pull/migrate/restart children after publication, but still needs a full restart
to load this watchdog. Check the schema before bootstrapping the new supervisor.

Campaigns, leads, copy and schedules are not changed by this control. Future
campaign preparation and 9 a.m. monitoring requested by the user are a subsequent
workflow, not part of installing or testing this feature.
