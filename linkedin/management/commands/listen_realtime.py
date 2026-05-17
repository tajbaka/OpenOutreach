"""`manage.py listen_realtime` — the realtime listener child process.

Spawned and supervised by the daemon (see linkedin/realtime/supervisor.py).
Resolves the same LinkedIn account the daemon runs as, then connects to the
daemon's browser over CDP and streams inbound messages. Not meant to be run
by hand in normal operation, though it can be for debugging.
"""
from __future__ import annotations

import logging
import sys

from django.core.management.base import BaseCommand, CommandError

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Run the realtime inbound-message listener (child process of the daemon)."

    def handle(self, *args, **opts):
        from linkedin.conf import get_daemon_handle
        from linkedin.models import LinkedInProfile
        from linkedin.operators import resolve_operator
        from linkedin.realtime.listener import run_listener

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
        logger.info("listen_realtime: starting for operator=%s (%s)", operator, username)
        code = run_listener(operator=operator, username=username)
        sys.exit(code)
