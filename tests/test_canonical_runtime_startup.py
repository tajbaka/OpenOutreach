from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]


def test_make_normal_runtime_targets_use_supervisor():
    makefile = (ROOT_DIR / "Makefile").read_text()

    assert "run: ## run the full LinkedIn + Gmail runtime under the supervisor\n\t.venv/bin/python daemon_supervisor.py" in makefile
    assert "caffeinate -ims .venv/bin/python daemon_supervisor.py" in makefile
    assert "run-linkedin: ## run only the LinkedIn daemon" in makefile
    assert "\n\t.venv/bin/python manage.py\n" in makefile


def test_docker_start_execs_supervisor_without_git_updates():
    start_script = (ROOT_DIR / "compose" / "linkedin" / "start").read_text()

    assert "exec python daemon_supervisor.py --no-update" in start_script
    assert "\npython manage.py\n" not in start_script


def test_windows_awake_runner_uses_supervisor():
    awake_script = (ROOT_DIR / "run-openoutreach-awake.ps1").read_text()

    assert "& $python daemon_supervisor.py" in awake_script
    assert "& $python manage.py" not in awake_script
