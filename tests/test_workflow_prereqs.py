"""Tests for the workflow prerequisite staleness check.

Covers `check_prereqs`, `format_report`, and `prompt_if_stale`. The dependency
graph itself (`WORKFLOW_PREREQS`) is exercised through the public API rather
than directly, so a graph change automatically widens coverage of the
workflows that consume it.
"""
from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone

import pytest

from linkedin import workflow_prereqs as wp
from linkedin.models import WorkflowRun


@pytest.fixture
def now_utc():
    return datetime.now(timezone.utc)


def _record_run(name: str, operator: str, completed_at: datetime) -> WorkflowRun:
    """Test helper — bypass auto_now_add by writing then patching the row."""
    wr = WorkflowRun.objects.create(name=name, operator=operator, summary="", counts={})
    WorkflowRun.objects.filter(pk=wr.pk).update(completed_at=completed_at)
    wr.refresh_from_db()
    return wr


# ------------------------------------------------------------------
# check_prereqs
# ------------------------------------------------------------------


def test_check_prereqs_no_upstream_returns_empty_rows(db):
    """import-connections has no upstream prereqs — report should be empty."""
    report = wp.check_prereqs("import-connections", operator="Arian")
    assert report.workflow == "import-connections"
    assert report.operator == "Arian"
    assert report.rows == []
    assert not report.has_warnings


def test_check_prereqs_unknown_workflow_returns_empty_rows(db):
    """An undeclared workflow is treated as having no prereqs (graceful)."""
    report = wp.check_prereqs("nonsense", operator="Arian")
    assert report.rows == []


def test_check_prereqs_marks_never_when_no_workflow_run_row(db):
    """backfill-messages depends on import-connections — never run = warn."""
    report = wp.check_prereqs("backfill-messages", operator="Arian")
    assert len(report.rows) == 1
    row = report.rows[0]
    assert row.workflow == "import-connections"
    assert row.operator == "Arian"
    assert row.status == "never"
    assert row.completed_at is None
    assert row.is_warning


def test_check_prereqs_marks_fresh_when_within_window(db, now_utc):
    """A row 1 day old should be 'fresh' (window is 7 days)."""
    _record_run("import-connections", "Arian", now_utc - timedelta(days=1))
    report = wp.check_prereqs("backfill-messages", operator="Arian")
    row = report.rows[0]
    assert row.status == "fresh"
    assert not row.is_warning
    assert 23 < row.age_hours < 25


def test_check_prereqs_marks_stale_when_past_window(db, now_utc):
    """A row 9 days old should be 'stale'."""
    _record_run("import-connections", "Arian", now_utc - timedelta(days=9))
    report = wp.check_prereqs("backfill-messages", operator="Arian")
    row = report.rows[0]
    assert row.status == "stale"
    assert row.is_warning


def test_check_prereqs_per_operator_scoping(db, now_utc):
    """Arian's row should not satisfy Chuka's prereq for the same workflow."""
    _record_run("import-connections", "Arian", now_utc - timedelta(hours=2))
    # Arian: fresh
    arian = wp.check_prereqs("backfill-messages", operator="Arian")
    assert arian.rows[0].status == "fresh"
    # Chuka: never (Arian's run doesn't count for Chuka)
    chuka = wp.check_prereqs("backfill-messages", operator="Chuka")
    assert chuka.rows[0].status == "never"


def test_check_prereqs_followup_pulls_per_operator_rows(db, now_utc):
    """followup → data-sync / backfill / import-connections (all per-operator).

    When called with operator="Arian", only Arian-tagged WorkflowRun rows
    should satisfy the prereqs.
    """
    _record_run("data-sync", "Arian", now_utc - timedelta(hours=3))
    _record_run("backfill-messages", "Arian", now_utc - timedelta(hours=3))
    # No import-connections row for Arian → that one should be 'never'.
    _record_run("import-connections", "Chuka", now_utc - timedelta(hours=3))

    report = wp.check_prereqs("followup", operator="Arian")
    by_name = {r.workflow: r for r in report.rows}
    assert by_name["data-sync"].status == "fresh"
    assert by_name["backfill-messages"].status == "fresh"
    assert by_name["import-connections"].status == "never"
    assert report.has_warnings


def test_check_prereqs_warns_on_empty_operator_for_per_op_prereq(db, caplog):
    """Programmer error guard — calling per-op check with operator="" should
    log a warning and treat prereqs as never-run rather than silently
    matching every row in the DB."""
    report = wp.check_prereqs("backfill-messages", operator="")
    assert report.rows[0].status == "never"
    assert any("empty operator" in rec.message for rec in caplog.records)


# ------------------------------------------------------------------
# format_report
# ------------------------------------------------------------------


def test_format_report_no_upstream_message(db):
    report = wp.check_prereqs("import-connections", operator="Arian")
    text = wp.format_report(report)
    assert "no upstream prereqs" in text


def test_format_report_renders_fresh_and_stale_rows(db, now_utc):
    _record_run("import-connections", "Arian", now_utc - timedelta(hours=2))
    _record_run("data-sync", "Arian", now_utc - timedelta(days=9))
    report = wp.check_prereqs("followup", operator="Arian")
    text = wp.format_report(report)
    assert "operator: Arian" in text
    assert "✓" in text  # fresh row marker
    assert "⚠" in text  # stale/never row marker
    assert "STALE" in text  # rendered status label
    assert "NEVER run" in text  # backfill-messages was never recorded


# ------------------------------------------------------------------
# prompt_if_stale — TTY / non-TTY behavior
# ------------------------------------------------------------------


class _FakeStdin:
    def __init__(self, response: str, is_tty: bool = True):
        self._buf = io.StringIO(response)
        self._is_tty = is_tty

    def isatty(self) -> bool:
        return self._is_tty

    def readline(self) -> str:
        return self._buf.readline()


def test_prompt_if_stale_returns_true_when_no_warnings(db, now_utc):
    """Fresh prereqs → no prompt, just print and return True."""
    _record_run("import-connections", "Arian", now_utc - timedelta(hours=2))
    report = wp.check_prereqs("backfill-messages", operator="Arian")
    stdin = _FakeStdin("", is_tty=True)
    stdout = io.StringIO()
    assert wp.prompt_if_stale(report, stdin=stdin, stdout=stdout) is True
    # No prompt line should have been emitted.
    assert "Continue anyway" not in stdout.getvalue()


def test_prompt_if_stale_continue_on_y(db):
    """Operator types 'y' → returns True."""
    report = wp.check_prereqs("backfill-messages", operator="Arian")  # never-run
    stdin = _FakeStdin("y\n", is_tty=True)
    stdout = io.StringIO()
    assert wp.prompt_if_stale(report, stdin=stdin, stdout=stdout) is True
    assert "Continue anyway" in stdout.getvalue()


def test_prompt_if_stale_abort_on_n(db):
    """Operator types 'n' → returns False."""
    report = wp.check_prereqs("backfill-messages", operator="Arian")
    stdin = _FakeStdin("n\n", is_tty=True)
    stdout = io.StringIO()
    assert wp.prompt_if_stale(report, stdin=stdin, stdout=stdout) is False
    assert "Aborted" in stdout.getvalue()


def test_prompt_if_stale_abort_on_blank(db):
    """Blank answer → default to abort (capital N in the prompt)."""
    report = wp.check_prereqs("backfill-messages", operator="Arian")
    stdin = _FakeStdin("\n", is_tty=True)
    stdout = io.StringIO()
    assert wp.prompt_if_stale(report, stdin=stdin, stdout=stdout) is False


def test_prompt_if_stale_reprompts_on_garbage_input(db):
    """Operator types nonsense, then 'y'."""
    report = wp.check_prereqs("backfill-messages", operator="Arian")
    stdin = _FakeStdin("maybe?\nyes\n", is_tty=True)
    stdout = io.StringIO()
    assert wp.prompt_if_stale(report, stdin=stdin, stdout=stdout) is True
    assert "Didn't recognize" in stdout.getvalue()


def test_prompt_if_stale_auto_continues_on_non_tty(db, capsys):
    """Cron / pipe → auto-continue with stderr warning, no readline call."""
    report = wp.check_prereqs("backfill-messages", operator="Arian")
    stdin = _FakeStdin("", is_tty=False)
    stdout = io.StringIO()
    assert wp.prompt_if_stale(report, stdin=stdin, stdout=stdout) is True
    err = capsys.readouterr().err
    assert "non-TTY" in err
    assert "Auto-continuing" in err


def test_prompt_if_stale_skip_print_when_requested(db):
    """print_report=False suppresses the initial report print but still
    prompts on warnings."""
    report = wp.check_prereqs("backfill-messages", operator="Arian")
    stdin = _FakeStdin("n\n", is_tty=True)
    stdout = io.StringIO()
    wp.prompt_if_stale(report, stdin=stdin, stdout=stdout, print_report=False)
    out = stdout.getvalue()
    # Report header shouldn't be in output, but the prompt should.
    assert "Prerequisite check" not in out
    assert "Continue anyway" in out
