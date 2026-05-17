"""Per-account 'listener was last alive at' timestamp file.

One JSON file per LinkedIn username at
data/listener-heartbeat-<safe_username>.json — same data/ + safe-name
convention as the cookie store. Refreshed periodically by the listener
while it runs; read once at daemon startup by the catch-up.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path

from django.utils import timezone

from linkedin.conf import ROOT_DIR

logger = logging.getLogger(__name__)

_SAFE_NAME_RE = re.compile(r"[^a-z0-9]+")


def heartbeat_path_for(username: str) -> Path:
    """data/listener-heartbeat-<safe>.json for a LinkedIn username.

    Safe-name rule matches linkedin.browser.cookie_store.cookie_path_for.
    """
    safe = _SAFE_NAME_RE.sub("-", (username or "").lower()).strip("-")
    if not safe:
        raise ValueError("cannot derive heartbeat path from empty username")
    return ROOT_DIR / "data" / f"listener-heartbeat-{safe}.json"


def write_heartbeat(username: str) -> None:
    """Stamp the heartbeat file with the current time. Best-effort."""
    path = heartbeat_path_for(username)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"last_alive": timezone.now().isoformat()}),
            encoding="utf-8",
        )
    except OSError as e:
        logger.warning("Failed to write heartbeat file %s: %s", path, e)


def read_heartbeat(username: str) -> datetime | None:
    """Return the last-alive datetime, or None if missing / unreadable.

    The returned datetime is timezone-aware (guaranteed by USE_TZ=True +
    isoformat()), so callers can safely subtract timezone.now().
    """
    path = heartbeat_path_for(username)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return datetime.fromisoformat(data["last_alive"])
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        logger.warning("Unreadable heartbeat file %s: %s", path, e)
        return None
