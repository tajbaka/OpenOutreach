# Daemon Startup Integrity Checks — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Before the daemon starts, check the git checkout is not behind its upstream (offering or auto-running a pull), and warn the operator about missing environment variables.

**Architecture:** Two new self-contained, Django-free modules — `linkedin/version_check.py` (git fetch/compare/pull) and `linkedin/env_check.py` + `linkedin/env_spec.py` (declared env-var registry + startup warnings). Both are invoked at the top of `manage.py`'s no-args daemon branch, before `_ensure_db()`, so a pull restarts the process before migrations run on stale code.

**Tech Stack:** Python 3 stdlib (`subprocess`, `sys`, `dataclasses`), pytest, Django (only for the daemon path, not the new modules).

**Spec:** `docs/superpowers/specs/2026-05-19-daemon-startup-checks-design.md`

---

## File Structure

- **Create** `linkedin/version_check.py` — git update detection + pull. Public: `check_for_updates()`.
- **Create** `linkedin/env_spec.py` — declared `EnvVar` registry of project-owned env vars. The single source of truth (Spec 2's `.env.example` generator will consume it too).
- **Create** `linkedin/env_check.py` — startup env-var warnings. Public: `check_env_vars()`.
- **Create** `tests/test_startup_checks.py` — tests for all three modules.
- **Modify** `manage.py` — call both checks at the top of the `len(sys.argv) == 1` branch.
- **Modify** `CLAUDE.md`, `ARCHITECTURE.md` — document the startup checks.

Project conventions: `.venv/bin/python` for Python; commit messages are single-line, no `Co-Authored-By`, no conventional-commit prefix (match recent history, e.g. `Accept LinkedIn URL header in seed CSV imports`).

---

## Task 1: Git inspection helpers in `version_check.py`

**Files:**
- Create: `linkedin/version_check.py`
- Test: `tests/test_startup_checks.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_startup_checks.py`:

```python
"""Daemon startup integrity checks — git update check + env-var warnings.

`linkedin.version_check` and `linkedin.env_check` run at the top of the
daemon branch in manage.py. Both are Django-free; git behaviour is tested
by patching the `_git` seam, env behaviour with monkeypatch.setenv/delenv.
"""
import subprocess
from unittest.mock import MagicMock

import pytest

from linkedin import version_check


def _completed(stdout="", returncode=0, stderr=""):
    """Build a fake subprocess.CompletedProcess for patching `_git`."""
    return subprocess.CompletedProcess(
        args=["git"], returncode=returncode, stdout=stdout, stderr=stderr
    )


class TestGitHelpers:
    def test_is_git_checkout_true(self, monkeypatch):
        monkeypatch.setattr(
            version_check, "_git",
            lambda args, check=True: _completed(stdout="true\n"),
        )
        assert version_check._is_git_checkout() is True

    def test_is_git_checkout_false_when_not_a_repo(self, monkeypatch):
        monkeypatch.setattr(
            version_check, "_git",
            lambda args, check=True: _completed(stdout="", returncode=128),
        )
        assert version_check._is_git_checkout() is False

    def test_is_git_checkout_false_when_git_missing(self, monkeypatch):
        def _raise(args, check=True):
            raise FileNotFoundError("git")
        monkeypatch.setattr(version_check, "_git", _raise)
        assert version_check._is_git_checkout() is False

    def test_upstream_ref_returns_tracking_branch(self, monkeypatch):
        monkeypatch.setattr(
            version_check, "_git",
            lambda args, check=True: _completed(stdout="origin/main\n"),
        )
        assert version_check._upstream_ref() == "origin/main"

    def test_upstream_ref_none_when_no_upstream(self, monkeypatch):
        monkeypatch.setattr(
            version_check, "_git",
            lambda args, check=True: _completed(stdout="", returncode=128),
        )
        assert version_check._upstream_ref() is None

    def test_commits_behind_parses_count(self, monkeypatch):
        monkeypatch.setattr(
            version_check, "_git",
            lambda args, check=True: _completed(stdout="3\n"),
        )
        assert version_check._commits_behind("origin/main") == 3

    def test_commits_behind_zero_on_git_error(self, monkeypatch):
        monkeypatch.setattr(
            version_check, "_git",
            lambda args, check=True: _completed(stdout="", returncode=1),
        )
        assert version_check._commits_behind("origin/main") == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_startup_checks.py::TestGitHelpers -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'linkedin.version_check'`

- [ ] **Step 3: Write the implementation**

Create `linkedin/version_check.py`:

```python
"""Daemon startup git update check.

Called once at daemon startup (manage.py, no-args branch) BEFORE migrations
so a pull can restart the process before stale code runs. Compares the
current branch's local HEAD against its configured upstream (`@{u}`), not a
hardcoded branch — correct on any deployment branch. Django-free.

`_git` is the single seam: every git call goes through it so tests patch
one function. In Docker-style image deployments there is no `.git`, so the
whole check is a silent no-op and image deploys keep their rebuild path.
"""
import logging
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# version_check.py lives in linkedin/, the repo root is its parent.
REPO_ROOT = Path(__file__).resolve().parent.parent


def _git(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    """Run a git command in the repo root. The one seam tests patch."""
    return subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=check,
    )


def _is_git_checkout() -> bool:
    """True only if the repo root is inside a git working tree."""
    try:
        result = _git(["rev-parse", "--is-inside-work-tree"], check=False)
    except FileNotFoundError:
        return False  # git is not installed
    return result.returncode == 0 and result.stdout.strip() == "true"


def _upstream_ref() -> str | None:
    """The current branch's upstream tracking ref (e.g. 'origin/main').

    Returns None when HEAD is detached or the branch has no upstream — both
    are 'skip the check' situations, not errors.
    """
    result = _git(
        ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _commits_behind(upstream: str) -> int:
    """How many commits local HEAD is behind `upstream`. 0 on any git error."""
    result = _git(["rev-list", "--count", f"HEAD..{upstream}"], check=False)
    if result.returncode != 0:
        return 0
    return int(result.stdout.strip() or 0)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_startup_checks.py::TestGitHelpers -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add linkedin/version_check.py tests/test_startup_checks.py
git commit -m "Add git inspection helpers for daemon update check"
```

---

## Task 2: `check_for_updates()` orchestration

**Files:**
- Modify: `linkedin/version_check.py`
- Test: `tests/test_startup_checks.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_startup_checks.py`:

```python
class TestCheckForUpdates:
    def test_skips_when_not_a_git_checkout(self, monkeypatch):
        monkeypatch.setattr(version_check, "_is_git_checkout", lambda: False)
        # No exit, no exception — just returns.
        version_check.check_for_updates()

    def test_skips_when_no_upstream(self, monkeypatch):
        monkeypatch.setattr(version_check, "_is_git_checkout", lambda: True)
        monkeypatch.setattr(version_check, "_upstream_ref", lambda: None)
        version_check.check_for_updates()

    def test_continues_when_fetch_fails(self, monkeypatch):
        monkeypatch.setattr(version_check, "_is_git_checkout", lambda: True)
        monkeypatch.setattr(version_check, "_upstream_ref", lambda: "origin/main")
        monkeypatch.setattr(
            version_check, "_git",
            lambda args, check=True: _completed(returncode=1, stderr="no network"),
        )
        # fetch failed -> warn and continue, no exit.
        version_check.check_for_updates()

    def test_continues_when_up_to_date(self, monkeypatch):
        monkeypatch.setattr(version_check, "_is_git_checkout", lambda: True)
        monkeypatch.setattr(version_check, "_upstream_ref", lambda: "origin/main")
        monkeypatch.setattr(
            version_check, "_git",
            lambda args, check=True: _completed(returncode=0),
        )
        monkeypatch.setattr(version_check, "_commits_behind", lambda u: 0)
        version_check.check_for_updates()

    def test_interactive_decline_continues(self, monkeypatch):
        monkeypatch.setattr(version_check, "_is_git_checkout", lambda: True)
        monkeypatch.setattr(version_check, "_upstream_ref", lambda: "origin/main")
        monkeypatch.setattr(
            version_check, "_git",
            lambda args, check=True: _completed(returncode=0),
        )
        monkeypatch.setattr(version_check, "_commits_behind", lambda u: 2)
        monkeypatch.setattr(version_check, "_stdio_is_tty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda prompt="": "n")
        pull = MagicMock()
        monkeypatch.setattr(version_check, "_pull_and_exit", pull)
        version_check.check_for_updates()
        pull.assert_not_called()

    def test_interactive_accept_pulls(self, monkeypatch):
        monkeypatch.setattr(version_check, "_is_git_checkout", lambda: True)
        monkeypatch.setattr(version_check, "_upstream_ref", lambda: "origin/main")
        monkeypatch.setattr(
            version_check, "_git",
            lambda args, check=True: _completed(returncode=0),
        )
        monkeypatch.setattr(version_check, "_commits_behind", lambda u: 2)
        monkeypatch.setattr(version_check, "_stdio_is_tty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda prompt="": "y")
        pull = MagicMock()
        monkeypatch.setattr(version_check, "_pull_and_exit", pull)
        version_check.check_for_updates()
        pull.assert_called_once()

    def test_headless_auto_pulls(self, monkeypatch):
        monkeypatch.setattr(version_check, "_is_git_checkout", lambda: True)
        monkeypatch.setattr(version_check, "_upstream_ref", lambda: "origin/main")
        monkeypatch.setattr(
            version_check, "_git",
            lambda args, check=True: _completed(returncode=0),
        )
        monkeypatch.setattr(version_check, "_commits_behind", lambda u: 5)
        monkeypatch.setattr(version_check, "_stdio_is_tty", lambda: False)
        pull = MagicMock()
        monkeypatch.setattr(version_check, "_pull_and_exit", pull)
        version_check.check_for_updates()
        pull.assert_called_once()

    def test_pull_success_exits_zero(self, monkeypatch):
        monkeypatch.setattr(
            version_check, "_git",
            lambda args, check=True: _completed(returncode=0),
        )
        with pytest.raises(SystemExit) as exc:
            version_check._pull_and_exit()
        assert exc.value.code == 0

    def test_pull_failure_notifies_and_exits_one(self, monkeypatch):
        def _raise(args, check=True):
            raise subprocess.CalledProcessError(
                returncode=1, cmd=["git", "pull"], stderr="local changes",
            )
        monkeypatch.setattr(version_check, "_git", _raise)
        notify = MagicMock()
        monkeypatch.setattr("linkedin.notifications.slack.notify_error", notify)
        with pytest.raises(SystemExit) as exc:
            version_check._pull_and_exit()
        assert exc.value.code == 1
        notify.assert_called_once()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_startup_checks.py::TestCheckForUpdates -v`
Expected: FAIL — `AttributeError: module 'linkedin.version_check' has no attribute 'check_for_updates'`

- [ ] **Step 3: Write the implementation**

Append to `linkedin/version_check.py`:

```python
def _stdio_is_tty() -> bool:
    """True only when both stdin and stdout are interactive terminals.

    Wrapped as a function so tests patch one seam instead of sys streams.
    """
    return sys.stdin.isatty() and sys.stdout.isatty()


def _pull_and_exit() -> None:
    """Run `git pull --ff-only`, then exit the process.

    Success -> exit 0: imported modules still hold the OLD code, so the
    process MUST restart to run the pulled code. Failure (merge conflict,
    dirty tree blocking the pull) -> loud log + Slack + exit 1, per spec.
    """
    try:
        _git(["pull", "--ff-only"])
    except subprocess.CalledProcessError as exc:
        # Lazy import: keeps version_check.py Django-free for fast tests.
        from linkedin.notifications.slack import notify_error
        logger.error(
            "version check: 'git pull --ff-only' failed:\n%s",
            (exc.stderr or "").strip(),
        )
        notify_error(
            "daemon:update", exc,
            context={"stderr": (exc.stderr or "").strip()[:500]},
        )
        sys.exit(1)
    logger.info("version check: updated — restart required, exiting")
    sys.exit(0)


def check_for_updates() -> None:
    """Daemon startup update check. Called once from manage.py.

    May call sys.exit(0) after a successful pull or sys.exit(1) on a failed
    pull. Returns normally in every other case (skip / up-to-date / fetch
    failed / interactive decline) so the daemon continues on current code.
    """
    if not _is_git_checkout():
        logger.debug("version check: not a git checkout, skipping")
        return

    upstream = _upstream_ref()
    if upstream is None:
        logger.debug("version check: no upstream tracking branch, skipping")
        return

    fetch = _git(["fetch"], check=False)
    if fetch.returncode != 0:
        logger.warning(
            "version check: 'git fetch' failed, continuing on current code: %s",
            fetch.stderr.strip(),
        )
        return

    behind = _commits_behind(upstream)
    if behind == 0:
        logger.debug("version check: up to date with %s", upstream)
        return

    logger.warning(
        "version check: %d new commit(s) available on %s", behind, upstream
    )

    if _stdio_is_tty():
        answer = input(
            f"Pull {behind} new commit(s) and restart? [y/N] "
        ).strip().lower()
        if answer not in {"y", "yes"}:
            logger.warning(
                "version check: update declined, continuing on current code"
            )
            return
    else:
        logger.info(
            "version check: no TTY, auto-pulling %d commit(s)", behind
        )

    _pull_and_exit()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_startup_checks.py::TestCheckForUpdates -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Commit**

```bash
git add linkedin/version_check.py tests/test_startup_checks.py
git commit -m "Add check_for_updates orchestration for daemon update check"
```

---

## Task 3: Wire `check_for_updates()` into `manage.py`

**Files:**
- Modify: `manage.py` (the `if __name__ == "__main__":` block, `len(sys.argv) == 1` branch)

- [ ] **Step 1: Read the current daemon branch**

Run: `.venv/bin/python -c "import pathlib; print(pathlib.Path('manage.py').read_text()[-700:])"`
Confirm the branch reads:

```python
if __name__ == "__main__":
    if len(sys.argv) == 1:
        # No arguments → run the daemon. ...
        from linkedin.notifications.slack import notify_on_error
        _ensure_db()
        with notify_on_error("daemon:startup"):
            _run_daemon()
```

- [ ] **Step 2: Add the call before `_ensure_db()`**

Edit `manage.py` — replace:

```python
    if len(sys.argv) == 1:
        # No arguments → run the daemon. Top-level Exception goes to Slack
        # before re-raising so an operator sees the crash even when the
        # process logs scroll off. KeyboardInterrupt / SystemExit pass
        # through untouched.
        from linkedin.notifications.slack import notify_on_error
        _ensure_db()
```

with:

```python
    if len(sys.argv) == 1:
        # No arguments → run the daemon. Top-level Exception goes to Slack
        # before re-raising so an operator sees the crash even when the
        # process logs scroll off. KeyboardInterrupt / SystemExit pass
        # through untouched.
        #
        # Startup integrity checks run FIRST, before migrations: the update
        # check may exit so the process restarts on freshly pulled code.
        from linkedin.version_check import check_for_updates
        check_for_updates()
        from linkedin.notifications.slack import notify_on_error
        _ensure_db()
```

- [ ] **Step 3: Verify the daemon still imports cleanly**

Run: `.venv/bin/python -c "import ast; ast.parse(open('manage.py').read()); print('manage.py parses OK')"`
Expected: `manage.py parses OK`

- [ ] **Step 4: Run the full startup-checks test file**

Run: `.venv/bin/python -m pytest tests/test_startup_checks.py -v`
Expected: PASS (all tests so far)

- [ ] **Step 5: Commit**

```bash
git add manage.py
git commit -m "Run update check at daemon startup before migrations"
```

---

## Task 4: Env-var registry `env_spec.py`

**Files:**
- Create: `linkedin/env_spec.py`
- Test: `tests/test_startup_checks.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_startup_checks.py`:

```python
from linkedin import env_spec


class TestEnvSpec:
    def test_registry_is_non_empty(self):
        assert len(env_spec.ENV_VARS) > 0

    def test_var_names_are_unique(self):
        names = [v.name for v in env_spec.ENV_VARS]
        assert len(names) == len(set(names))

    def test_known_required_vars_present(self):
        required = {v.name for v in env_spec.ENV_VARS if v.required}
        assert {"LLM_API_KEY", "LINKEDIN_USERNAME", "LINKEDIN_PASSWORD"} <= required

    def test_django_internal_vars_excluded(self):
        names = {v.name for v in env_spec.ENV_VARS}
        assert "DJANGO_SETTINGS_MODULE" not in names
        assert "DJANGO_ALLOW_ASYNC_UNSAFE" not in names

    def test_every_var_has_a_group_and_description(self):
        for v in env_spec.ENV_VARS:
            assert v.group, f"{v.name} missing group"
            assert v.description, f"{v.name} missing description"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_startup_checks.py::TestEnvSpec -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'linkedin.env_spec'`

- [ ] **Step 3: Write the implementation**

Create `linkedin/env_spec.py`:

```python
"""Declared registry of project-owned environment variables.

The single source of truth for env-var metadata. `env_check.py` uses it for
startup warnings; Spec 2's `.env.example` generator will consume the same
registry. Only variables the OpenOutreach code actually reads belong here —
not transitive library/test noise, and not code-set vars like
DJANGO_SETTINGS_MODULE.

`required` means the daemon cannot do useful work without it. `default`
mirrors the fallback the code applies when the var is unset (None = no
fallback; the integration is simply inert when missing).
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class EnvVar:
    name: str
    required: bool
    secret: bool
    default: str | None
    group: str
    description: str


ENV_VARS: tuple[EnvVar, ...] = (
    # --- credentials -------------------------------------------------------
    EnvVar("LINKEDIN_USERNAME", True, False, None, "credentials",
           "Primary LinkedIn account the daemon runs as."),
    EnvVar("LINKEDIN_PASSWORD", True, True, None, "credentials",
           "Password for the primary LinkedIn account."),
    EnvVar("BACKFILL_LINKEDIN_USERNAME", False, False, None, "credentials",
           "Second LinkedIn account used by backfill_messages / import_connections."),
    EnvVar("BACKFILL_LINKEDIN_PASSWORD", False, True, None, "credentials",
           "Password for the backfill LinkedIn account."),
    EnvVar("SALES_NAV_LINKEDIN_USERNAME", False, False, None, "credentials",
           "LinkedIn account used for Sales Navigator export commands."),
    EnvVar("SALES_NAV_LINKEDIN_PASSWORD", False, True, None, "credentials",
           "Password for the Sales Navigator account."),
    # --- llm ---------------------------------------------------------------
    EnvVar("LLM_API_KEY", True, True, None, "llm",
           "API key for the LLM provider; daemon hard-exits without it."),
    EnvVar("AI_MODEL", True, False, None, "llm",
           "LLM model id used for qualification and synthesis."),
    EnvVar("LLM_API_BASE", False, False, None, "llm",
           "Override base URL for the LLM provider (optional)."),
    # --- database ----------------------------------------------------------
    EnvVar("DATABASE_URL", False, True, None, "database",
           "Postgres connection string; falls back to local SQLite if unset."),
    # --- slack -------------------------------------------------------------
    EnvVar("SLACK_WEBHOOK_URL", False, True, None, "slack",
           "Ops channel webhook: errors, new connections, monitoring, sweeps."),
    EnvVar("SLACK_REPLIES_WEBHOOK_URL", False, True, None, "slack",
           "Replies channel webhook: inbound DM + phone-enrichment notices."),
    EnvVar("SLACK_SIGNING_SECRET", False, True, None, "slack",
           "HMAC secret for the Vercel slack_enrich function (set on Vercel)."),
    # --- google sheets -----------------------------------------------------
    EnvVar("GOOGLE_SHEETS_ID", False, False, None, "sheets",
           "Spreadsheet id for the CRM sheet sync; unset disables the sync."),
    EnvVar("GOOGLE_SHEETS_CREDENTIALS_PATH", False, False, None, "sheets",
           "Path to the service-account JSON key for Sheets access."),
    EnvVar("GOOGLE_SHEETS_TAB_NAME", False, False, "People", "sheets",
           "Worksheet tab name the sync writes to."),
    # --- company metadata --------------------------------------------------
    EnvVar("OUR_COMPANY_NAME", False, False, "", "company",
           "Company name substituted into outbound templates."),
    EnvVar("OUR_WEBSITE_URL", False, False, "", "company",
           "Company URL substituted into outbound templates."),
    # --- enrichment provider keys ------------------------------------------
    EnvVar("BETTERCONTACT_API_KEY", False, True, None, "enrichment",
           "API key for the BetterContact phone-enrichment provider."),
    EnvVar("LEADMAGIC_API_KEY", False, True, None, "enrichment",
           "API key for the LeadMagic phone-enrichment provider."),
    EnvVar("PROSPEO_API_KEY", False, True, None, "enrichment",
           "API key for the Prospeo phone-enrichment provider."),
    # --- feature flags -----------------------------------------------------
    EnvVar("ENABLE_CONNECT", False, False, "true", "feature_flags",
           "Enable the connect (invite) task lane."),
    EnvVar("ENABLE_SWEEP_CONNECTIONS", False, False, "true", "feature_flags",
           "Enable the bulk connection-sweep task."),
    EnvVar("ENABLE_FOLLOW_UP", False, False, "true", "feature_flags",
           "Enable the post-accept follow-up DM."),
    EnvVar("ENABLE_AUTO_DISCOVERY", False, False, "true", "feature_flags",
           "Enable automatic lead discovery."),
    EnvVar("ENABLE_REALTIME_LISTENER", False, False, "false", "feature_flags",
           "Enable the realtime inbound-DM listener child process."),
    EnvVar("ENABLE_NODE_MONITOR", False, False, "true", "feature_flags",
           "Enable peer-node liveness and degraded-state monitoring."),
    EnvVar("ENABLE_ACTIVE_HOURS", False, False, "true", "feature_flags",
           "Restrict daemon work to configured active hours."),
    EnvVar("ENABLE_FREEMIUM_CAMPAIGN", False, False, "false", "feature_flags",
           "Enable the freemium campaign path."),
    EnvVar("ENABLE_AUTO_PHONE_ENRICHMENT", False, False, "false", "feature_flags",
           "Auto-enqueue a waterfall enrichment task on every inbound reply."),
    # --- rate limits -------------------------------------------------------
    EnvVar("MAX_TOTAL_DAILY_ACTIONS", False, False, "200", "limits",
           "Hard cap on total daemon actions per day."),
    EnvVar("CONNECT_DAILY_LIMIT", False, False, None, "limits",
           "Daily connect-invite cap (0/unset = no extra cap)."),
    EnvVar("CONNECT_WEEKLY_LIMIT", False, False, None, "limits",
           "Weekly connect-invite cap (0/unset = no extra cap)."),
    EnvVar("FOLLOW_UP_DAILY_LIMIT", False, False, None, "limits",
           "Daily follow-up DM cap (0/unset = no extra cap)."),
    EnvVar("CONNECTION_SWEEP_INTERVAL_HOURS", False, False, "2", "limits",
           "Hours between connection sweeps."),
    # --- schedule ----------------------------------------------------------
    EnvVar("ACTIVE_START_HOUR", False, False, "9", "schedule",
           "First active hour, inclusive, local time."),
    EnvVar("ACTIVE_END_HOUR", False, False, "17", "schedule",
           "Last active hour, exclusive, local time."),
    EnvVar("ACTIVE_TIMEZONE", False, False, "America/Toronto", "schedule",
           "IANA timezone for active-hours and rest-day calculations."),
    EnvVar("REST_DAYS", False, False, "5,6", "schedule",
           "Comma-separated weekday indices the daemon rests (Mon=0)."),
    # --- realtime listener -------------------------------------------------
    EnvVar("LISTENER_CDP_PORT", False, False, "9222", "realtime",
           "Chrome remote-debugging port the listener connects over."),
    EnvVar("LISTENER_CATCHUP_GAP_MINUTES", False, False, "30", "realtime",
           "Off-hours gap the startup catch-up surfaces."),
    EnvVar("LISTENER_PUMP_SLICE_SECONDS", False, False, "30", "realtime",
           "SSE pump slice length in seconds."),
    EnvVar("LISTENER_HEARTBEAT_STALE_MINUTES", False, False, "30", "realtime",
           "Listener heartbeat age that flags it as stalled."),
    # --- enrichment tuning -------------------------------------------------
    EnvVar("ENRICHMENT_MAX_DURATION_SECONDS", False, False, "600", "enrichment_tuning",
           "Max wall-clock for one enrichment task."),
    EnvVar("ENRICHMENT_HTTP_TIMEOUT_SECONDS", False, False, "5", "enrichment_tuning",
           "Per-request HTTP timeout for enrichment providers."),
    EnvVar("ENRICHMENT_WAIT_POLL_SECONDS", False, False, "30", "enrichment_tuning",
           "Poll interval while waiting on an enrichment result."),
    EnvVar("BETTERCONTACT_POLL_INTERVAL_SECONDS", False, False, "15", "enrichment_tuning",
           "Poll interval for BetterContact's async job API."),
    # --- node monitoring ---------------------------------------------------
    EnvVar("MONITOR_INTERVAL_SECONDS", False, False, "300", "monitoring",
           "Heartbeat/scan interval for the node monitor."),
    EnvVar("PEER_STALE_MINUTES", False, False, "15", "monitoring",
           "Peer heartbeat age that reports the peer node down."),
    EnvVar("DEGRADED_REALERT_HOURS", False, False, "6", "monitoring",
           "Cooldown before re-alerting an ongoing outage."),
    EnvVar("TASK_FAILURE_STREAK_THRESHOLD", False, False, "5", "monitoring",
           "Consecutive task failures that trigger a degraded alert."),
)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_startup_checks.py::TestEnvSpec -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add linkedin/env_spec.py tests/test_startup_checks.py
git commit -m "Add declared env-var registry"
```

---

## Task 5: `check_env_vars()` startup warning

**Files:**
- Create: `linkedin/env_check.py`
- Test: `tests/test_startup_checks.py`

Behavior: missing **required** var → include in the warning summary, log at WARNING. Missing **optional var with no default** → include as "missing optional", but if nothing required is missing the summary logs at INFO. Missing optional var **that has a default** → silently fine (it just uses its default), never reported. Never aborts startup.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_startup_checks.py`:

```python
from linkedin import env_check


class TestCheckEnvVars:
    def _isolate_env(self, monkeypatch):
        """Clear every registered var so each test controls the environment."""
        for v in env_spec.ENV_VARS:
            monkeypatch.delenv(v.name, raising=False)

    def test_warns_when_required_var_missing(self, monkeypatch, caplog):
        self._isolate_env(monkeypatch)
        with caplog.at_level("WARNING", logger="linkedin.env_check"):
            env_check.check_env_vars()
        assert "LLM_API_KEY" in caplog.text
        assert any(r.levelname == "WARNING" for r in caplog.records)

    def test_optional_without_default_reported_as_optional(self, monkeypatch, caplog):
        self._isolate_env(monkeypatch)
        # Satisfy every required var so the summary is INFO-level.
        for v in env_spec.ENV_VARS:
            if v.required:
                monkeypatch.setenv(v.name, "x")
        with caplog.at_level("INFO", logger="linkedin.env_check"):
            env_check.check_env_vars()
        # GOOGLE_SHEETS_ID is optional with no default -> reported.
        assert "GOOGLE_SHEETS_ID" in caplog.text

    def test_optional_with_default_not_reported(self, monkeypatch, caplog):
        self._isolate_env(monkeypatch)
        for v in env_spec.ENV_VARS:
            if v.required:
                monkeypatch.setenv(v.name, "x")
        with caplog.at_level("INFO", logger="linkedin.env_check"):
            env_check.check_env_vars()
        # GOOGLE_SHEETS_TAB_NAME is optional WITH a default -> not reported.
        assert "GOOGLE_SHEETS_TAB_NAME" not in caplog.text

    def test_silent_when_all_present(self, monkeypatch, caplog):
        for v in env_spec.ENV_VARS:
            monkeypatch.setenv(v.name, "value")
        with caplog.at_level("DEBUG", logger="linkedin.env_check"):
            env_check.check_env_vars()
        assert not any(r.levelname == "WARNING" for r in caplog.records)

    def test_never_raises(self, monkeypatch):
        self._isolate_env(monkeypatch)
        # Must return None, never raise — it only warns.
        assert env_check.check_env_vars() is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_startup_checks.py::TestCheckEnvVars -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'linkedin.env_check'`

- [ ] **Step 3: Write the implementation**

Create `linkedin/env_check.py`:

```python
"""Daemon startup environment-variable warnings.

Iterates the `env_spec.ENV_VARS` registry at daemon startup and logs one
grouped summary of missing variables. Advisory only — it never aborts
startup. Existing hard requirements (e.g. LLM_API_KEY hard-exit in
manage.py) keep their own enforcement; this check just surfaces the full
picture early, in one operator-readable block.
"""
import logging
import os

from linkedin.env_spec import ENV_VARS

logger = logging.getLogger(__name__)


def check_env_vars() -> None:
    """Log a summary of missing env vars. Returns None, never raises.

    A missing optional var that has a default is NOT reported — it simply
    uses its fallback. Only missing required vars and missing optional vars
    with no default (inert integrations) appear in the summary.
    """
    missing_required = []
    missing_optional = []
    for var in ENV_VARS:
        if os.getenv(var.name, "").strip():
            continue
        if var.required:
            missing_required.append(var)
        elif var.default is None:
            missing_optional.append(var)
        # else: optional with a default -> fine, not reported.

    if not missing_required and not missing_optional:
        logger.debug("env check: all required env vars present")
        return

    lines = ["env check: environment variable summary"]
    if missing_required:
        lines.append(f"  MISSING REQUIRED ({len(missing_required)}):")
        for v in missing_required:
            lines.append(f"    - {v.name} [{v.group}] — {v.description}")
    if missing_optional:
        lines.append(f"  missing optional integrations ({len(missing_optional)}):")
        for v in missing_optional:
            lines.append(f"    - {v.name} [{v.group}] — {v.description}")
    lines.append("  (declared in linkedin/env_spec.py)")
    summary = "\n".join(lines)

    if missing_required:
        logger.warning(summary)
    else:
        logger.info(summary)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_startup_checks.py::TestCheckEnvVars -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add linkedin/env_check.py tests/test_startup_checks.py
git commit -m "Add startup env-var checker"
```

---

## Task 6: Wire `check_env_vars()` into `manage.py`

**Files:**
- Modify: `manage.py` (the `len(sys.argv) == 1` branch, after `check_for_updates()`)

- [ ] **Step 1: Add the call after `check_for_updates()`**

Edit `manage.py` — replace:

```python
        from linkedin.version_check import check_for_updates
        check_for_updates()
        from linkedin.notifications.slack import notify_on_error
        _ensure_db()
```

with:

```python
        from linkedin.version_check import check_for_updates
        from linkedin.env_check import check_env_vars
        check_for_updates()
        check_env_vars()
        from linkedin.notifications.slack import notify_on_error
        _ensure_db()
```

- [ ] **Step 2: Verify `manage.py` parses**

Run: `.venv/bin/python -c "import ast; ast.parse(open('manage.py').read()); print('manage.py parses OK')"`
Expected: `manage.py parses OK`

- [ ] **Step 3: Run the full startup-checks test file**

Run: `.venv/bin/python -m pytest tests/test_startup_checks.py -v`
Expected: PASS (all tests — 31 total)

- [ ] **Step 4: Run the whole suite to confirm nothing regressed**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS — no new failures vs. baseline.

- [ ] **Step 5: Commit**

```bash
git add manage.py
git commit -m "Run env-var checker at daemon startup"
```

---

## Task 7: Documentation

**Files:**
- Modify: `CLAUDE.md` (Architecture quick-reference — the `Entry` bullet)
- Modify: `ARCHITECTURE.md`

- [ ] **Step 1: Update the `Entry` bullet in `CLAUDE.md`**

In `CLAUDE.md`, find the `- **Entry**:` bullet under "Architecture (quick reference)" and replace it with:

```markdown
- **Entry**: `manage.py` — no args runs daemon (startup checks → onboarding → browser → task queue loop); with args delegates to Django CLI. Auto-migrates + CRM bootstrap on startup. **Startup integrity checks** run first in the no-args branch, before migrations: `linkedin/version_check.py:check_for_updates()` compares the checkout against its git upstream (`@{u}`) and, when behind, prompts on a TTY or auto-pulls headless — a successful `git pull --ff-only` exits 0 (process must restart for new code), a failed pull exits 1 after a Slack `notify_error`; it is a silent no-op when there is no `.git` (Docker images). `linkedin/env_check.py:check_env_vars()` then logs one grouped warning for missing env vars declared in `linkedin/env_spec.py` (advisory only, never aborts). `env_spec.py` is the canonical env-var registry.
```

- [ ] **Step 2: Add a startup-checks section to `ARCHITECTURE.md`**

Run: `.venv/bin/python -c "import pathlib; t=pathlib.Path('ARCHITECTURE.md').read_text(); print(t[:600])"`
Identify the section that documents `manage.py` / daemon entry, and add this subsection immediately after it:

```markdown
### Startup integrity checks

Before the daemon does any work, `manage.py`'s no-args branch runs two
checks (both Django-free modules, both invoked before `_ensure_db()`):

- `linkedin/version_check.py` — `check_for_updates()` runs `git fetch` and
  compares local `HEAD` to the current branch's upstream `@{u}`. When
  behind: a TTY session is prompted to pull; a headless run auto-pulls.
  A successful `git pull --ff-only` calls `sys.exit(0)` (imported modules
  still hold the old code — the process must restart); a failed pull logs
  loudly, posts `notify_error`, and `sys.exit(1)`. No `.git` directory
  (Docker image deployments) → silent no-op.
- `linkedin/env_check.py` — `check_env_vars()` logs one grouped summary of
  missing environment variables. Advisory only; never aborts startup.
- `linkedin/env_spec.py` — the declared `EnvVar` registry that
  `check_env_vars()` reads. Single source of truth for project-owned env
  vars (a future `.env.example` generator will consume it too).
```

- [ ] **Step 3: Verify the docs render**

Run: `grep -n "Startup integrity checks" CLAUDE.md ARCHITECTURE.md`
Expected: a match in each file.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md ARCHITECTURE.md
git commit -m "Document daemon startup integrity checks"
```

---

## Self-Review

**Spec coverage:**
- Update check at top of daemon branch before `_ensure_db()` → Task 2 + Task 3 ✓
- Git upstream comparison (`HEAD` vs `@{u}`) → Task 1 (`_upstream_ref`, `_commits_behind`) ✓
- Skip when not a git checkout / no upstream → Task 2 (`check_for_updates`) ✓
- Fetch failure warn-and-continue → Task 2 ✓
- TTY prompt / headless auto-pull → Task 2 ✓
- `git pull --ff-only`, exit 0 on success → Task 2 (`_pull_and_exit`) ✓
- Pull failure → loud log + `notify_error` + exit 1 → Task 2 ✓
- Dirty tree: pull anyway, failure handled as pull failure → Task 2 (no pre-check; `CalledProcessError` path) ✓
- Env-var checker, warn-only → Task 5 ✓
- Declared registry as single source of truth → Task 4 ✓
- Tests in `tests/test_startup_checks.py` → every task ✓
- Docs (`CLAUDE.md`, `ARCHITECTURE.md`) → Task 7 ✓

**Placeholder scan:** No TBD/TODO; every code step shows complete code.

**Type consistency:** `_git(args, *, check=True)` signature is used consistently in Tasks 1–2 and patched identically in tests. `EnvVar(name, required, secret, default, group, description)` field order matches between `env_spec.py` definition (Task 4) and all positional constructions. `check_for_updates()` / `check_env_vars()` / `_pull_and_exit()` / `_stdio_is_tty()` names match between implementation, tests, and `manage.py` wiring.

**Spec note carried forward:** the exit-1-on-pull-failure crash-loop risk under `restart: always` is an accepted tradeoff (documented in the spec, §"Crash-loop note") — no task mitigates it by design.
