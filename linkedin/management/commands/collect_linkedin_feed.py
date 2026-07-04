"""Collect LinkedIn home-feed posts from the daemon browser over CDP."""
from __future__ import annotations

import logging
from pathlib import Path
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Collect LinkedIn home-feed posts for the daemon's current sender account."

    def add_arguments(self, parser):
        parser.add_argument("--job-id", type=int, help="Claim this specific due collection job.")
        parser.add_argument("--cdp-port", type=int, help="Override the daemon browser CDP port.")
        parser.add_argument("--max-posts", type=int, help="Override max posts collected this run.")
        parser.add_argument(
            "--stop-after-seen",
            type=int,
            help="Override repeated seen-observation stop threshold.",
        )
        parser.add_argument(
            "--scroll-pause-seconds",
            type=float,
            help="Override pause between feed scrolls.",
        )
        parser.add_argument(
            "--since-days",
            type=int,
            help=(
                "Force a one-off backfill to posts newer than now minus this "
                "many days, ignoring the normal daily cutoff."
            ),
        )

    def handle(self, *args, **opts):
        from linkedin.conf import get_daemon_handle
        from linkedin.feed_collection import (
            claim_due_collection_job,
            collect_feed_for_job,
            ensure_collection_jobs,
            mark_job_completed,
            mark_job_failed,
        )
        from linkedin.models import LinkedInProfile
        from linkedin.operators import resolve_operator
        from linkedin.single_instance import SingleInstanceGuard

        handle = get_daemon_handle()
        if not handle:
            raise CommandError(
                "No daemon LinkedIn account configured - set LINKEDIN_USERNAME in .env."
            )
        profile = (
            LinkedInProfile.objects.select_related("user")
            .filter(user__username=handle)
            .first()
        )
        if profile is None:
            raise CommandError(f"No LinkedInProfile for handle {handle!r}.")

        account_username = profile.linkedin_username
        operator = resolve_operator(account_username)
        since_days = opts.get("since_days")
        cutoff_at = None
        if since_days is not None:
            if since_days <= 0:
                raise CommandError("--since-days must be a positive integer.")
            cutoff_at = timezone.now() - timedelta(days=since_days)
            job = ensure_collection_jobs(
                operator=operator,
                account_username=account_username,
            )
            job.status = job.Status.RUNNING
            job.started_at = timezone.now()
            job.finished_at = None
            job.error = ""
            job.save(update_fields=["status", "started_at", "finished_at", "error", "updated_at"])
        else:
            job = claim_due_collection_job(
                operator=operator,
                account_username=account_username,
                job_id=opts.get("job_id"),
            )
        if job is None:
            self.stdout.write("No due LinkedIn feed collection job.")
            return

        guard = SingleInstanceGuard(
            pidfile=Path("data") / f"collect-linkedin-feed-{handle}.pid",
            marker="manage.py collect_linkedin_feed",
            logger=logger,
        )
        try:
            guard.acquire()
            self.stdout.write(
                f"Collecting LinkedIn feed for {operator} ({account_username})..."
            )
            result = collect_feed_for_job(
                job,
                cdp_port=opts.get("cdp_port"),
                cutoff_at=cutoff_at,
                max_posts=opts.get("max_posts"),
                stop_after_seen=opts.get("stop_after_seen"),
                scroll_pause_seconds=opts.get("scroll_pause_seconds"),
            )
            mark_job_completed(job, result)
            self.stdout.write(
                self.style.SUCCESS(
                    "Collected "
                    f"{result.posts_seen} posts "
                    f"({result.posts_created} new posts, "
                    f"{result.observations_created} new observations)."
                ),
            )
        except Exception as exc:
            mark_job_failed(job, str(exc))
            raise CommandError(f"LinkedIn feed collection failed: {exc}") from exc
        finally:
            guard.release()
