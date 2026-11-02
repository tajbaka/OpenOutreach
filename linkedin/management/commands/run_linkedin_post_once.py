"""Supervisor-owned child: publish at most one queued personal post for this sender.

Attaches to the running daemon browser over loopback CDP only and opens its
own tabs. One attempt per row; uncertain rows are never retried. Each finished
row posts one message to the feed Slack channel (SLACK_HIGH_SIGNAL_URL).
"""
import json
import os
import signal
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from linkedin.exceptions import LinkedInPostWorkerRefused, PostBrowserUnavailableError, PostInterruptedError
from linkedin.personal_posts import run_once


def _interrupt(signum, _frame):
    # Turn the supervisor's terminate into an exception so an attempted click
    # is recorded as uncertain and the owned tab is closed.
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    raise PostInterruptedError("Post worker asked to stop")


class Command(BaseCommand):
    help = "Publish at most one queued personal LinkedIn post for LINKEDIN_USERNAME's operator (supervised child)."

    def handle(self, *args, **opts):
        from linkedin.conf import LISTENER_CDP_PORT

        signal.signal(signal.SIGTERM, _interrupt)
        root = Path(__file__).resolve().parents[3]
        try:
            summary = run_once(
                linkedin_username=os.environ.get("LINKEDIN_USERNAME", ""),
                root=root, port=LISTENER_CDP_PORT,
            )
        except LinkedInPostWorkerRefused as exc:
            raise CommandError(f"Post worker refused: {exc}") from None
        except PostBrowserUnavailableError as exc:
            raise CommandError(f"Daemon browser unavailable ({exc.error_type}); rows stay queued.") from None
        self.stdout.write(json.dumps(summary, sort_keys=True))
