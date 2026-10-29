"""One supervisor-owned, bounded fresh-feed check; never claims historical jobs."""
import os
import time
import traceback
from datetime import datetime
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from linkedin.feed_sampling import FeedSampleSchedule, SampleWatchdog, claim_receipt, run_sample, write_json


class Command(BaseCommand):
    help = "Run one reserved fresh-feed sample for 165–195 seconds with paced scrolling."

    def add_arguments(self, parser):
        parser.add_argument("--run-id", required=True)

    def handle(self, *args, **opts):
        from linkedin.conf import get_daemon_handle, LISTENER_CDP_PORT
        from linkedin.models import LinkedInProfile
        from linkedin.operators import resolve_operator

        root = Path(__file__).resolve().parents[3]
        schedule = FeedSampleSchedule(root, os.environ.get("LINKEDIN_USERNAME", ""))
        state = schedule.read()
        if not state.get("running") or state.get("hold") or state.get("run_id") != opts["run_id"]:
            raise CommandError("No matching reserved feed sample")
        receipt = schedule.receipt_path(opts["run_id"])
        watchdog = SampleWatchdog(LISTENER_CDP_PORT, receipt, started_at=datetime.fromisoformat(state["started_at"]))
        watchdog.start()
        claimed = False
        try:
            # The reservation admits one run per sender checkout, and only the
            # process that creates its receipt may act on it. No Postgres session
            # lock: a transaction pooler can strand one on a pooled session.
            claimed = claim_receipt(receipt, {
                "status": "starting", "target_id": "", "duration_seconds": watchdog.duration_seconds,
            })
            if not claimed:
                raise CommandError("This sample already attempted collection")
            if watchdog.deadline <= time.monotonic():
                raise CommandError("The reserved sample expired before it started")
            handle = get_daemon_handle()
            profile = LinkedInProfile.objects.get(user__username=handle)
            if profile.stop_requested or not profile.active:
                raise CommandError("Feed sender is stopped or inactive")
            operator = resolve_operator(profile.linkedin_username)
            result = run_sample(
                operator=operator, account_username=profile.linkedin_username,
                port=LISTENER_CDP_PORT, watchdog=watchdog,
            )
            write_json(receipt, {
                "status": "completed", "target_id": "", "duration_seconds": watchdog.duration_seconds, **result,
            })
            self.stdout.write(f"Feed sample: {result['posts_created']} new posts; {result['stop_reason']}.")
        except Exception as exc:
            # The supervisor latches a persistent hold. Do not expose page text,
            # connection strings, or provider exception messages.
            if claimed:
                write_json(receipt, {
                    "status": "failed", "error_type": type(exc).__name__,
                    "target_id": watchdog.target_id, "cdp_port": LISTENER_CDP_PORT,
                    "duration_seconds": watchdog.duration_seconds,
                    "frames": [
                        {"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
                        for frame in traceback.extract_tb(exc.__traceback__)[-6:]
                    ],
                })
            raise CommandError(f"Feed sample held: {type(exc).__name__}") from None
        finally:
            watchdog.stop()
