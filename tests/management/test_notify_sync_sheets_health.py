from datetime import datetime
from pathlib import Path

from linkedin.management.commands import notify_sync_sheets_health as health


def _successful_log(run_id: str = "abc123") -> str:
    return "\n".join(
        (
            f"[2026-08-26T09:00:00-04:00] starting crm_v2_workflow run_id={run_id}",
            f"[2026-08-26T09:00:01-04:00] finished sync_crm_v2_context run_id={run_id} exit_code=0",
            f"[2026-08-26T09:00:02-04:00] finished refresh_crm_v2 run_id={run_id} exit_code=0",
            f"[2026-08-26T09:00:03-04:00] finished crm_v2_workflow run_id={run_id} exit_code=0",
        )
    )


def test_default_log_path_is_v2_specific():
    assert health.DEFAULT_LOG_PATH == Path("data/logs/crm_v2_task.log")


def test_latest_run_lines_ignore_an_older_failure():
    lines = [
        "starting crm_v2_workflow run_id=abc111",
        "crm_v2_workflow failed run_id=abc111: context failed",
        *_successful_log("def456").splitlines(),
    ]

    latest = health._latest_run_lines(lines)

    assert latest[0].endswith("starting crm_v2_workflow run_id=def456")
    assert health._latest_exit_code(latest) == 0
    assert health._latest_phase_exit_codes(latest) == {
        "context": 0,
        "refresh": 0,
    }


def test_evaluate_health_requires_both_v2_phases(monkeypatch, tmp_path):
    log_path = tmp_path / "crm_v2_task.log"
    log_path.write_text(
        "\n".join(
            (
                "starting crm_v2_workflow run_id=abc123",
                "finished refresh_crm_v2 run_id=abc123 exit_code=0",
                "finished crm_v2_workflow run_id=abc123 exit_code=0",
            )
        ),
        encoding="utf-8",
    )
    now = datetime.now(health.LOCAL_TZ)
    monkeypatch.setattr(
        health,
        "_scheduled_task",
        lambda _name: {"State": "Ready", "Enabled": True},
    )
    monkeypatch.setattr(
        health,
        "_scheduled_task_info",
        lambda _name: {"LastRunTime": now.isoformat(), "LastTaskResult": 0},
    )

    result = health.evaluate_health(task_name="task", log_path=log_path)

    assert result.status == "failed"
    assert "both required phases" in result.reason


def test_evaluate_health_detects_failure_even_if_start_rolled_out_of_tail(
    monkeypatch,
    tmp_path,
):
    log_path = tmp_path / "crm_v2_task.log"
    log_path.write_text(
        "crm_v2_workflow failed run_id=abc123: context failed\n",
        encoding="utf-8",
    )
    now = datetime.now(health.LOCAL_TZ)
    monkeypatch.setattr(
        health,
        "_scheduled_task",
        lambda _name: {"State": "Ready", "Enabled": True},
    )
    monkeypatch.setattr(
        health,
        "_scheduled_task_info",
        lambda _name: {"LastRunTime": now.isoformat(), "LastTaskResult": 1},
    )

    result = health.evaluate_health(task_name="task", log_path=log_path)

    assert result.status == "failed"
    assert "workflow failure" in result.reason


def test_evaluate_health_accepts_one_complete_v2_run(monkeypatch, tmp_path):
    log_path = tmp_path / "crm_v2_task.log"
    log_path.write_text(_successful_log(), encoding="utf-8")
    now = datetime.now(health.LOCAL_TZ)
    monkeypatch.setattr(
        health,
        "_scheduled_task",
        lambda _name: {"State": "Ready", "Enabled": True},
    )
    monkeypatch.setattr(
        health,
        "_scheduled_task_info",
        lambda _name: {"LastRunTime": now.isoformat(), "LastTaskResult": 0},
    )

    result = health.evaluate_health(task_name="task", log_path=log_path)

    assert result.status == "healthy"
    assert result.latest_exit_code == 0


def test_scheduled_wrapper_runs_context_then_routine_v2_refresh():
    root = Path(__file__).resolve().parents[2]
    wrapper = (root / "scripts" / "run_sync_sheets.ps1").read_text(encoding="utf-8")

    context = "manage.py sync_crm_v2_context --apply"
    refresh = "manage.py refresh_crm_v2 --apply --routine"
    assert wrapper.index(context) < wrapper.index(refresh)
    assert "--manual-pin StackArmor" in wrapper
    assert "--owner-override Ramp=Arian" in wrapper
    assert "--owner-override StackArmor=Arian" in wrapper
    assert '"crm_v2_task.log"' in wrapper
    assert "manage.py refresh_crm --apply" not in wrapper
