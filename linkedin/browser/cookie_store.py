"""Disk-cached Playwright `storage_state` for LinkedIn sessions.

Shared between the daemon's `AccountSession` (was DB-backed via
`LinkedInProfile.cookie_data`) and the standalone scripts'
`StandaloneLinkedInSession`. One file per LinkedIn username at
`data/cookies-<safe_username>.json` — same convention the standalone
sessions have used since 2026-05-11, now reused by the daemon.

Why per-username (not per-slot): swapping which account a slot points
at (e.g. flipping `LINKEDIN_USERNAME` in .env from Arian to Chuka)
silently kept the old account's cookies under a slot-named file. The
validity check still passed because the feed loads — just as the
wrong identity. Per-username filenames mean two accounts never
collide on disk; a mismatched username just means no cached file
exists and the session forces a fresh login.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from linkedin.conf import ROOT_DIR

logger = logging.getLogger(__name__)

_SAFE_NAME_RE = re.compile(r"[^a-z0-9]+")


def cookie_path_for(username: str) -> Path:
    """Return the cookie cache path for a given LinkedIn username.

    Convention: lowercase the username, replace any run of non-alphanumerics
    with a single `-`, strip leading/trailing `-`. So
        chukyjack@gmail.com → cookies-chukyjack-gmail-com.json
        arian@tryfedrampgpt.com → cookies-arian-tryfedrampgpt-com.json
    """
    safe = _SAFE_NAME_RE.sub("-", (username or "").lower()).strip("-")
    if not safe:
        raise ValueError("cannot derive cookie path from empty username")
    return ROOT_DIR / "data" / f"cookies-{safe}.json"


def profile_dir_for(username: str) -> Path:
    """Return the persistent-context profile directory for a LinkedIn username.

    Convention mirrors `cookie_path_for`: `data/profile-<safe_username>/`.
    Used by the daemon's `launch_persistent_context` (the listener child
    process shares this context over CDP).
    """
    safe = _SAFE_NAME_RE.sub("-", (username or "").lower()).strip("-")
    if not safe:
        raise ValueError("cannot derive profile dir from empty username")
    return ROOT_DIR / "data" / f"profile-{safe}"


def load_cookies(path: Path) -> dict | None:
    """Read cached Playwright storage_state from disk, or None on miss / corrupt."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Failed to read cached cookies at %s: %s", path, e)
        return None


def save_cookies(state: dict, path: Path) -> None:
    """Persist Playwright `context.storage_state()` to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state), encoding="utf-8")


def clear_cookies(path: Path) -> None:
    """Remove a stale cookie file. Idempotent — missing path is a no-op."""
    try:
        path.unlink()
    except FileNotFoundError:
        pass
