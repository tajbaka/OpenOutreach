"""`manage.py listen_realtime` — the realtime listener child process.

Spawned and supervised by the daemon (see linkedin/realtime/supervisor.py).
Resolves the same LinkedIn account the daemon runs as, then connects to the
daemon's browser over CDP and streams inbound messages. Not meant to be run
by hand in normal operation, though it can be for debugging.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Run the realtime inbound-message listener (child process of the daemon)."

    def handle(self, *args, **opts):
        from linkedin.conf import ROOT_DIR, get_daemon_handle
        from linkedin.models import LinkedInProfile
        from linkedin.operators import resolve_operator
        from linkedin.realtime.listener import run_listener
        from linkedin.single_instance import SingleInstanceGuard

        handle = get_daemon_handle()
        if not handle:
            raise CommandError(
                "No daemon LinkedIn account configured — set LINKEDIN_USERNAME in .env."
            )
        profile = (
            LinkedInProfile.objects.select_related("user")
            .filter(user__username=handle)
            .first()
        )
        if profile is None:
            raise CommandError(f"No LinkedInProfile for handle {handle!r}.")

        username = profile.linkedin_username
        operator = resolve_operator(username)
        pidfile = Path("data") / f"listen-realtime-{handle}.pid"
        marker = "manage.py listen_realtime"

        def _matches_listener(proc) -> bool:
            try:
                cmdline = proc.cmdline()
                if not cmdline or "manage.py" not in cmdline or "listen_realtime" not in cmdline:
                    return False
                return Path(proc.cwd()) == ROOT_DIR
            except (psutil.Error, OSError):
                return False

        import psutil
        guard = SingleInstanceGuard(
            pidfile=pidfile,
            marker=marker,
            logger=logger,
            match_process=_matches_listener,
        )
        guard.acquire()
        logger.info("listen_realtime: starting for operator=%s (%s)", operator, username)
        try:
            code = run_listener(operator=operator, username=username)
        finally:
            guard.release()
        sys.exit(code)
