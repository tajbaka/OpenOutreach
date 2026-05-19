# Daemon Startup Checks — Design

**Date:** 2026-05-19
**Status:** Approved — ready for implementation planning

## Goal

Before the daemon starts doing real work, verify two things:

1. The git checkout is not behind its upstream branch, and
2. The operator is warned about missing environment variables.

The checks should fit the current deployment reality: interactive on a dev box,
headless under systemd/cron, and a no-op inside Docker-style deployments that
do not ship a `.git` directory.

## Decisions locked in

| Decision | Choice | Rationale |
|---|---|---|
| Where checks run | At the very top of `manage.py`'s no-args daemon branch, before `_ensure_db()` | If code is updated, the process should restart before migrations or daemon boot happen on stale code. |
| Update source of truth | Git upstream comparison (`HEAD` vs `@{u}`) | Accurate for the currently checked-out branch; does not assume `main`. |
| Headless behavior when behind | Auto-pull silently, then exit | No prompt is possible; the operator asked for unattended update behavior. |
| Interactive behavior when behind | Prompt the user whether to pull | Matches local dev expectations and avoids surprising pulls in a terminal session. |
| Pull mode | `git pull --ff-only` | Safe and deterministic; avoids creating merge commits during daemon startup. |
| Post-pull behavior | Exit immediately after a successful pull | The current Python process still has the old code loaded in memory. Restart is required to pick up the new code. |
| Pull failure behavior | Log loudly, notify Slack via `notify_error`, exit non-zero | The operator explicitly chose fail-fast over running stale code after a blocked update. |
| Dirty working tree handling | Attempt the pull anyway | Locked by operator decision. If the dirty tree blocks the pull, that is surfaced as a failure and the daemon exits. |
| Env-var checker behavior | Warn only; never blocks startup by itself | The operator asked for a checker that warns on missing vars. Existing hard requirements keep their current enforcement paths. |
| Env-var source of truth | New declared registry in code | One canonical mapping should later drive `.env.example` generation in Spec 2. |

## Scope

This spec covers only daemon startup integrity checks:

- update detection and optional/automatic pulling
- missing-env-var warnings at startup

It does **not** include:

- automatic version stamping
- git hooks
- `.env.example` generation

Those belong to the separate repo-hygiene spec that follows this one.

## Architecture

### Startup flow

The daemon path today is the `len(sys.argv) == 1` branch in `manage.py`. The
new startup sequence becomes:

```text
python manage.py
  │
  ▼
check_for_updates()
  ├─ maybe skip
  ├─ maybe prompt / auto-pull
  ├─ maybe exit(0) after successful pull
  └─ maybe exit(1) on pull failure
  │
  ▼
check_env_vars()
  └─ warn on missing vars
  │
  ▼
_ensure_db()
  │
  ▼
run_daemon()
```

Ordering matters:

- the git check must run first so new code can be pulled before migrations
  execute
- the env check should still run before `_ensure_db()` so operators see config
  issues immediately at process start

## Component A — Git update check

### Public API

New module: `linkedin/version_check.py`

Public entrypoint:

- `check_for_updates() -> None`

This function is called only by the daemon startup path in `manage.py`.

### Decision flow

| Situation | Action |
|---|---|
| Repository is not a git checkout (`.git` missing or git commands fail with "not a repository") | Skip silently |
| Current checkout is detached or has no upstream tracking branch | Skip and debug-log why |
| `git fetch` fails (offline, DNS, auth, remote unavailable) | Warn and continue on current code |
| Local branch is up to date with upstream | Debug-log and continue |
| Local branch is behind and stdin/stdout is a TTY | Prompt: pull latest updates? |
| Local branch is behind and no TTY is available | Auto-pull silently |
| `git pull --ff-only` succeeds | Log old/new revision info and `sys.exit(0)` |
| `git pull --ff-only` fails | Log loudly, `notify_error(...)`, `sys.exit(1)` |

### Git strategy

The check compares the current branch's local `HEAD` to its configured upstream
branch (`@{u}`), not to a hardcoded branch name. That keeps behavior correct on
feature branches and any deployment branch that tracks something other than
`main`.

Expected git sub-steps:

1. Confirm this is a git checkout.
2. Discover the current branch and upstream.
3. Run `git fetch`.
4. Compare `HEAD` vs upstream.
5. If behind, either prompt or auto-pull.
6. Pull with `git pull --ff-only`.

The implementation should use a thin internal helper for running git commands,
so tests can patch one seam instead of mocking subprocess calls everywhere.

### Prompting behavior

Interactive prompt is used only when a TTY is available. The prompt should be
brief and explicit about the consequence:

- updates are available
- pulling will cause the process to exit so it can be restarted on fresh code

If the user declines, the daemon continues on the current code after a warning
log. This is intentionally less strict than the headless path.

### Headless behavior

When no prompt is possible, the daemon auto-pulls and then exits on success.
This is the operator-approved behavior for systemd/cron-style runs.

Important boundary:

- in Docker/image-based deployments there is usually no `.git` directory, so
  the check becomes a no-op and never attempts self-updating

That keeps image-based deployments on their normal "rebuild/redeploy" path.

### Dirty working tree

The daemon does **not** pre-check cleanliness or abort early because the
operator explicitly chose "pull anyway". If the working tree blocks the pull,
that failure is treated the same as any other pull failure:

- loud logs
- Slack error notification
- non-zero exit

### Notifications and logging

Only pull failures escalate to Slack. Routine "already up to date", "not a git
checkout", or "fetch failed, continuing" outcomes stay in logs only.

Suggested logging levels:

- debug: skipped because no git checkout, no upstream, already up to date
- warning: fetch failed; interactive user declined available updates
- error: pull failed and the daemon will exit

### Exit semantics

| Outcome | Exit behavior |
|---|---|
| No update or skipped check | Continue startup |
| User declines pull in interactive mode | Continue startup |
| Pull succeeds | `sys.exit(0)` |
| Pull fails | `sys.exit(1)` |

The successful-pull exit is mandatory because imported Python modules remain the
old code until process restart.

### Crash-loop note

`sys.exit(1)` on pull failure can create restart-loop noise under aggressive
supervisors such as `restart: always`. That is an accepted tradeoff in this
spec: failing loudly is preferred over quietly running stale code after a
blocked update.

## Component B — Env-var checker

### Public API

New modules:

- `linkedin/env_spec.py`
- `linkedin/env_check.py`

Public entrypoint:

- `check_env_vars() -> None`

### Registry design

`env_spec.py` becomes the canonical declared registry for project-owned
environment variables. Each variable is represented by metadata rich enough to
support both startup warnings now and `.env.example` generation later.

Proposed structure:

- `name`
- `required`
- `secret`
- `default`
- `group`
- `description`

This registry should include only variables that the OpenOutreach codebase
actually reads, not transitive library/test/tooling noise.

### Initial audit basis

The registry will be built from the current code-level env-var audit already
performed before this spec. That audit found roughly 45 project-owned variables
across these categories:

- LinkedIn account credentials
- LLM configuration
- database configuration
- Slack webhooks and signing secret
- Google Sheets integration
- company metadata
- feature flags
- timing/rate-limit tuning knobs
- enrichment provider API keys

`DJANGO_SETTINGS_MODULE` and `DJANGO_ALLOW_ASYNC_UNSAFE` are set by code and do
not belong in the operator-facing registry.

### Warning behavior

`check_env_vars()` iterates the registry and logs a startup summary for missing
variables.

It should distinguish at least:

- missing required vars
- missing recommended/optional vars

The checker does **not** abort startup. It is informational. Existing
downstream code that already hard-fails on truly required vars keeps doing so.
For example, if the daemon later requires `LLM_API_KEY` in an existing code
path, that enforcement remains unchanged; this startup check just surfaces the
problem earlier and more comprehensively.

### Output shape

The warning should be operator-readable, grouped, and emitted once per startup
rather than as dozens of one-line warnings. A single summary is easier to scan
in systemd logs and Slack-copied diagnostics.

Recommended content:

- which vars are missing
- whether each is required or optional
- a short hint to compare against `.env` / future `.env.example`

### Why a registry now

This spec intentionally introduces the registry before `.env.example`
generation. That avoids duplicated env-var definitions and keeps Spec 2 simple:
the future example-file generator will consume the exact same registry used by
startup warnings.

## Error handling

Per the project rule, expected recoverable cases are handled explicitly;
unexpected exceptions still crash.

### Expected cases

- git checkout missing
- git upstream missing
- git fetch failure due to network/auth/remote issues
- interactive user declining a pull
- missing env vars

These are handled and converted into skip/warn/continue behavior as defined
above.

### Expected but fatal case

- `git pull --ff-only` failure

This is an expected operational failure mode, but by product decision it is
fatal to startup: report it and exit non-zero.

### Unexpected exceptions

Anything outside the explicitly handled paths should propagate and crash, rather
than being swallowed by broad startup wrappers.

## Testing

New tests in `tests/test_startup_checks.py`.

Coverage should include:

- non-git checkout skips cleanly
- detached HEAD / missing upstream skips cleanly
- fetch failure warns and continues
- already-up-to-date branch continues
- interactive behind-state prompt: yes path triggers pull and exit 0
- interactive behind-state prompt: no path continues
- headless behind-state auto-pulls and exits 0
- pull failure triggers Slack notification and exits 1
- env checker warns on missing required vars
- env checker warns on missing optional vars
- env checker does not fail when everything is present

Implementation note:

- git behavior should be tested by mocking the internal git helper
- env behavior should be tested with `monkeypatch.setenv` / `delenv`

## Documentation updates

When implemented, update:

- `CLAUDE.md` to document the daemon startup checks and their operator-facing
  behavior
- `ARCHITECTURE.md` to mention the pre-daemon startup validation flow

The implementation should also point forward to the follow-on spec where the
env registry becomes the source for generated `.env.example` content.

## Out of scope

- auto-updating inside Docker/image deployments
- merge-based pulls or automatic conflict resolution
- stashing/rebasing local changes
- generating `.env.example`
- version-file stamping or git hooks
