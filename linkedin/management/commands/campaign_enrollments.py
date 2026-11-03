import json
from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Preview/apply an external lead's enrollment into a normal campaign, or read its status."

    def add_arguments(self, parser):
        parser.add_argument("action", choices=["submit", "status", "preflight", "callbacks", "outcomes", "ack-outcomes"])
        parser.add_argument("--input", type=Path)
        parser.add_argument("--source")
        parser.add_argument("--reference-id")
        parser.add_argument("--operator")
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        from linkedin import conf
        from linkedin.campaign_integrations import EnrollmentHold, status, submit
        from linkedin.message_delivery import MessageDeliveryError
        from linkedin.models import Campaign, CampaignSubmission
        try:
            if options["action"] == "submit":
                if not options["input"]:
                    raise CommandError("--input is required")
                result = submit(json.loads(options["input"].read_text()), apply=options["apply"])
            elif options["action"] == "status":
                if not options["source"] or not options["reference_id"]:
                    raise CommandError("--source and --reference-id are required")
                row = CampaignSubmission.objects.select_related("enrollment__deal__lead").filter(
                    source=options["source"], reference_id=options["reference_id"]).first()
                result = status(row) if row else {"status": "missing"}
            elif options["action"] == "callbacks":
                if not options["operator"] or not options["apply"]:
                    raise CommandError("--operator and --apply are required")
                from linkedin.campaign_callbacks import deliver, reconcile
                result = {"prepared": reconcile(operator=options["operator"]), "delivered": deliver(operator=options["operator"])}
            elif options["action"] in {"outcomes", "ack-outcomes"}:
                if not options["operator"] or not options["source"] or not options["apply"]:
                    raise CommandError("--operator, --source and --apply are required")
                from linkedin.campaign_callbacks import local_outcomes, acknowledge_local_outcomes
                if options["action"] == "outcomes":
                    result = {"events": local_outcomes(operator=options["operator"], source=options["source"])}
                else:
                    if not options["input"]:
                        raise CommandError("--input is required")
                    result = {"acknowledged": acknowledge_local_outcomes(
                        operator=options["operator"], source=options["source"],
                        receipts=json.loads(options["input"].read_text()))}
            else:
                result = {"enabled": conf.ENABLE_CAMPAIGN_INTEGRATIONS,
                          "local_outcomes_supported": True,
                          "message_version_hash_supported": True,
                          "connect_outside_hours_campaign_ids": list(Campaign.objects.filter(
                              allow_connections_outside_hours=True,
                          ).order_by("pk").values_list("pk", flat=True)),
                          "callbacks_configured": bool(len(conf.OUTREACH_CALLBACK_SECRET) >= 32 and conf.OUTREACH_CALLBACK_ALLOWED_HOSTS),
                          "replies_configured": bool(conf.ENABLE_REALTIME_LISTENER and conf.SLACK_REPLIES_WEBHOOK_URL)}
        except EnrollmentHold as exc:
            result = {"status": "held", "reason": str(exc)}
        except MessageDeliveryError:
            result = {"status": "held", "reason": "campaign_message_program_invalid"}
        except (ValueError, ValidationError) as exc:
            raise CommandError("Invalid enrollment input: " + type(exc).__name__) from exc
        self.stdout.write(json.dumps({"applied": options["apply"], "result": result}, default=str))
