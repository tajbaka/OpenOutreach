"""Single-instance process guard using psutil.

Used for long-running local worker processes that should only have one live
instance per account/repo at a time. On startup the guard checks a pidfile,
verifies whether the recorded process is still alive and matches the expected
command marker, and if so terminates that stale process tree before claiming
the pidfile for the current process.
"""
from __future__ import annotations

import atexit
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import psutil


@dataclass
class _PidRecord:
    pid: int
    marker: str
    created_at: float | None = None


class SingleInstanceGuard:
    """Ensure only one matching process instance is alive at a time."""

    def __init__(
        self,
        *,
        pidfile: Path,
        marker: str,
        logger,
        match_process: Callable[[psutil.Process], bool] | None = None,
    ):
        self._pidfile = pidfile
        self._marker = marker
        self._logger = logger
        self._match_process = match_process
        self._claimed = False

    def acquire(self) -> None:
        self._pidfile.parent.mkdir(parents=True, exist_ok=True)
        prior = self._read_record()
        if prior and prior.pid != os.getpid() and self._is_matching_process_alive(prior):
            self._logger.warning(
                "Found prior %s instance (pid=%s) — terminating it before startup",
                self._marker,
                prior.pid,
            )
            self._terminate_process_tree(prior.pid)

        if self._match_process is not None:
            self._terminate_legacy_matches()

        self._write_record()
        self._claimed = True
        atexit.register(self.release)

    def release(self) -> None:
        if not self._claimed:
            return
        try:
            current = self._read_record()
            if current and current.pid == os.getpid():
                self._pidfile.unlink(missing_ok=True)
        finally:
            self._claimed = False

    def _read_record(self) -> _PidRecord | None:
        try:
            payload = json.loads(self._pidfile.read_text())
        except FileNotFoundError:
            return None
        except Exception as exc:
            self._logger.debug("Could not parse pidfile %s: %s", self._pidfile, exc)
            return None
        try:
            return _PidRecord(
                pid=int(payload["pid"]),
                marker=str(payload["marker"]),
                created_at=float(payload["created_at"]) if payload.get("created_at") is not None else None,
            )
        except Exception:
            return None

    def _write_record(self) -> None:
        payload = {
            "pid": os.getpid(),
            "marker": self._marker,
            "created_at": psutil.Process(os.getpid()).create_time(),
        }
        tmp = self._pidfile.with_suffix(self._pidfile.suffix + ".tmp")
        tmp.write_text(json.dumps(payload))
        tmp.replace(self._pidfile)

    def _is_matching_process_alive(self, record: _PidRecord) -> bool:
        try:
            proc = psutil.Process(record.pid)
            if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
                return False
            if record.created_at is not None and abs(proc.create_time() - record.created_at) > 1:
                return False
            cmdline = " ".join(proc.cmdline())
            return record.marker == self._marker and self._marker in cmdline
        except psutil.Error:
            return False

    def _terminate_legacy_matches(self) -> None:
        victims: list[int] = []
        own_process_tree = {os.getpid()}
        try:
            proc = psutil.Process(os.getpid())
            own_process_tree.update(parent.pid for parent in proc.parents())
        except psutil.Error:
            pass
        for proc in psutil.process_iter(["pid"]):
            try:
                if proc.pid in own_process_tree:
                    continue
                if self._match_process and self._match_process(proc):
                    victims.append(proc.pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        for pid in victims:
            self._logger.warning(
                "Found legacy %s instance (pid=%s) — terminating it before startup",
                self._marker,
                pid,
            )
            self._terminate_process_tree(pid)

    def _terminate_process_tree(self, pid: int) -> None:
        try:
            parent = psutil.Process(pid)
        except psutil.Error:
            return

        victims = parent.children(recursive=True)
        victims.append(parent)
        for proc in victims:
            try:
                proc.terminate()
            except psutil.Error:
                pass
        _, alive = psutil.wait_procs(victims, timeout=5)
        for proc in alive:
            try:
                proc.kill()
            except psutil.Error:
                pass
        psutil.wait_procs(alive, timeout=2)
        self._logger.info(
            "Stopped prior %s process tree rooted at pid=%s",
            self._marker,
            pid,
        )
