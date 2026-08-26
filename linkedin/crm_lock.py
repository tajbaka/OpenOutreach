"""Cross-host serialization for the CRM refresh workflow.

The CRM publisher mutates several related worksheet views.  A session-level
PostgreSQL advisory lock keeps two scheduler nodes from importing and
publishing the same workbook concurrently.  Tests and local SQLite runs use a
process-local non-blocking lock so accidental re-entry still fails closed.
"""
from __future__ import annotations

import hashlib
import threading
from contextlib import contextmanager
from typing import Iterator

from django.db import connections


class CrmRefreshAlreadyRunning(RuntimeError):
    """Raised when another CRM refresh owns the global publisher lock."""


_LOCAL_LOCK = threading.Lock()


def advisory_lock_key() -> int:
    digest = hashlib.blake2b(
        b"openoutreach:refresh-crm",
        digest_size=8,
    ).digest()
    return int.from_bytes(digest, byteorder="big", signed=True)


@contextmanager
def crm_refresh_lock() -> Iterator[None]:
    """Hold the CRM refresh lock for the complete import/publish cycle."""
    lock_connection = connections["default"].copy(alias="refresh_crm_lock")
    try:
        if lock_connection.vendor != "postgresql":
            if not _LOCAL_LOCK.acquire(blocking=False):
                raise CrmRefreshAlreadyRunning(
                    "Another CRM refresh is already running"
                )
            try:
                yield
            finally:
                _LOCAL_LOCK.release()
            return

        key = advisory_lock_key()
        with lock_connection.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(%s)", [key])
            acquired = bool(cursor.fetchone()[0])
        if not acquired:
            raise CrmRefreshAlreadyRunning(
                "Another CRM refresh is already running"
            )
        try:
            yield
        finally:
            with lock_connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [key])
    finally:
        lock_connection.close()
