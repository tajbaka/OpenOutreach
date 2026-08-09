"""Post one FedRAMP Marketplace listener run status to the ops Slack channel."""
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Post a FedRAMP Marketplace listener run summary to SLACK_WEBHOOK_URL."

    def add_arguments(self, parser):
        parser.add_argument(
            "--status",
            required=True,
            choices=("success", "empty", "failed"),
        )
        parser.add_argument("--new-source-entries", type=int, default=0)
        parser.add_argument("--target-transitions", type=int, default=0)
        parser.add_argument("--reviewed-decisions", type=int, default=0)
        parser.add_argument("--slack-alerts", type=int, default=0)
        parser.add_argument("--detail", default="")

    def handle(self, *args, **options):
        from linkedin.notifications.slack import notify_marketplace_listener_status

        count_names = (
            "new_source_entries",
            "target_transitions",
            "reviewed_decisions",
            "slack_alerts",
        )
        for name in count_names:
            if options[name] < 0:
                raise CommandError(f"--{name.replace('_', '-')} cannot be negative")
        sent = notify_marketplace_listener_status(
            status=options["status"],
            new_source_entries=options["new_source_entries"],
            target_transitions=options["target_transitions"],
            reviewed_decisions=options["reviewed_decisions"],
            slack_alerts=options["slack_alerts"],
            detail=options["detail"],
        )
        self.stdout.write(
            self.style.SUCCESS("Marketplace status Slack post sent.")
            if sent
            else self.style.WARNING("Marketplace status Slack post was not sent.")
        )
