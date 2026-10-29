"""Inspect the local sender's sampler, or explicitly resume after review."""
import json
import os
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from linkedin.feed_sampling import FeedSampleSchedule, MAX_SECONDS, PROCESS_GRACE_SECONDS, write_json


class Command(BaseCommand):
    help = "Show local feed sampling state. Resuming requires reviewing the failed run/browser first."

    def add_arguments(self, parser):
        parser.add_argument("--resume-after-review", action="store_true")

    def handle(self, *args, **opts):
        root = Path(__file__).resolve().parents[3]
        schedule = FeedSampleSchedule(root, os.environ.get("LINKEDIN_USERNAME", ""))
        state = schedule.read()
        if opts["resume_after_review"]:
            now = datetime.now(timezone.utc)
            if state.get("running"):
                started = datetime.fromisoformat(state["started_at"])
                if (now - started).total_seconds() <= MAX_SECONDS + PROCESS_GRACE_SECONDS:
                    raise CommandError("A sample may still be running; do not clear its reservation")
            if not state.get("hold") and not state.get("running"):
                raise CommandError("There is no feed collection hold to resume")
            # Keep the previous run's receipt. Resume never launches a browser;
            # the next eligible supervisor check remains 55–65 minutes away.
            delay = random.randint(55 * 60, 65 * 60)
            state.update(running=False, hold="", reviewed_at=now.isoformat(),
                         next_due_at=(now + timedelta(seconds=delay)).isoformat(), interval_seconds=delay)
            write_json(schedule.path, state)
        self.stdout.write(json.dumps(state or {"status": "No local sample has run"}, indent=2))
