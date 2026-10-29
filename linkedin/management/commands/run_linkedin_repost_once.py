"""Supervisor-owned child: execute at most one queued repost for this sender.

Attaches to the running daemon browser over loopback CDP only and opens its
own tabs. One attempt per row; uncertain rows are never retried. Each finished
row posts one message to the feed Slack channel (SLACK_HIGH_SIGNAL_URL).
"""
import json
import os
import signal
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from linkedin.exceptions import LinkedInRepostWorkerRefused, RepostBrowserUnavailableError, RepostInterruptedError
from linkedin.reposts import reposts_enabled, run_once


def _interrupt(signum, _frame):
    # Turn the supervisor's terminate into an exception so an attempted click
    # is recorded as uncertain and the owned tab is closed.
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    raise RepostInterruptedError("Repost worker asked to stop")


class Command(BaseCommand):
    help = "Run at most one queued LinkedIn repost for LINKEDIN_USERNAME's operator (supervised child)."

    def handle(self, *args, **opts):
        from linkedin.conf import LISTENER_CDP_PORT

        if not reposts_enabled():
            raise CommandError("ENABLE_LINKEDIN_REPOSTS is off; no repost was attempted.")
        signal.signal(signal.SIGTERM, _interrupt)
        root = Path(__file__).resolve().parents[3]
        try:
            summary = run_once(
                linkedin_username=os.environ.get("LINKEDIN_USERNAME", ""),
                root=root, port=LISTENER_CDP_PORT,
            )
        except LinkedInRepostWorkerRefused as exc:
            raise CommandError(f"Repost worker refused: {exc}") from None
        except RepostBrowserUnavailableError as exc:
            raise CommandError(f"Daemon browser unavailable ({exc.error_type}); rows stay queued.") from None
        self.stdout.write(json.dumps(summary, sort_keys=True))
