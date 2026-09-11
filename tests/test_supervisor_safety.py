from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import psutil
import pytest

import supervisor_safety as safety_module
from linkedin.exceptions import SupervisorShutdownError, SupervisorStopped
from supervisor_safety import SupervisorSafety


@pytest.fixture
def world(monkeypatch):
    events = []
    processes = {}
    launches = []
    monitors = []

    class Process:
        def __init__(self, pid, *, stubborn=False, unkillable=False):
            self.pid = pid
            self.created = float(pid)
            self.alive = True
            self.reused = False
            self.stubborn = stubborn
            self.unkillable = unkillable
            self.descendants = []
            processes[pid] = self

        def create_time(self):
            return self.created

        def is_running(self):
            return self.alive and not self.reused

        def children(self, *, recursive):
            assert recursive is True
            events.append(("children", self.pid))
            return list(self.descendants)

        def terminate(self):
            events.append(("terminate", self.pid))
            if not self.stubborn:
                self.alive = False

        def kill(self):
            events.append(("kill", self.pid))
            if not self.unkillable:
                self.alive = False

    class Popen:
        def __init__(self, args, **kwargs):
            self.pid = 100 + len(launches)
            self.process = Process(self.pid)
            self.args = args
            self.kwargs = kwargs
            launches.append(self)
            events.append(("launch", self.pid, args))

        def poll(self):
            return None if self.process.alive else 0

        def terminate(self):
            self.process.terminate()

        def kill(self):
            self.process.kill()

        def wait(self, *, timeout):
            assert not self.process.alive
            return 0

    def wait_procs(procs, *, timeout):
        events.append(("wait", tuple(proc.pid for proc in procs), timeout))
        return [proc for proc in procs if not proc.is_running()], [proc for proc in procs if proc.is_running()]

    def alert(reason):
        events.append(("alert", reason))

    def monitor(check=lambda: False, on_stop=alert, interval=.005):
        result = SupervisorSafety(check, on_stop, interval_seconds=interval)
        monitors.append(result)
        return result

    monkeypatch.setattr(safety_module.subprocess, "Popen", Popen)
    monkeypatch.setattr(safety_module.psutil, "Process", lambda pid: processes[pid])
    monkeypatch.setattr(safety_module.psutil, "wait_procs", wait_procs)
    yield SimpleNamespace(
        events=events, processes=processes, launches=launches,
        Process=Process, Popen=Popen, monitor=monitor,
    )
    for monitor_instance in monitors:
        monitor_instance.close()


@pytest.mark.parametrize("result,permitted,stopped", [(False, True, False), (True, False, True), (None, False, False)])
def test_preflight_requires_known_clear_control(world, result, permitted, stopped):
    safety = world.monitor(lambda: result)

    assert safety.preflight() is permitted
    assert safety.stopped.is_set() is stopped
    assert world.launches == []
    assert safety.emergency is stopped
    assert [event for event in world.events if event[0] == "alert"] == (
        [("alert", "sender_stop")] if stopped else []
    )


def test_stop_captures_tree_before_termination_and_leaves_unrelated_processes(world):
    safety = world.monitor()
    root = safety.launch(["manage.py"], cwd="checkout", env={"example": "value"})
    child = world.Process(200)
    grandchild = world.Process(201)
    root.process.descendants = [child, grandchild]
    unrelated = world.Process(900)

    safety.request_stop()

    assert root.kwargs == {"cwd": "checkout", "env": {"example": "value"}}
    assert not any(proc.alive for proc in (root.process, child, grandchild))
    assert unrelated.alive
    termination = next(i for i, event in enumerate(world.events) if event[0] == "terminate")
    assert ("children", root.pid) in world.events[:termination]
    assert world.events[-1] == ("alert", "sender_stop")
    assert {event[1] for event in world.events if event[0] == "terminate"} == {100, 200, 201}


def test_known_descendant_remains_owned_after_root_exits(world):
    safety = world.monitor()
    root = safety.launch(["manage.py"])
    child = world.Process(200)
    root.process.descendants = [child]
    # Another launch observes descendants while their first parent is alive.
    safety.launch(["git", "fetch"])
    root.process.alive = False
    root.process.descendants = []

    safety.request_stop()

    assert not child.alive
    assert (100, 100.0) not in safety._owned
    assert (200, 200.0) in safety._owned


def test_pid_reuse_does_not_signal_new_process(world):
    safety = world.monitor()
    root = safety.launch(["git", "fetch"])
    root.process.reused = True

    safety.request_stop()

    assert not [event for event in world.events if event[0] in {"terminate", "kill"}]


def test_termination_escalates_with_bounded_waits_before_alert(world):
    safety = world.monitor()
    root = safety.launch(["git", "fetch"])
    root.process.stubborn = True

    safety.request_stop()

    assert [event for event in world.events if event[0] in {"terminate", "kill", "wait", "alert"}] == [
        ("terminate", 100), ("wait", (100,), 5), ("kill", 100),
        ("wait", (100,), 5), ("alert", "sender_stop"),
    ]


def test_surviving_process_reports_failure_without_claiming_complete_stop(world):
    safety = world.monitor()
    root = safety.launch(["git", "fetch"])
    root.process.stubborn = root.process.unkillable = True

    with pytest.raises(SupervisorShutdownError):
        safety.request_stop()

    assert safety.stopped.is_set()
    assert world.events[-1] == ("alert", "shutdown_failed:SupervisorShutdownError")
    with pytest.raises(SupervisorShutdownError):
        safety.raise_if_failed()


def test_stop_is_sticky_blocks_new_launches_and_alerts_once(world):
    safety = world.monitor()
    safety.launch(["manage.py"])

    safety.request_stop()
    safety.request_stop()
    safety.request_stop(emergency=False)
    with pytest.raises(SupervisorStopped):
        safety.launch(["git", "fetch"])

    assert len(world.launches) == 1
    assert safety.emergency is True
    assert [event for event in world.events if event[0] == "alert"] == [("alert", "sender_stop")]


def test_normal_shutdown_stops_owned_work_without_emergency_alert(world):
    safety = world.monitor()
    root = safety.launch(["manage.py"])

    safety.request_stop(emergency=False)

    assert not root.process.alive
    assert safety.stopped.is_set()
    assert safety.emergency is False
    assert not [event for event in world.events if event[0] == "alert"]


def test_stop_racing_launch_waits_for_registration_then_contains_child(monkeypatch, world):
    safety = world.monitor()
    entered = Event()
    release = Event()
    stop_attempted = Event()

    def delayed_popen(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return world.Popen(*args, **kwargs)

    def stop():
        stop_attempted.set()
        safety.request_stop()

    monkeypatch.setattr(safety_module.subprocess, "Popen", delayed_popen)
    with ThreadPoolExecutor(max_workers=2) as executor:
        launching = executor.submit(safety.launch, ["git", "fetch"])
        try:
            assert entered.wait(timeout=5)
            stopping = executor.submit(stop)
            assert stop_attempted.wait(timeout=5)
        finally:
            release.set()
        root = launching.result(timeout=5)
        stopping.result(timeout=5)

    assert not root.process.alive
    with pytest.raises(SupervisorStopped):
        safety.launch(["manage.py"])


def test_reentrant_signal_during_popen_defers_alert_until_new_child_is_contained(monkeypatch, world):
    safety = world.monitor()

    def interrupted_popen(*args, **kwargs):
        safety.request_stop()
        assert not [event for event in world.events if event[0] == "alert"]
        return world.Popen(*args, **kwargs)

    monkeypatch.setattr(safety_module.subprocess, "Popen", interrupted_popen)

    with pytest.raises(SupervisorStopped):
        safety.launch(["git", "fetch"])

    assert not world.launches[0].process.alive
    assert world.events[-1] == ("alert", "sender_stop")


def test_runtime_unknown_control_keeps_workers_until_confirmed_stop(world):
    checks = Queue()
    unknown_seen = Event()
    alerted = Event()
    checks.put(None)

    def check():
        result = checks.get(timeout=5)
        if result is None:
            unknown_seen.set()
        return result

    safety = world.monitor(check, lambda reason: alerted.set())
    root = safety.launch(["manage.py"])
    git = safety.launch(["git", "fetch"])
    safety.start()

    assert unknown_seen.wait(timeout=5)
    assert root.process.alive and git.process.alive
    assert not safety.stopped.is_set()
    checks.put(True)
    assert alerted.wait(timeout=5)
    assert not root.process.alive and not git.process.alive
    safety.raise_if_failed()


def test_unexpected_checker_failure_stops_owned_work_and_propagates_original(world, caplog):
    error = ValueError("private-value-never-log")
    alerted = Event()
    reasons = []

    def check():
        raise error

    def alert(reason):
        reasons.append(reason)
        alerted.set()

    safety = world.monitor(check, alert)
    root = safety.launch(["manage.py"])
    safety.start()

    assert alerted.wait(timeout=5)
    assert not root.process.alive
    assert safety.stopped.is_set()
    assert reasons == ["control_error:ValueError"]
    with pytest.raises(ValueError) as caught:
        safety.raise_if_failed()
    assert caught.value is error
    assert "private-value-never-log" not in caplog.text


def test_stop_error_does_not_prevent_attempting_other_owned_processes(monkeypatch, world):
    safety = world.monitor()
    first = safety.launch(["manage.py"])
    second = safety.launch(["git", "fetch"])
    monkeypatch.setattr(second.process, "terminate", Mock(side_effect=psutil.AccessDenied(second.pid)))

    with pytest.raises(psutil.AccessDenied):
        safety.request_stop()

    assert not first.process.alive
    assert not second.process.alive
    assert world.events[-1] == ("alert", "shutdown_failed:AccessDenied")


def test_callback_failure_is_retained_after_processes_are_stopped(world):
    error = RuntimeError("notification failed")
    safety = world.monitor(on_stop=Mock(side_effect=error))
    root = safety.launch(["manage.py"])

    with pytest.raises(RuntimeError) as caught:
        safety.request_stop()

    assert caught.value is error
    assert not root.process.alive
    with pytest.raises(RuntimeError) as saved:
        safety.raise_if_failed()
    assert saved.value is error


def test_close_from_watchdog_callback_never_joins_itself(world):
    closed = Event()
    safety = None

    def alert(reason):
        safety.close()
        closed.set()

    safety = world.monitor(lambda: True, alert)
    safety.start()

    assert closed.wait(timeout=5)
    safety.raise_if_failed()


def test_close_has_bounded_join_even_when_checker_is_stalled(world):
    safety = world.monitor()
    thread = Mock()
    safety._thread = thread

    safety.close()

    thread.join.assert_called_once_with(timeout=35)


def test_close_waits_for_in_progress_watchdog_notification(world):
    callback_entered = Event()
    release_callback = Event()
    close_entered = Event()
    close_finished = Event()

    def alert(reason):
        callback_entered.set()
        assert release_callback.wait(timeout=5)

    safety = world.monitor(lambda: True, alert)
    safety.start()

    def close():
        close_entered.set()
        safety.close()
        close_finished.set()

    with ThreadPoolExecutor(max_workers=1) as executor:
        try:
            assert callback_entered.wait(timeout=5)
            closing = executor.submit(close)
            assert close_entered.wait(timeout=5)
            assert not close_finished.is_set()
        finally:
            release_callback.set()
        closing.result(timeout=5)
    assert close_finished.is_set()


def test_completed_root_records_are_pruned_without_losing_live_descendants(world):
    safety = world.monitor()
    first = safety.launch(["git", "fetch"])
    descendant = world.Process(200)
    first.process.descendants = [descendant]
    second = safety.launch(["git", "status"])
    first.process.alive = second.process.alive = False

    safety.launch(["git", "pull"])

    assert len(safety._roots) == 1
    assert {pid for pid, _created in safety._owned} == {102, 200}
    assert not safety._dead


def test_unexpected_preflight_error_prevents_launch_and_is_retained(world):
    error = ValueError("bad check")
    safety = world.monitor(Mock(side_effect=error))

    with pytest.raises(ValueError):
        safety.preflight()

    assert safety.stopped.is_set()
    with pytest.raises(SupervisorStopped):
        safety.launch(["manage.py"])
    assert world.launches == []


def test_invalid_check_value_fails_closed(world):
    safety = world.monitor(lambda: 1)

    with pytest.raises(TypeError):
        safety.preflight()

    assert safety.stopped.is_set()
