"""Independent sender-stop checks and containment of supervisor-owned children.

Process ownership comes only from a subprocess launched here or a descendant
observed beneath one. Captured descendants remain tracked after their parent
exits. An already-orphaned process that was never observed cannot be recovered
by PID/name guesses and is deliberately outside this registry.
"""
from __future__ import annotations

import logging
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass

import psutil

from linkedin.exceptions import SupervisorShutdownError, SupervisorStopped


logger = logging.getLogger(__name__)
TERMINATE_TIMEOUT_SECONDS = 5
KILL_TIMEOUT_SECONDS = 5
WATCHDOG_CLOSE_TIMEOUT_SECONDS = 35


@dataclass(frozen=True)
class _OwnedProcess:
    process: psutil.Process
    created_at: float

    @property
    def identity(self) -> tuple[int, float]:
        return self.process.pid, self.created_at


class SupervisorSafety:
    """Fail closed on a persistent sender stop, independently of the main loop."""

    def __init__(
        self,
        check_stop: Callable[[], bool | None],
        on_stop: Callable[[str], None],
        interval_seconds: float = 15,
    ):
        if interval_seconds <= 0:
            raise ValueError("Stop watchdog interval must be positive")
        self.stopped = threading.Event()
        self.emergency = False
        self._check_stop = check_stop
        self._on_stop = on_stop
        self._interval_seconds = interval_seconds
        # Signals can run in the launching thread. Reentrancy plus a second
        # stopped check after Popen contains a launch interrupted by a signal.
        self._lock = threading.RLock()
        self._closed = threading.Event()
        self._thread: threading.Thread | None = None
        self._failure: Exception | None = None
        self._notified = False
        self._stopping = False
        self._launching = False
        self._owned: dict[tuple[int, float], _OwnedProcess] = {}
        self._dead: set[tuple[int, float]] = set()
        self._snapshotted: set[tuple[int, float]] = set()
        self._roots: list[subprocess.Popen] = []
        self._root_identities: dict[int, tuple[int, float]] = {}

    @staticmethod
    def _validate_check(result: bool | None) -> bool | None:
        if result is not None and result is not True and result is not False:
            raise TypeError("Stop checker must return True, False, or None")
        return result

    def preflight(self) -> bool:
        """Require a known clear stop flag before launching initial workers."""
        if self.stopped.is_set():
            return False
        try:
            result = self._validate_check(self._check_stop())
        except Exception as exc:
            self._fail_closed(exc)
            raise
        if result is True:
            self.request_stop()
        return result is False and not self.stopped.is_set()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None or self._closed.is_set() or self.stopped.is_set():
                return
            self._thread = threading.Thread(
                target=self._watch, name="supervisor-sender-stop", daemon=True,
            )
            self._thread.start()

    def close(self) -> None:
        """Stop checking; owned processes are stopped separately by request_stop."""
        self._closed.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            # A confirmed stop may already have killed workers and be inside
            # the urgent notification's bounded primary/fallback requests.
            thread.join(timeout=WATCHDOG_CLOSE_TIMEOUT_SECONDS)

    def raise_if_failed(self) -> None:
        with self._lock:
            failure = self._failure
        if failure is not None:
            raise failure

    def _remember(self, process: psutil.Process) -> _OwnedProcess:
        record = _OwnedProcess(process, process.create_time())
        self._owned.setdefault(record.identity, record)
        return self._owned[record.identity]

    def _live(self, record: _OwnedProcess) -> bool:
        if record.identity in self._dead:
            return False
        try:
            # is_running performs psutil's PID-reuse check; create_time alone
            # may be cached on the handle and must not be the only guard.
            alive = (
                record.process.is_running()
                and record.process.create_time() == record.created_at
            )
        except psutil.NoSuchProcess:
            alive = False
        if not alive:
            self._dead.add(record.identity)
        return alive

    def _capture_descendants(self) -> None:
        failures: list[Exception] = []
        for record in list(self._owned.values()):
            try:
                if not self._live(record):
                    continue
                descendants = record.process.children(recursive=True)
            except psutil.NoSuchProcess:
                continue
            except Exception as exc:
                failures.append(exc)
                continue
            for child in descendants:
                try:
                    self._remember(child)
                except psutil.NoSuchProcess:
                    continue
                except Exception as exc:
                    failures.append(exc)
            self._snapshotted.add(record.identity)
        # Prune only exited identities whose descendants were captured. Any
        # observed orphan stays in _owned independently of its former root.
        retired = self._dead & self._snapshotted
        for identity in retired:
            self._owned.pop(identity, None)
        self._dead.difference_update(retired)
        self._snapshotted.difference_update(retired)
        retained_roots = []
        for proc in self._roots:
            if self._root_identities.get(id(proc)) in retired:
                self._root_identities.pop(id(proc), None)
            else:
                retained_roots.append(proc)
        self._roots = retained_roots
        if failures:
            raise failures[0]

    def launch(self, args, **kwargs) -> subprocess.Popen:
        """Create and register a subprocess atomically against the stop latch."""
        with self._lock:
            if self.stopped.is_set():
                raise SupervisorStopped("Supervisor is stopped; child launch refused")
            self._launching = True
            try:
                proc = subprocess.Popen(args, **kwargs)
                self._roots.append(proc)
                try:
                    record = self._remember(psutil.Process(proc.pid))
                except psutil.NoSuchProcess:
                    # A very short command may have exited before inspection.
                    # Keep its exact Popen object for shutdown/reaping checks.
                    pass
                else:
                    self._root_identities[id(proc)] = record.identity
                self._capture_descendants()
            except Exception as exc:
                self._launching = False
                self._fail_closed(exc)
                raise
            self._launching = False
            if self.stopped.is_set():
                self.request_stop(emergency=False)
                raise SupervisorStopped("Supervisor stopped during child launch")
            return proc

    def _record_failure(self, exc: Exception) -> None:
        with self._lock:
            if self._failure is None:
                self._failure = exc
        logger.error("Supervisor safety failed (%s)", type(exc).__name__)

    def _terminate_owned(self) -> None:
        """Attempt every owned root/tree even when one cannot be inspected."""
        failures: list[Exception] = []
        try:
            self._capture_descendants()
        except Exception as exc:
            failures.append(exc)

        candidates: list[_OwnedProcess] = []
        # Descendants are registered after parents, so stop them first after
        # the complete snapshot. No machine-wide enumeration is used.
        for record in reversed(list(self._owned.values())):
            try:
                if self._live(record):
                    candidates.append(record)
                    record.process.terminate()
            except psutil.NoSuchProcess:
                continue
            except Exception as exc:
                failures.append(exc)

        processes = [record.process for record in candidates]
        try:
            _gone, alive = psutil.wait_procs(processes, timeout=TERMINATE_TIMEOUT_SECONDS)
        except Exception as exc:
            failures.append(exc)
            alive = processes
        alive_ids = {id(process) for process in alive}
        survivors: list[psutil.Process] = []
        for record in candidates:
            if id(record.process) not in alive_ids:
                continue
            try:
                if self._live(record):
                    survivors.append(record.process)
                    record.process.kill()
            except psutil.NoSuchProcess:
                continue
            except Exception as exc:
                failures.append(exc)
        try:
            _gone, alive = psutil.wait_procs(survivors, timeout=KILL_TIMEOUT_SECONDS)
        except Exception as exc:
            failures.append(exc)
        else:
            if alive:
                failures.append(SupervisorShutdownError("Owned processes survived shutdown deadlines"))

        # Only roots whose identity could not be captured use their original
        # Popen handle. Popen checks/reaps its own child before signalling it.
        for proc in self._roots:
            if id(proc) in self._root_identities:
                continue
            try:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=TERMINATE_TIMEOUT_SECONDS)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=KILL_TIMEOUT_SECONDS)
            except ProcessLookupError:
                continue
            except Exception as exc:
                failures.append(exc)
        if failures:
            raise failures[0]

    def _shutdown(self, *, emergency: bool, reason: str | None) -> None:
        failure = None
        notify_reason = None
        with self._lock:
            self.stopped.set()
            self.emergency = self.emergency or emergency
            if self._stopping or self._launching:
                return
            self._stopping = True
            try:
                self._terminate_owned()
            except Exception as exc:
                failure = exc
                self._record_failure(exc)
                reason = f"shutdown_failed:{type(exc).__name__}"
            finally:
                self._stopping = False
            if not self._notified and (reason is not None or self.emergency):
                self._notified = True
                notify_reason = reason or "sender_stop"
        if notify_reason is not None:
            try:
                self._on_stop(notify_reason)
            except Exception as exc:
                self._record_failure(exc)
                if failure is None:
                    failure = exc
        if failure is not None:
            raise failure

    def request_stop(self, *, emergency: bool = True) -> None:
        """Latch cancellation, stop only owned work, then notify at most once."""
        self._shutdown(emergency=emergency, reason="sender_stop" if emergency else None)

    def _fail_closed(self, exc: Exception) -> None:
        self._record_failure(exc)
        try:
            self._shutdown(emergency=False, reason=f"control_error:{type(exc).__name__}")
        except Exception:
            # _shutdown already recorded/logged its error. Preserve the first
            # checker failure for the main thread rather than hiding it.
            pass

    def _watch(self) -> None:
        try:
            while not self._closed.wait(self._interval_seconds):
                if self.stopped.is_set():
                    return
                with self._lock:
                    self._capture_descendants()
                result = self._validate_check(self._check_stop())
                if result is True:
                    self.request_stop()
                    return
                # None is an expected transient read failure during runtime;
                # the independent checker will retry without killing workers.
        except Exception as exc:
            self._fail_closed(exc)
