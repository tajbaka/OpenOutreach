import json
from pathlib import Path

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

    def handle(self, *args, **options):
        from linkedin.general_icp_json import load_general_message_programs
        from linkedin.general_icp_messages import publish_message_programs
        from linkedin.models import Campaign, LinkedInProfile, MessageProgramVersion

        path = Path(options["json_path"])
        if not path.exists():
            raise CommandError(f"File not found: {path}")

        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CommandError(f"Invalid JSON in {path}: {exc}") from exc

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
        program_key = str(payload.get("message_program_key") or "").strip()
        with transaction.atomic():
            if program_key:
                drafts = {draft.key: draft for draft in load_general_message_programs()}
                draft = drafts.get(program_key)
                if draft is None:
                    raise CommandError(
                        f"Unknown message_program_key {program_key!r}; "
                        f"available: {sorted(drafts)!r}"
                    )
                (decision,) = publish_message_programs(
                    (draft,),
                    published_by=f"campaign-import:{name}"[:150],
                )
                defaults["active_message_version"] = MessageProgramVersion.objects.get(
                    program__key=program_key,
                    version=decision.target_version,
                )

            if not Campaign.objects.filter(name=name).exists():
                owner_profile = (
                    LinkedInProfile.objects.filter(active=True)
                    .select_related("user")
                    .order_by("pk")
                    .first()
                )
                if owner_profile is None:
                    raise CommandError(
                        "Cannot create a Campaign because no active LinkedInProfile exists "
                        "to own it."
                    )
                defaults["user"] = owner_profile.user
            campaign, created = Campaign.objects.update_or_create(name=name, defaults=defaults)
        action = "Created" if created else "Updated"
        self.stdout.write(
            self.style.SUCCESS(
                f"{action} campaign '{campaign.name}' (id={campaign.pk})"
            )
        )
