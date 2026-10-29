# Claude Code CRM review runner

Runs the daily contextual review with Claude Code instead of the Codex
automation: first [Actions](actions-workflow.md), then the
[Active Accounts Next step](active-account-next-steps-workflow.md) review, using
the requests in [contextual-crm-review-prompts.md](contextual-crm-review-prompts.md).
The workflow rules are unchanged; only the agent and scheduler differ.

| File | Purpose |
| --- | --- |
| `scripts/claude_crm_review_prompt.md` | Self-contained run prompt (`{{MODE}}`, `{{PYTHON}}` filled by the wrapper) |
| `scripts/claude_crm_review_settings.json` | Headless permissions: Python, read tools and receipt writes only |
| `scripts/run_claude_crm_review.ps1` | Windows runner (`-Mode Preview` default, `-Mode Apply` scheduled) |
| `scripts/run_claude_crm_review.sh` | macOS/Linux runner (`preview` default, `apply`) |
| `scripts/register_claude_crm_review.ps1` | Registers/removes the Windows Scheduled Task |

## Safety model

- `claude -p` runs with `--permission-mode dontAsk`, so anything not allowed by
  the settings file is refused rather than prompted. It may run only the
  checkout's Python, read files, and write under `artifacts/followups/` and
  `artifacts/claude-review/`. It cannot read `.env`, `secrets/` or cookie files
  directly, and it loads no MCP servers (`--strict-mcp-config`).
- `--setting-sources project` ignores user-level settings, hooks and plugins on
  the host, so runs behave the same on every computer.
- Preview mode asks for no Sheet writes. This is an instruction, not a technical
  block: the same Python access is needed to read the Sheets.
- Apply refuses to start while the Codex automation
  `review-followups-across-linkedin-and-email` is `ACTIVE` on the same host, and a
  lock folder prevents overlapping runs. Pause the runner on any other host
  yourself; only one runner may be enabled.

## Set up on the receiving computer (Windows)

1. Pull this checkout. It needs its existing `.env`, database connection, CRM
   service account, Arian Gmail OAuth token and Granola API key.
2. Install Claude Code and sign in as the user who will own the task:
   `claude auth login`, then confirm with `claude auth status`. Claude Code on
   Windows also needs Git for Windows (it runs commands through Git Bash).
3. Preview from the checkout root and read the report:

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_claude_crm_review.ps1
   Get-Content artifacts\claude-review\latest.md
   ```

   Expect `STATUS: PREVIEW`, the proposed Actions/Next step changes and a
   receipts path. Check `artifacts\claude-review\runs.log` for errors.
4. Pause the Codex automation "Review contextual Actions and account next steps"
   in Codex.
5. Register the daily task (09:00 local; set the computer to America/Toronto):

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register_claude_crm_review.ps1
   ```

To roll back, run the register script with `-Unregister`, then re-enable the
Codex automation.

## Reading results

Each run writes `artifacts/claude-review/<date>/<run-id>.md` and
`artifacts/claude-review/latest.md`, and appends to `runs.log`. The report's
first line is `STATUS: QUIET`, `CHANGES`, `PREVIEW` or `BLOCKED`. The runner
exits 0 for the first three, 2 for `BLOCKED`, 3 for a missing status and the
Claude exit code if Claude itself failed, so Task Scheduler shows failures.
Private receipts stay in `artifacts/followups/` as the workflow docs require.
