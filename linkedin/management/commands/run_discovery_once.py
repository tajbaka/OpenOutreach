"""Run a bounded discovery-only browser session for one LinkedIn sender."""
from __future__ import annotations

import time

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from linkedin import conf
from linkedin.browser.registry import get_or_create_session
from linkedin.discovery.browser_safety import assert_discovery_browser_available
from linkedin.discovery.collector import (
    discovery_available_now,
    discovery_enabled_for_sender,
    reconcile_discovery_tasks,
)
from linkedin.discovery.config import (
    discovery_day_end,
    validate_discovery_settings,
)
from linkedin.exceptions import DiscoverySessionConflictError
from linkedin.models import LinkedInDiscoveryLead, LinkedInProfile, Task
from linkedin.operators import resolve_operator
from linkedin.tasks.discovery import handle_discovery


class Command(BaseCommand):
    help = (
        "Run bounded sender-scoped discovery Tasks in one browser session without claiming "
        "connect, follow-up, sweep, status, or manual-reply work."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--handle",
            required=True,
            help="Django username for the LinkedInProfile to run.",
        )
        parser.add_argument(
            "--max-tasks",
            type=int,
            default=1,
            help="Maximum discovery Tasks to process in this browser session (default: 1).",
        )

    def handle(self, *args, **options):
        validate_discovery_settings()
        if not conf.ENABLE_PROFILE_DISCOVERY:
            raise CommandError("ENABLE_PROFILE_DISCOVERY is false.")
        max_tasks = options["max_tasks"]
        if max_tasks < 1:
            raise CommandError("--max-tasks must be at least 1.")

        try:
            profile = LinkedInProfile.objects.select_related("user").get(
                active=True,
                user__username=options["handle"],
            )
        except LinkedInProfile.DoesNotExist as exc:
            raise CommandError("No matching active LinkedInProfile found.") from exc

        operator = resolve_operator(profile.linkedin_username)
        try:
            assert_discovery_browser_available(
                operator=operator,
                account_username=profile.linkedin_username,
            )
        except DiscoverySessionConflictError as exc:
            raise CommandError(str(exc)) from exc
        if not discovery_enabled_for_sender(profile, operator):
            raise CommandError(
                f"Discovery is not configured for sender {operator}.",
            )
        if not discovery_available_now(profile, operator):
            raise CommandError(
                "Discovery is not currently eligible: weekday connection work "
                "is incomplete or today's discovery limit has been reached.",
            )

        reconcile_discovery_tasks(profile, operator)
        campaign_ids = list(
            profile.user.campaigns.filter(status="active").values_list(
                "pk",
                flat=True,
            ),
        )
        wait_seconds = Task.objects.seconds_to_next(
            operator=operator,
            campaign_ids=campaign_ids,
            task_types={Task.TaskType.DISCOVERY},
        )
        if wait_seconds is None:
            raise CommandError("No due discovery Task is available for this sender.")

        before = LinkedInDiscoveryLead.objects.filter(
            stored_by_operator=operator,
        ).count()
        session = get_or_create_session(handle=profile.user.username)
        session.campaign = session.campaigns.filter(status="active").first()
        completed = 0
        try:
            session.ensure_browser()
            while completed < max_tasks:
                wait_seconds = Task.objects.seconds_to_next(
                    operator=operator,
                    campaign_ids=campaign_ids,
                    task_types={Task.TaskType.DISCOVERY},
                )
                if wait_seconds is None:
                    break
                day_end = discovery_day_end()
                if wait_seconds:
                    if wait_seconds >= (day_end - timezone.now()).total_seconds():
                        break
                    time.sleep(wait_seconds)

                task = Task.objects.claim_next(
                    operator=operator,
                    campaign_ids=campaign_ids,
                    task_types={Task.TaskType.DISCOVERY},
                )
                if task is None:
                    continue
                try:
                    handle_discovery(task, session)
                except Exception as exc:
                    task.mark_failed(str(exc))
                    raise
                task.mark_completed()
                completed += 1
        finally:
            session.close()

        after = LinkedInDiscoveryLead.objects.filter(
            stored_by_operator=operator,
        ).count()
        self.stdout.write(
            self.style.SUCCESS(
                f"Completed {completed} discovery Task(s) for {operator}; "
                f"new_profiles={after - before}.",
            ),
        )
