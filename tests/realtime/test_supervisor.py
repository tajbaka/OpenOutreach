"""Tests for the realtime listener supervisor."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from linkedin.realtime.supervisor import ListenerSupervisor


def _fake_proc(alive=True):
    proc = MagicMock()
    proc.poll.return_value = None if alive else 1
    return proc


def test_ensure_running_spawns_when_no_process():
    sup = ListenerSupervisor()
    with patch("linkedin.realtime.supervisor.subprocess.Popen", return_value=_fake_proc()) as popen:
        sup.ensure_running()
    popen.assert_called_once()


def test_ensure_running_is_noop_when_process_alive():
    sup = ListenerSupervisor()
    with patch("linkedin.realtime.supervisor.subprocess.Popen", return_value=_fake_proc(alive=True)) as popen:
        sup.ensure_running()
        sup.ensure_running()
    popen.assert_called_once()


def test_ensure_running_respawns_after_death():
    sup = ListenerSupervisor()
    dead, alive = _fake_proc(alive=False), _fake_proc(alive=True)
    with patch("linkedin.realtime.supervisor.subprocess.Popen", side_effect=[dead, alive]) as popen:
        sup.ensure_running()
        sup.ensure_running()
    assert popen.call_count == 2


def test_ensure_running_gives_up_after_max_failures():
    sup = ListenerSupervisor()
    with patch("linkedin.realtime.supervisor.subprocess.Popen",
               side_effect=OSError("cannot spawn")) as popen:
        for _ in range(ListenerSupervisor.MAX_SPAWN_FAILURES + 5):
            sup.ensure_running()
    assert popen.call_count == ListenerSupervisor.MAX_SPAWN_FAILURES


def test_stop_terminates_a_running_process():
    sup = ListenerSupervisor()
    proc = _fake_proc(alive=True)
    with patch("linkedin.realtime.supervisor.subprocess.Popen", return_value=proc):
        sup.ensure_running()
    sup.stop()
    proc.terminate.assert_called_once()


def test_stop_is_noop_when_nothing_running():
    sup = ListenerSupervisor()
    sup.stop()  # must not raise


def test_stop_resets_failure_count_so_ensure_can_spawn_again():
    sup = ListenerSupervisor()
    with patch("linkedin.realtime.supervisor.subprocess.Popen",
               side_effect=OSError("boom")):
        for _ in range(ListenerSupervisor.MAX_SPAWN_FAILURES + 2):
            sup.ensure_running()
    sup.stop()
    with patch("linkedin.realtime.supervisor.subprocess.Popen", return_value=_fake_proc()) as popen:
        sup.ensure_running()
    popen.assert_called_once()
