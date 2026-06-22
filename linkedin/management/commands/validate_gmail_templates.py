"""Validate Gmail post-accept templates without sending email."""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Validate Gmail ICP templates for allowed placeholders and render safety."

    def handle(self, *args, **options):
        from gmail.templates import validate_all_templates
        from linkedin.exceptions import SheetsError

        try:
            result = validate_all_templates()
        except SheetsError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(self.style.SUCCESS(
            "Validated "
            f"{result.enabled_steps} Gmail template step(s); "
            f"{result.disabled_blocks} disabled block(s)."
        ))
