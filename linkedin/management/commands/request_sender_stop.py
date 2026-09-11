"""Latch or explicitly clear one sender's emergency supervisor stop."""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from linkedin.models import LinkedInProfile
from linkedin.operators import CANONICAL_OPERATOR_HANDLES, resolve_operator


def _exact_profile(operator: str) -> LinkedInProfile:
    matches = [
        profile
        for profile in LinkedInProfile.objects.only(
            "id", "linkedin_username", "restart_requested", "stop_requested",
        ).order_by("pk")
        if resolve_operator(profile.linkedin_username) == operator
    ]
    if len(matches) != 1:
        raise CommandError(
            f"Expected exactly one LinkedInProfile for {operator} by its LinkedIn username; "
            f"found {len(matches)}. No stop flag changed."
        )
    return matches[0]


class Command(BaseCommand):
    help = "Preview a persistent sender emergency stop; --apply arms it, --clear --apply clears it."

    def add_arguments(self, parser):
        parser.add_argument(
            "--operator", required=True,
            help="Canonical sender: Arian, Chuka, Athena, or Leili. Eddy resolves to Chuka.",
        )
        parser.add_argument(
            "--clear", action="store_true",
            help="Explicitly clear only the stop latch; this does not launch workers.",
        )
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--apply", action="store_true", help="Apply the selected stop-latch change.")
        mode.add_argument("--dry-run", action="store_true", help="Preview only (the default).")

    def handle(self, *args, **options):
        operator = resolve_operator(options["operator"])
        if operator not in CANONICAL_OPERATOR_HANDLES:
            raise CommandError("Choose a known operator: Arian, Chuka (Eddy), Athena, or Leili.")

        if not options["apply"]:
            profile = _exact_profile(operator)
            action = "clear the stop latch only" if options["clear"] else "latch stop and cancel any restart request"
            self.stdout.write(
                f"Dry run: {operator}, LinkedInProfile {profile.pk}: would {action}; "
                f"stop_requested={profile.stop_requested}. No changes made."
            )
            return

        with transaction.atomic():
            profile = _exact_profile(operator)
            locked = LinkedInProfile.objects.select_for_update().only(
                "id", "linkedin_username", "restart_requested", "stop_requested",
            ).get(pk=profile.pk)
            if resolve_operator(locked.linkedin_username) != operator:
                raise CommandError("The sender profile changed during resolution. No stop flag changed.")
            if options["clear"]:
                LinkedInProfile.objects.filter(pk=locked.pk).update(stop_requested=False)
                message = (
                    f"Emergency stop cleared for {operator}, LinkedInProfile {locked.pk}. "
                    "No workers started and no restart requested."
                )
            else:
                LinkedInProfile.objects.filter(pk=locked.pk).update(
                    stop_requested=True, restart_requested=False,
                )
                message = (
                    f"Emergency stop latched for {operator}, LinkedInProfile {locked.pk}; "
                    "pending restart cancelled. The matching supervisor must observe it "
                    "before shutdown is confirmed."
                )
        self.stdout.write(message)
