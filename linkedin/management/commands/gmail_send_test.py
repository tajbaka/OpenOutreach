from __future__ import annotations

from django.core.management.base import BaseCommand

from gmail.client import GmailClient


class Command(BaseCommand):
    help = "Send a one-off Gmail API test message using a configured operator mapping."

    def add_arguments(self, parser):
        parser.add_argument("--operator", required=True, help="Operator handle, e.g. Arian.")
        parser.add_argument("--to", required=True, help="Recipient email address.")
        parser.add_argument("--subject", default="OpenOutreach Gmail test")
        parser.add_argument(
            "--body",
            default="This is a Gmail API test send from OpenOutreach.",
        )

    def handle(self, *args, **options):
        client = GmailClient(operator=options["operator"])
        send_result = client.send_message(
            to=options["to"],
            subject=options["subject"],
            body=options["body"],
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Sent Gmail test message id={send_result.message_id} "
                f"thread={send_result.thread_id} from={client.send_as} "
                f"to={options['to']}"
            )
        )
