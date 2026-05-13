"""Workflow prerequisite staleness checks.

Each of our operator-run workflows (import_connections → backfill_messages →
data-sync → followup) writes a `linkedin.WorkflowRun` row on completion.
This module reads those rows to tell the operator, at the START of a run,
whether the upstream workflows are fresh enough that this run will produce
quality output.

Dependency graph (one source of truth, edit `WORKFLOW_PREREQS` when adding
a new workflow):

  followup           depends on  data-sync, backfill-messages, import-connections
  data-sync          depends on  backfill-messages, import-connections
  backfill-messages  depends on  import-connections
  import-connections depends on  (nothing — entry point)

The check is **per-operator** for per-operator workflows (data-sync,
backfill-messages, import-connections) and **global** for global workflows
(followup). The followup workflow's Phase 0.5 was the original consumer;
this module generalizes that logic so management commands can reuse it
without copy-pasting the inline block.

UX model: print a human-readable staleness report to stdout, then prompt
the operator interactively to continue or abort. On non-TTY stdin (cron,
pipe), auto-continue with a stderr warning so automated runs aren't
blocked on input(). The prompt always returns True if there are no
warnings.

Wire-up pattern (management commands):

    from linkedin.workflow_prereqs import run_prereq_gate
    if not run_prereq_gate("backfill-messages", operator="Arian"):
        return  # operator chose abort

Wire-up pattern (doc-driven workflows in manage.py shell):

    from linkedin.workflow_prereqs import check_prereqs, format_report
    report = check_prereqs("followup", operator="")
    print(format_report(report))
"""
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Literal

logger = logging.getLogger(__name__)


# Workflows we treat as global (single timeline, operator="" on WorkflowRun).
# Everything else is per-operator and writes WorkflowRun rows tagged with
# the operator's canonical handle ("Chuka" / "Arian" / ...).
GLOBAL_WORKFLOWS = {"followup"}

# Source of truth: which workflows depend on which upstream ones.
# Order in the list = order shown in the report.
WORKFLOW_PREREQS: dict[str, list[str]] = {
    "followup":           ["data-sync", "backfill-messages", "import-connections"],
    "data-sync":          ["backfill-messages", "import-connections"],
    "backfill-messages":  ["import-connections"],
    "import-connections": [],
    # Sales-nav scrapes are one-off; intentionally not in the graph yet.
}

# How old a prereq has to be before it's flagged. 7 days is enough cadence
# that a once-a-week ops rhythm won't trigger warnings, but stale prereqs
# from "last month" do.
STALE_AFTER_HOURS = 24 * 7


@dataclass
class PrereqStatus:
    """One row in the staleness report."""

    workflow: str
    operator: str  # "" for global workflows
    status: Literal["fresh", "stale", "never"]
    completed_at: datetime | None
    age_hours: float | None

    @property
    def is_warning(self) -> bool:
        return self.status in ("stale", "never")


@dataclass
class StalenessReport:
    workflow: str         # the workflow that's about to run
    operator: str         # the operator about to run it ("" for global)
    rows: list[PrereqStatus]

    @property
    def has_warnings(self) -> bool:
        return any(r.is_warning for r in self.rows)


def check_prereqs(workflow: str, operator: str = "") -> StalenessReport:
    """Build a staleness report for `workflow`'s upstream dependencies.

    `operator` is the canonical handle ("Chuka" / "Arian" / ...) of who's
    about to run this workflow. Per-operator prereqs (data-sync,
    backfill-messages, import-connections) are checked against that
    operator's WorkflowRun rows. Global prereqs (followup) are checked
    against the operator="" timeline regardless.

    Empty operator + per-operator prereq is a programmer error — log a
    warning and treat the prereq as never-run so the report still surfaces.
    """
    from linkedin.models import WorkflowRun  # local import — avoid Django bootstrap at module-import time

    now = datetime.now(timezone.utc)
    prereq_names = WORKFLOW_PREREQS.get(workflow, [])
    rows: list[PrereqStatus] = []

    for prereq in prereq_names:
        if prereq in GLOBAL_WORKFLOWS:
            qs = WorkflowRun.objects.filter(name=prereq, operator="")
            row_operator = ""
        else:
            if not operator:
                logger.warning(
                    "check_prereqs(%r) called with empty operator but %r is per-operator — "
                    "treating as never-run", workflow, prereq,
                )
                rows.append(PrereqStatus(prereq, "", "never", None, None))
                continue
            qs = WorkflowRun.objects.filter(name=prereq, operator=operator)
            row_operator = operator

        latest = qs.order_by("-completed_at").first()
        if latest is None:
            rows.append(PrereqStatus(prereq, row_operator, "never", None, None))
            continue

        age = now - latest.completed_at
        age_hours = age.total_seconds() / 3600
        status: Literal["fresh", "stale"]
        if age > timedelta(hours=STALE_AFTER_HOURS):
            status = "stale"
        else:
            status = "fresh"
        rows.append(PrereqStatus(prereq, row_operator, status, latest.completed_at, age_hours))

    return StalenessReport(workflow=workflow, operator=operator, rows=rows)


def format_report(report: StalenessReport) -> str:
    """Render the report as a multi-line string for stdout.

    Shape (omits the header when there are no upstream prereqs):

        Prerequisite check for `backfill-messages` (operator: Arian):
          ✓ import-connections    fresh   (ran 26h ago — 2026-05-11 20:50 UTC)
          ⚠ data-sync             stale   (ran 9d ago — 2026-05-03 14:22 UTC)
          ⚠ import-connections    NEVER run for Chuka
    """
    if not report.rows:
        return f"Prerequisite check for `{report.workflow}`: no upstream prereqs."

    op_suffix = f" (operator: {report.operator})" if report.operator else " (global)"
    lines = [f"Prerequisite check for `{report.workflow}`{op_suffix}:"]

    name_width = max(len(r.workflow) for r in report.rows)
    for r in report.rows:
        bullet = "⚠" if r.is_warning else "✓"
        name = r.workflow.ljust(name_width)
        if r.status == "never":
            who = f"for {r.operator}" if r.operator else ""
            lines.append(f"  {bullet} {name}  NEVER run {who}".rstrip())
        else:
            age = _humanize_age(r.age_hours or 0)
            ts = r.completed_at.strftime("%Y-%m-%d %H:%M UTC") if r.completed_at else "?"
            label = "fresh" if r.status == "fresh" else "STALE"
            lines.append(f"  {bullet} {name}  {label}   (ran {age} ago — {ts})")

    return "\n".join(lines)


def _humanize_age(hours: float) -> str:
    """7 → '7h', 26 → '26h', 168 → '7d', 200 → '8d'."""
    if hours < 24:
        return f"{int(hours)}h"
    return f"{int(hours / 24)}d"


def prompt_if_stale(
    report: StalenessReport,
    *,
    stdin=None,
    stdout=None,
    print_report: bool = True,
) -> bool:
    """Print the report and, if any prereq is stale/never, prompt continue/abort.

    Returns True (proceed) or False (operator chose abort).

    TTY rule: on a non-interactive stdin (cron, pipe), skip the prompt and
    auto-continue with a stderr warning so automated runs aren't blocked.

    `print_report=False` skips the initial print (useful when the caller
    already printed a richer multi-report layout and just wants the prompt
    half of this function's behavior). `stdin` / `stdout` are injectable
    for tests; default to sys.stdin / sys.stdout.
    """
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout

    if print_report:
        text = format_report(report)
        print(text, file=stdout)

    if not report.has_warnings:
        return True

    # Non-TTY → auto-continue but make it loud so the cron log surfaces it.
    is_tty = getattr(stdin, "isatty", lambda: False)()
    if not is_tty:
        print(
            f"[non-TTY] Auto-continuing despite stale prereqs for `{report.workflow}`. "
            "Re-run upstreams to silence this.",
            file=sys.stderr,
        )
        return True

    print("", file=stdout)
    print(
        "Some prerequisites are stale or have never run for this operator. "
        "Continuing now will produce lower-quality output (e.g. missing Gmail "
        "context, missing LinkedIn DMs). Re-running the upstreams first is "
        "recommended.",
        file=stdout,
    )
    while True:
        print("", file=stdout)
        print("Continue anyway? [y/N]: ", end="", file=stdout, flush=True)
        try:
            answer = stdin.readline().strip().lower()
        except (KeyboardInterrupt, EOFError):
            print("\nAborted.", file=stdout)
            return False
        if answer in ("y", "yes"):
            return True
        if answer in ("", "n", "no"):
            print("Aborted.", file=stdout)
            return False
        print(f"Didn't recognize {answer!r} — type y or n.", file=stdout)


def run_prereq_gate(workflow: str, operator: str = "") -> bool:
    """One-shot helper: build report, print it, prompt if stale, return decision.

    The intended call site for management commands: at the top of
    `_handle_impl`, `if not run_prereq_gate("backfill-messages", operator): return`.
    """
    report = check_prereqs(workflow, operator=operator)
    return prompt_if_stale(report)
