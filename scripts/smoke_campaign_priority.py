r"""Portable, no-send campaign smoke test. Never loads the working .env or PostgreSQL.

Windows: .venv\Scripts\python.exe -I scripts\smoke_campaign_priority.py
Optional: --integration-dir C:\review\rb2b-integration
This is a simulation, not permission or evidence to deploy to production.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
CORE_TESTS = (
    "tests/test_campaign_smoke.py",
    "tests/test_campaign_priority.py", "tests/test_campaign_integrations.py",
    "tests/test_campaign_hours.py", "tests/test_schedule.py",
    "tests/test_browser_recovery.py",
    "tests/test_retired_connection_tasks.py", "tests/test_legacy_rb2b.py", "tests/test_rb2b_webhook.py", "tests/tasks/test_claim_filter.py",
    "tests/test_action_log.py", "tests/tasks/test_message_program_runtime.py",
    "tests/test_daemon_resilience.py",
    "tests/tasks/test_follow_up_schedule.py", "tests/tasks/test_stop_policy.py",
    "tests/realtime/test_handler.py", "tests/realtime/test_lead_lookup.py",
)


class SmokeSafetyError(RuntimeError):
    pass


def clean_environment():
    # Windows needs SystemRoot for DLL loading and its standard temp directory.
    # Do not inherit tokens, DATABASE_URL, Python plugins or sender configuration.
    keep = {"PATH", "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "COMSPEC", "PATHEXT",
            "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL", "TZ"}
    return {key: value for key, value in os.environ.items() if key.upper() in keep} | {
        "PYTHON_DOTENV_DISABLED": "1", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8",
    }


def child(suite, target, report):
    environment = clean_environment()
    os.environ.clear()
    os.environ.update(environment)
    original_cwd = Path.cwd()
    with tempfile.TemporaryDirectory(prefix="campaign-smoke-") as temp:
        scratch = Path(temp).resolve()
        # Windows libraries may require APPDATA; never point them at real data.
        appdata = scratch / "appdata"
        appdata.mkdir()
        os.environ["APPDATA"] = str(appdata)
        os.chdir(scratch)

        def audit(event, args):
            if event in {"subprocess.Popen", "os.system", "os.posix_spawn", "socket.connect",
                         "socket.getaddrinfo", "socket.bind", "socket.sendto"}:
                raise SmokeSafetyError("Blocked external I/O: " + event)
            if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
                original = Path(os.fsdecode(args[0])).absolute()
                path = original.resolve()
                if ((original.name.lower().startswith(".env") or path.name.lower().startswith(".env"))
                        and path not in {ROOT / ".env.example", target / ".env.example"}
                        and not path.is_relative_to(scratch)):
                    raise SmokeSafetyError("Blocked environment file")
                if any(path.is_relative_to((base / name).resolve()) for base in (ROOT, target) for name in ("data", "secrets")):
                    raise SmokeSafetyError("Blocked private runtime data")

        sys.addaudithook(audit)
        result = {"suite": suite, "mode": "simulation", "postgresql_validated": False,
                  "live_sends_permitted": False, "production_database_permitted": False,
                  "tests": [], "counts": {}, "guard_checks": []}

        def forbidden_database(*args, **kwargs):
            raise SmokeSafetyError("PostgreSQL is forbidden in the smoke simulation")

        class Report:
            def pytest_runtest_logreport(self, report):
                item = report
                if item.when == "call" or item.failed or item.skipped:
                    result["tests"].append({"test": item.nodeid, "phase": item.when, "outcome": item.outcome,
                                            "properties": dict(item.user_properties)})
                    counts = result["counts"]
                    counts[item.outcome] = counts.get(item.outcome, 0) + 1

        try:
            import dotenv
            dotenv.load_dotenv = lambda *a, **kw: False
            import psycopg
            psycopg.connect = forbidden_database
            psycopg.Connection.connect = forbidden_database
            psycopg.AsyncConnection.connect = forbidden_database
            # Exercise the guards before importing any application code. These
            # operations must raise before they can read a file or reach a host.
            import socket
            for name, probe in (
                ("environment_read", lambda: (ROOT / ".env").read_bytes()),
                ("network", lambda: socket.getaddrinfo("example.invalid", 443)),
                ("postgresql", lambda: psycopg.connect("synthetic-probe")),
                ("subprocess", lambda: sys.audit("subprocess.Popen", "blocked", [], None, None)),
            ):
                try:
                    probe()
                except SmokeSafetyError:
                    result["guard_checks"].append(name)
                else:
                    raise SmokeSafetyError("A required guard did not block its probe")
            import pytest
            sys.path.insert(0, str(target))
            args = ["-q", "-rs", "--tb=short", "-p", "no:cacheprovider", "--basetemp", str(scratch / "pytest")]
            if suite == "core":
                os.environ["DJANGO_SETTINGS_MODULE"] = "linkedin.django_settings"
                import django
                django.setup()
                from django.conf import settings
                if (settings.DATABASES["default"]["ENGINE"] != "django.db.backends.sqlite3"
                        or settings.DATABASES["default"]["NAME"] != ":memory:"):
                    raise SmokeSafetyError("An in-memory test database is required")
                from django.core.management import call_command
                call_command("makemigrations", check=True, dry_run=True, interactive=False)
                args += ["-c", str(ROOT / "pytest.ini"), "-p", "pytest_django.plugin", "-p", "pytest_mock"]
                args += [str(ROOT / path) for path in CORE_TESTS]
            else:
                # External integration has its own tests package; use another
                # process and empty config so Django's pytest.ini cannot leak in.
                config = scratch / "pytest.ini"
                config.write_text("[pytest]\n", encoding="utf-8")
                args += ["-c", str(config), str(target / "tests")]
            result["exit_code"] = int(pytest.main(args, plugins=[Report()]))
        except Exception as exc:
            result["exit_code"] = 2
            result["failure_type"] = type(exc).__name__
            print("Smoke setup failed:", type(exc).__name__, file=sys.stderr)
        finally:
            os.chdir(original_cwd)
            report.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        return result["exit_code"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--integration-dir", type=Path, help="Optional separate integration checkout; never loads its .env.")
    parser.add_argument("--output-dir", type=Path, help="New output directory; refuses to overwrite an existing directory.")
    parser.add_argument("--child", choices=("core", "integration"), help=argparse.SUPPRESS)
    parser.add_argument("--target", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--report", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        if not args.target or not args.report:
            parser.error("Child target and report are required")
        return child(args.child, args.target.resolve(), args.report.resolve())
    integration = args.integration_dir.resolve() if args.integration_dir else None
    if integration and not (integration / "rb2b_integration" / "review.py").is_file():
        parser.error("--integration-dir must identify the separate integration checkout")
    output = (args.output_dir or ROOT / "artifacts/qa/campaign-smoke" /
              datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")).resolve()
    output.mkdir(parents=True, exist_ok=False)
    suites = [("core", ROOT)] + ([("integration", integration)] if integration else [])
    summary = {"mode": "simulation", "platform": sys.platform, "python": sys.version.split()[0],
               "suites": [], "rollout_authorized": False, "postgresql_validated": False}
    for suite, target in suites:
        report = output / (suite + ".json")
        with (output / (suite + ".log")).open("w", encoding="utf-8") as log:
            try:
                completed = subprocess.run([sys.executable, "-I", "-B", str(Path(__file__).resolve()),
                    "--child", suite, "--target", str(target), "--report", str(report)],
                    cwd=output, env=clean_environment(), stdout=log, stderr=subprocess.STDOUT, timeout=180)
                code = completed.returncode
            except subprocess.TimeoutExpired:
                code = 124
        details = json.loads(report.read_text(encoding="utf-8")) if report.exists() else {}
        summary["suites"].append({"suite": suite, "exit_code": code, "counts": details.get("counts", {}),
                                  "guard_checks": details.get("guard_checks", []), "report": str(report)})
    summary["passed"] = all(item["exit_code"] == 0 and item["counts"].get("passed", 0) > 0
                            and len(item["guard_checks"]) == 4 for item in summary["suites"])
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
