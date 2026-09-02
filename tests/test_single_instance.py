from pathlib import Path

from linkedin.single_instance import matches_top_level_manage_daemon


class FakeProcess:
    def __init__(self, cmdline, cwd):
        self._cmdline = cmdline
        self._cwd = cwd

    def cmdline(self):
        return self._cmdline

    def cwd(self):
        return str(self._cwd)


def test_matches_argument_free_manage_daemon(tmp_path):
    proc = FakeProcess(["python", str(tmp_path / "manage.py")], tmp_path)

    assert matches_top_level_manage_daemon(proc, root_dir=tmp_path)


def test_does_not_match_gmail_management_command(tmp_path):
    proc = FakeProcess(
        [
            "python",
            str(tmp_path / "manage.py"),
            "run_gmail_worker",
            "--account",
            "eddy_boundera",
        ],
        tmp_path,
    )

    assert not matches_top_level_manage_daemon(proc, root_dir=tmp_path)


def test_does_not_match_manage_process_from_another_repo(tmp_path):
    other = tmp_path / "other"
    proc = FakeProcess(["python", str(other / "manage.py")], other)

    assert not matches_top_level_manage_daemon(proc, root_dir=tmp_path)
