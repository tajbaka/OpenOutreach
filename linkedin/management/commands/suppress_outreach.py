from django.core.management.base import BaseCommand, CommandError

from linkedin.models import OutreachSuppression
from linkedin.suppression import apply_suppression_to_existing_leads


class Command(BaseCommand):
    help = "Create/list/apply outbound outreach suppressions."

    def add_arguments(self, parser):
        parser.add_argument("--kind", choices=[OutreachSuppression.Kind.COMPANY, OutreachSuppression.Kind.LEAD])
        parser.add_argument("--value", help="Company or lead name to suppress.")
        parser.add_argument("--alias", action="append", default=[], help="Alias; may be repeated.")
        parser.add_argument("--domain", default="", help="Company/email domain to match.")
        parser.add_argument("--email", default="", help="Lead email to match.")
        parser.add_argument("--linkedin-url", default="", help="Lead LinkedIn URL to match.")
        parser.add_argument("--public-id", default="", help="Lead LinkedIn public identifier to match.")
        parser.add_argument("--reason", default="", help="Audit reason.")
        parser.add_argument("--inactive", action="store_true", help="Create/update as inactive.")
        parser.add_argument("--apply", action="store_true", help="Re-apply all active suppressions.")
        parser.add_argument("--list", action="store_true", help="List existing suppressions.")

    def handle(self, *args, **options):
        if options["list"]:
            for s in OutreachSuppression.objects.order_by("kind", "value"):
                state = "active" if s.active else "inactive"
                self.stdout.write(f"{s.id}\t{s.kind}\t{state}\t{s.value}\t{s.domain}\t{s.email}\t{s.reason}")
            return

        if options["apply"]:
            totals = {"leads": 0, "deals": 0, "tasks": 0}
            for suppression in OutreachSuppression.objects.filter(active=True):
                counts = apply_suppression_to_existing_leads(suppression)
                for key, value in counts.items():
                    totals[key] += value
            self.stdout.write(
                self.style.SUCCESS(
                    f"Applied active suppressions: {totals['leads']} leads, "
                    f"{totals['deals']} deals, {totals['tasks']} tasks"
                )
            )
            return

        kind = options["kind"]
        value = (options["value"] or "").strip()
        if not kind or not value:
            raise CommandError("Provide --kind and --value, or use --list/--apply.")

        suppression, _created = OutreachSuppression.objects.update_or_create(
            kind=kind,
            value=value,
            defaults={
                "aliases": options["alias"],
                "domain": options["domain"],
                "email": options["email"],
                "linkedin_url": options["linkedin_url"],
                "public_identifier": options["public_id"],
                "reason": options["reason"],
                "active": not options["inactive"],
            },
        )
        counts = apply_suppression_to_existing_leads(suppression)
        self.stdout.write(
            self.style.SUCCESS(
                f"Saved {suppression}. Applied to {counts['leads']} leads, "
                f"{counts['deals']} deals, {counts['tasks']} tasks."
            )
        )
