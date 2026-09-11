"""Request one sender's next supervised worker restart without touching outreach."""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from linkedin.models import LinkedInProfile
from linkedin.operators import CANONICAL_OPERATOR_HANDLES, resolve_operator


def _exact_profile(operator: str) -> LinkedInProfile:
    matches = [
        profile
        for profile in LinkedInProfile.objects.only(
            "id", "linkedin_username", "restart_requested",
        ).order_by("pk")
        if resolve_operator(profile.linkedin_username) == operator
    ]
    if len(matches) != 1:
        raise CommandError(
            f"Expected exactly one LinkedInProfile for {operator} by its LinkedIn username; "
            f"found {len(matches)}. No restart requested."
        )
    return matches[0]


class Command(BaseCommand):
    help = "Preview a one-shot sender worker restart request; --apply sets only its profile flag."

    def add_arguments(self, parser):
        parser.add_argument(
            "--operator",
            required=True,
            help="Canonical sender: Arian, Chuka, Athena, or Leili. Eddy resolves to Chuka.",
        )
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--apply", action="store_true", help="Set the restart request flag.")
        mode.add_argument("--dry-run", action="store_true", help="Preview only (the default).")

    def handle(self, *args, **options):
        operator = resolve_operator(options["operator"])
        if operator not in CANONICAL_OPERATOR_HANDLES:
            raise CommandError("Choose a known operator: Arian, Chuka (Eddy), Athena, or Leili.")

        if not options["apply"]:
            profile = _exact_profile(operator)
            status = "already requested" if profile.restart_requested else "would request restart"
            self.stdout.write(
                f"Dry run: {operator}, LinkedInProfile {profile.pk}: {status}. No changes made."
            )
            return

        with transaction.atomic():
            profile = _exact_profile(operator)
            locked = LinkedInProfile.objects.select_for_update().only(
                "id", "linkedin_username", "restart_requested",
            ).get(pk=profile.pk)
            if resolve_operator(locked.linkedin_username) != operator:
                raise CommandError("The sender profile changed during resolution. No restart requested.")
            if locked.restart_requested:
                message = f"Restart already requested for {operator}, LinkedInProfile {locked.pk}."
            else:
                LinkedInProfile.objects.filter(pk=locked.pk).update(restart_requested=True)
                message = (
                    f"Restart requested for {operator}, LinkedInProfile {locked.pk}; "
                    "the matching supervisor will consume it at its next poll."
                )
        self.stdout.write(message)
