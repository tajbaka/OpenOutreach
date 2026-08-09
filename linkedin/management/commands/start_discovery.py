"""Inspect or enqueue the standalone profile-discovery lane."""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from linkedin import conf
from linkedin.discovery.collector import enqueue_discovery
from linkedin.discovery.config import discovery_gate_open, validate_discovery_settings
from linkedin.discovery.limits import remaining_today, saved_today
from linkedin.icp_outbound import (
    discovery_search_queries,
    load_discovery_targets,
)
from linkedin.models import LinkedInProfile
from linkedin.operators import resolve_operator


class Command(BaseCommand):
    help = "Inspect or enqueue bounded LinkedIn profile discovery."

    def add_arguments(self, parser):
        parser.add_argument(
            "--handle",
            help="Django username for one LinkedInProfile; omit for all active profiles.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show sender configuration and capacity without creating a Task.",
        )

    def handle(self, *args, **options):
        validate_discovery_settings()
        profiles = LinkedInProfile.objects.filter(active=True).select_related("user")
        if options["handle"]:
            profiles = profiles.filter(user__username=options["handle"])
        profiles = list(profiles.order_by("user__username"))
        if not profiles:
            raise CommandError("No matching active LinkedInProfile found.")

        if not conf.ENABLE_PROFILE_DISCOVERY and not options["dry_run"]:
            raise CommandError(
                "ENABLE_PROFILE_DISCOVERY is false; no tasks were enqueued.",
            )

        for profile in profiles:
            operator = resolve_operator(profile.linkedin_username)
            targets = load_discovery_targets(operator)
            queries = discovery_search_queries(targets)
            saved = saved_today(operator)
            remaining = remaining_today(operator)
            gate = discovery_gate_open(profile)

            self.stdout.write(
                f"{operator} ({profile.user.username}): "
                f"enabled_icps={len(targets)} queries={len(queries)} "
                f"saved_today={saved}/{conf.DISCOVERY_DAILY_LIMIT} "
                f"remaining={remaining} eligible_now={gate}",
            )
            if options["dry_run"]:
                continue
            if enqueue_discovery(profile, operator):
                self.stdout.write(self.style.SUCCESS(f"Queued discovery for {operator}."))
            else:
                self.stdout.write(
                    self.style.WARNING(
                        f"No task created for {operator}; disabled, unconfigured, "
                        "at zero capacity, or already queued.",
                    ),
                )
