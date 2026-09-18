"""Bounded, content-free diagnostics for independent Gmail workers."""
from __future__ import annotations

import json
import logging
import os
import traceback
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler

from gmail.auth import account_for_key
from linkedin.conf import ROOT_DIR

logger = logging.getLogger(__name__)
logger.propagate = False
logger.addHandler(logging.NullHandler())
LOG_DIR = ROOT_DIR / "data" / "logs"


def _exception_details(exc):
    # Do not serialize exception messages, source lines, locals, or HTTP bodies.
    details = []
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        message = str(exc).lower()
        reason = "unclassified"
        for label, needles in (
            ("host_resolution_failed", ("getaddrinfo failed", "failed to resolve host", "could not translate host name")),
            ("connection_refused", ("connection refused", "actively refused")),
            ("connection_timeout", ("connection timeout", "connecttimeout", "connection timed out")),
            ("connection_closed", ("server closed the connection", "connection is closed", "connection reset")),
            ("oauth_invalid_grant", ("invalid_grant",)),
            ("authentication_failed", ("password authentication failed", "invalid credentials")),
            ("rate_limited", ("ratelimitexceeded", "userratelimitexceeded")),
        ):
            if any(needle in message for needle in needles):
                reason = label
                break
        details.append({
            "type": f"{type(exc).__module__}.{type(exc).__name__}",
            "reason": reason,
            "frames": [{"file": frame.filename, "line": frame.lineno, "function": frame.name}
                       for frame in traceback.extract_tb(exc.__traceback__)],
        })
        exc = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)
    return details


def log_worker_event(account, event, *, stage=None, task_id=None, task_type=None, exception=None):
    record = {"account": account, "pid": os.getpid(), "event": event}
    record.update({key: value for key, value in {
        "stage": stage, "task_id": task_id, "task_type": task_type,
    }.items() if value is not None})
    if exception is not None:
        record["exceptions"] = _exception_details(exception)
    logger.log(logging.ERROR if exception is not None else logging.INFO,
               json.dumps(record, sort_keys=True), extra={"worker_account": account})


@contextmanager
def worker_log(account):
    account_for_key(account)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        LOG_DIR / f"gmail-worker-{account}.log",
        maxBytes=5_000_000, backupCount=3, encoding="utf-8",
    )
    handler.addFilter(lambda record: getattr(record, "worker_account", None) == account)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    previous_level = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        yield
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
        handler.close()
