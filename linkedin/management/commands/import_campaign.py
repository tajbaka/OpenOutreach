import json
from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


class Command(BaseCommand):
    help = "Import a campaign definition from JSON."

    def add_arguments(self, parser):
        parser.add_argument(
            "json_path",
            help="Path to a campaign JSON file created by export_campaign.",
        )
        parser.add_argument(
            "--name",
            help="Override the imported campaign name.",
        )
        parser.add_argument("--owner-username", help="Exact sender's User.username; required for new campaigns.")
        parser.add_argument(
            "--gmail-start-mode", choices=("post_acceptance", "invitation_sent"),
            help="Explicit choice: post-acceptance or pre-acceptance email. Required for new campaigns.",
        )
        parser.add_argument("--dry-run", action="store_true", help="Preview owner, email timing and message program without writes.")

    def handle(self, *args, **options):
        from linkedin.campaign_setup import (
            exact_campaign_owner, explicit_gmail_start_mode,
            message_program_draft, snapshot_campaign_program,
        )
        from linkedin.models import Campaign

        path = Path(options["json_path"])
        if not path.exists():
            raise CommandError(f"File not found: {path}")

        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CommandError(f"Invalid JSON in {path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise CommandError("Campaign JSON must be an object.")

        name = (options.get("name") or payload.get("name") or "").strip()
        if not name:
            raise CommandError("Campaign JSON must include a non-empty 'name'.")

        defaults = {
            "product_docs": payload.get("product_docs", ""),
            "campaign_objective": payload.get("campaign_objective", ""),
            "booking_link": payload.get("booking_link", ""),
            "is_freemium": bool(payload.get("is_freemium", False)),
            "action_fraction": payload.get("action_fraction", 0.2),
            "seed_public_ids": payload.get("seed_public_ids") or [],
        }
        if "status" in payload:
            defaults["status"] = payload["status"]
        program_key = str(payload.get("message_program_key") or "").strip()
        mode = options.get("gmail_start_mode") or payload.get("gmail_start_mode")
        owner_username = options.get("owner_username") or payload.get("owner_username")
        try:
            with transaction.atomic():
                campaign = Campaign.objects.select_for_update().filter(name=name).first()
                created = campaign is None
                if created or "gmail_start_mode" in payload or options.get("gmail_start_mode") is not None:
                    defaults["gmail_start_mode"] = explicit_gmail_start_mode(mode)
                if created or "owner_username" in payload or options.get("owner_username") is not None:
                    defaults["user"] = exact_campaign_owner(owner_username)
                if created:
                    campaign = Campaign(name=name)
                for field, value in defaults.items():
                    setattr(campaign, field, value)
                if created and campaign.gmail_start_mode == Campaign.GmailStartMode.INVITATION_SENT and "status" not in payload:
                    campaign.status = Campaign.Status.DISABLED
                draft = message_program_draft(program_key) if program_key else None
                if created and campaign.gmail_start_mode == Campaign.GmailStartMode.INVITATION_SENT and draft is None:
                    raise ValidationError("Pre-acceptance email requires an explicit message_program_key for a new campaign.")
                # Validate changes before publishing a new immutable version.
                if not created:
                    campaign.clean()
                # The draft is not published during preview. Field/unique validation
                # plus the explicit pending-draft requirement above validates a new
                # campaign without fabricating a MessageProgramVersion record.
                campaign.clean_fields()
                campaign.validate_unique()
                self.stdout.write(
                    f"Campaign preview: {name}; owner_username={campaign.user.username}; "
                    f"status={campaign.status}; "
                    f"gmail_start_mode={campaign.gmail_start_mode} ({campaign.get_gmail_start_mode_display()}); "
                    f"message_program_key={program_key or '(unchanged/legacy)'}"
                )
                if options["dry_run"]:
                    self.stdout.write("Dry run: no campaign, enrollment, message version or Task written.")
                    return
                if draft is not None:
                    campaign.active_message_version = snapshot_campaign_program(draft, campaign_name=name)
                campaign.full_clean()
                campaign.save()
        except ValidationError as exc:
            raise CommandError("; ".join(exc.messages)) from exc
        action = "Created" if created else "Updated"
        self.stdout.write(
            self.style.SUCCESS(
                f"{action} campaign '{campaign.name}' (id={campaign.pk})"
            )
        )
