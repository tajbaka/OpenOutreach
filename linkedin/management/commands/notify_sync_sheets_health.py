"""Post a daily Slack health rollup for the Windows sync_sheets task."""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from django.core.management.base import BaseCommand


TASK_NAME = "OpenOutreach Sync Sheets"
DEFAULT_LOG_PATH = Path("data") / "logs" / "sync_sheets_task.log"
LOCAL_TZ = ZoneInfo("America/Toronto")
FINISHED_RE = re.compile(r"finished sync_sheets exit_code=(?P<code>-?\d+)")
FAILED_RE = re.compile(r"sync_sheets failed:", re.IGNORECASE)


@dataclass(frozen=True)
class HealthResult:
    status: str
    reason: str
    task: dict
    task_info: dict
    last_log_lines: list[str]
    latest_exit_code: int | None


class Command(BaseCommand):
    help = "Post a daily Slack health summary for the Windows sync_sheets Scheduled Task."

    def add_arguments(self, parser):
        parser.add_argument("--task-name", default=TASK_NAME)
        parser.add_argument("--log-path", default=str(DEFAULT_LOG_PATH))
        parser.add_argument("--no-slack", action="store_true", help="Print only; do not post to Slack.")

    def handle(self, *args, **options):
        result = evaluate_health(
            task_name=options["task_name"],
            log_path=Path(options["log_path"]),
        )
        message = render_text(result)
        self.stdout.write(message)
        if options["no_slack"]:
            return

        from linkedin.conf import SLACK_WEBHOOK_URL
        from linkedin.notifications.slack import _post_to_slack

        emoji = {
            "healthy": ":white_check_mark:",
            "warning": ":warning:",
            "failed": ":rotating_light:",
        }.get(result.status, ":grey_question:")
        payload = {
            "text": f"{emoji} OpenOutreach sync_sheets daily health: {result.status}",
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"{emoji} *OpenOutreach sync_sheets daily health: {result.status.upper()}*",
                    },
                },
                {"type": "section", "text": {"type": "mrkdwn", "text": slack_escape(message)}},
            ],
        }
        _post_to_slack(SLACK_WEBHOOK_URL, payload, "sync-sheets-health")


def evaluate_health(*, task_name: str, log_path: Path) -> HealthResult:
    task = _scheduled_task(task_name)
    task_info = _scheduled_task_info(task_name) if task else {}
    last_log_lines = _tail_log(log_path, max_lines=16)
    latest_exit_code = _latest_exit_code(last_log_lines)

    status = "healthy"
    reasons: list[str] = []
    now = datetime.now(LOCAL_TZ)
    expected_first_run = datetime.combine(now.date(), time(hour=9), tzinfo=LOCAL_TZ)

    if not task:
        status = "failed"
        reasons.append(f"Scheduled Task {task_name!r} was not found.")
    else:
        state = str(task.get("State") or "")
        if state not in {"Ready", "Running"}:
            status = "failed"
            reasons.append(f"Task state is {state or 'unknown'}.")
        if not task.get("Enabled", True):
            status = "failed"
            reasons.append("Task is disabled.")

    last_run = _parse_dt(task_info.get("LastRunTime"))
    last_result = _int_or_none(task_info.get("LastTaskResult"))
    if now >= expected_first_run:
        if last_run is None or last_run < expected_first_run:
            status = _worse(status, "warning")
            reasons.append("Task has not recorded a run since today's sync window opened.")
        elif last_result not in (0, None):
            status = _worse(status, "failed")
            reasons.append(f"Task Scheduler last result is {last_result}.")

    if not last_log_lines:
        status = _worse(status, "warning")
        reasons.append(f"Log file is missing or empty: {log_path}")
    elif any(FAILED_RE.search(line) for line in last_log_lines[-8:]):
        status = _worse(status, "failed")
        reasons.append("Recent log lines include a sync_sheets failure.")
    elif latest_exit_code is None:
        status = _worse(status, "warning")
        reasons.append("Could not find a recent finished sync_sheets exit code in the log.")
    elif latest_exit_code != 0:
        status = _worse(status, "failed")
        reasons.append(f"Latest logged sync_sheets exit code is {latest_exit_code}.")

    if not reasons:
        reasons.append("Task is present and latest logged sync_sheets run finished successfully.")

    return HealthResult(
        status=status,
        reason=" ".join(reasons),
        task=task,
        task_info=task_info,
        last_log_lines=last_log_lines,
        latest_exit_code=latest_exit_code,
    )


def render_text(result: HealthResult) -> str:
    task = result.task
    info = result.task_info
    lines = [
        f"Status: {result.status}",
        f"Reason: {result.reason}",
        f"Task state: {task.get('State', 'missing') if task else 'missing'}",
        f"Last run: {info.get('LastRunTime', 'unknown')}",
        f"Last task result: {info.get('LastTaskResult', 'unknown')}",
        f"Next run: {info.get('NextRunTime', 'unknown')}",
        f"Latest logged exit code: {result.latest_exit_code if result.latest_exit_code is not None else 'unknown'}",
    ]
    if result.last_log_lines:
        lines.append("Recent log:")
        lines.extend(result.last_log_lines[-8:])
    return "\n".join(lines)


def _scheduled_task(task_name: str) -> dict:
    script = (
        f"$task = Get-ScheduledTask -TaskName {json.dumps(task_name)} -ErrorAction SilentlyContinue; "
        "if ($task) { [pscustomobject]@{ TaskName=$task.TaskName; State=$task.State.ToString(); "
        "Enabled=$task.Settings.Enabled } | ConvertTo-Json -Compress }"
    )
    return _powershell_json(script)


def _scheduled_task_info(task_name: str) -> dict:
    script = (
        f"$info = Get-ScheduledTaskInfo -TaskName {json.dumps(task_name)} -ErrorAction SilentlyContinue; "
        "if ($info) { [pscustomobject]@{ "
        "LastRunTime=$info.LastRunTime.ToString('o'); "
        "LastTaskResult=$info.LastTaskResult; "
        "NextRunTime=$info.NextRunTime.ToString('o'); "
        "NumberOfMissedRuns=$info.NumberOfMissedRuns } | ConvertTo-Json -Compress }"
    )
    return _powershell_json(script)


def _powershell_json(script: str) -> dict:
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    output = (completed.stdout or "").strip()
    if completed.returncode != 0 or not output:
        return {}
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _tail_log(path: Path, *, max_lines: int) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8", errors="replace").splitlines()[-max_lines:]


def _latest_exit_code(lines: list[str]) -> int | None:
    for line in reversed(lines):
        match = FINISHED_RE.search(line)
        if match:
            return int(match.group("code"))
    return None


def _parse_dt(value: object) -> datetime | None:
    if not value:
        return None
    text = str(value)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.year < 2000:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=LOCAL_TZ)
    return parsed.astimezone(LOCAL_TZ)


def _int_or_none(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _worse(current: str, candidate: str) -> str:
    rank = {"healthy": 0, "warning": 1, "failed": 2}
    return candidate if rank[candidate] > rank[current] else current


def slack_escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
