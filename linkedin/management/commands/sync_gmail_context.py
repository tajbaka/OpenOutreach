from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from gmail.auth import GMAIL_ACCOUNTS, GMAIL_OPERATOR_MAPPING
from gmail.client import GmailClient
from gmail.data_sync import (
    candidate_leads,
    combine_results,
    self_emails_for_client,
    sync_gmail_note_emails,
    sync_gmail_threads,
)
from linkedin.models import WorkflowRun


DEFAULT_OPERATOR_FOR_ACCOUNT = {
    "arian_boundera": "Arian",
    "eddy_boundera": "Athena",
}


class Command(BaseCommand):
    help = (
        "Sync Gmail prospect threads and Gmail-delivered Gemini/Meet notes "
        "into crm.Message and crm.Meeting for followup generation."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--operator",
            help="Operator mapping to use, e.g. Arian, Leili, Athena, Chuka.",
        )
        parser.add_argument(
            "--account",
            choices=sorted(GMAIL_ACCOUNTS),
            help="Gmail account key. Omit with --operator to use that operator's account.",
        )
        parser.add_argument("--campaign", type=int, help="Restrict leads to one campaign id.")
        parser.add_argument("--lead-id", type=int, action="append", default=[])
        parser.add_argument("--limit", type=int, help="Limit candidate leads per account.")
        parser.add_argument("--since-days", type=int, default=365)
        parser.add_argument(
            "--all-leads",
            action="store_true",
            help="Include all non-disqualified leads with email, not only active deal states.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Fetch and match, but do not write Message/Meeting/WorkflowRun rows.",
        )
        parser.add_argument(
            "--skip-threads",
            action="store_true",
            help="Skip normal prospect Gmail thread persistence.",
        )
        parser.add_argument(
            "--skip-notes",
            action="store_true",
            help="Skip Gmail-delivered Gemini/Meet note persistence.",
        )
        parser.add_argument(
            "--no-create-missing-meetings",
            action="store_true",
            help=(
                "Only attach notes to existing crm.Meeting rows. By default, "
                "a note email can create a Meeting only when its title uniquely "
                "identifies one CRM lead."
            ),
        )
        parser.add_argument(
            "--show-unmatched",
            type=int,
            default=10,
            help="Print up to N unmatched note-email subjects.",
        )

    def handle(self, *args, **options):
        if options["skip_threads"] and options["skip_notes"]:
            raise CommandError("Nothing to do: both --skip-threads and --skip-notes were set.")

        account_ops = self._resolve_account_operators(options)
        total = None

        for account_key, operator in account_ops:
            self.stdout.write(f"Account {account_key} via operator {operator}")
            client = GmailClient(operator=operator)
            self_emails = self_emails_for_client(client)
            self.stdout.write("  self emails: " + ", ".join(sorted(self_emails)))

            leads = list(self._lead_queryset(options))
            if options["limit"]:
                leads = leads[: options["limit"]]
            self.stdout.write(f"  candidate leads: {len(leads)}")

            results = []
            if not options["skip_threads"]:
                thread_result = sync_gmail_threads(
                    client=client,
                    leads=leads,
                    self_emails=self_emails,
                    since_days=options["since_days"],
                    dry_run=options["dry_run"],
                )
                results.append(thread_result)
                self.stdout.write(
                    "  Gmail threads: "
                    f"leads_with_threads={thread_result.leads_with_email_threads} "
                    f"threads_fetched={thread_result.gmail_threads_fetched} "
                    f"messages_created={thread_result.gmail_messages_created}"
                )

            if not options["skip_notes"]:
                notes_result = sync_gmail_note_emails(
                    client=client,
                    leads=leads,
                    since_days=options["since_days"],
                    dry_run=options["dry_run"],
                    create_missing_meetings=not options["no_create_missing_meetings"],
                )
                results.append(notes_result)
                self.stdout.write(
                    "  Gemini note emails: "
                    f"seen={notes_result.note_emails_seen} "
                    f"matched={notes_result.note_emails_matched} "
                    f"created_meetings={notes_result.note_emails_created_meetings} "
                    f"updated_meetings={notes_result.note_emails_updated_meetings} "
                    f"unchanged={notes_result.note_emails_unchanged} "
                    f"unmatched={notes_result.note_emails_unmatched}"
                )
                for item in notes_result.unmatched_notes[: options["show_unmatched"]]:
                    self.stdout.write(
                        "    unmatched note: "
                        f"{item['date'][:10]} | {item['subject'][:140]}"
                    )

            account_result = combine_results(*results)
            total = account_result if total is None else combine_results(total, account_result)
            if not options["dry_run"]:
                self._record_workflow_runs(account_key, account_result)

        if total is None:
            return
        self.stdout.write(
            self.style.SUCCESS(
                "Done. "
                f"leads_considered={total.leads_considered} "
                f"gmail_threads_fetched={total.gmail_threads_fetched} "
                f"gmail_messages_created={total.gmail_messages_created} "
                f"note_emails_seen={total.note_emails_seen} "
                f"note_emails_matched={total.note_emails_matched} "
                f"note_emails_unmatched={total.note_emails_unmatched}"
            )
        )

    def _resolve_account_operators(self, options) -> list[tuple[str, str]]:
        if options["operator"]:
            mapping = GMAIL_OPERATOR_MAPPING.get(options["operator"])
            if mapping is None:
                known = ", ".join(sorted(GMAIL_OPERATOR_MAPPING))
                raise CommandError(f"Unknown operator {options['operator']!r}; known: {known}")
            account_key = mapping["gmail_account"]
            if options["account"] and options["account"] != account_key:
                raise CommandError(
                    f"--operator {options['operator']} maps to {account_key}, "
                    f"not --account {options['account']}"
                )
            return [(account_key, options["operator"])]

        if options["account"]:
            return [(options["account"], DEFAULT_OPERATOR_FOR_ACCOUNT[options["account"]])]

        return [
            (account_key, DEFAULT_OPERATOR_FOR_ACCOUNT[account_key])
            for account_key in sorted(GMAIL_ACCOUNTS)
        ]

    def _lead_queryset(self, options):
        qs = candidate_leads(
            campaign_id=options["campaign"],
            all_leads=options["all_leads"],
        )
        if options["lead_id"]:
            qs = qs.filter(id__in=options["lead_id"])
        return qs

    def _record_workflow_runs(self, account_key: str, result) -> None:
        operators = [
            op for op, mapping in GMAIL_OPERATOR_MAPPING.items()
            if mapping["gmail_account"] == account_key
        ]
        counts = {
            "leads_considered": result.leads_considered,
            "gmail_threads_fetched": result.gmail_threads_fetched,
            "gmail_messages_created": result.gmail_messages_created,
            "note_emails_seen": result.note_emails_seen,
            "note_emails_matched": result.note_emails_matched,
            "note_emails_created_meetings": result.note_emails_created_meetings,
            "note_emails_updated_meetings": result.note_emails_updated_meetings,
            "note_emails_unchanged": result.note_emails_unchanged,
            "note_emails_unmatched": result.note_emails_unmatched,
        }
        summary = (
            f"account={account_key} "
            f"gmail_threads_fetched={result.gmail_threads_fetched} "
            f"gmail_messages_created={result.gmail_messages_created} "
            f"note_emails_seen={result.note_emails_seen} "
            f"note_emails_matched={result.note_emails_matched}"
        )
        for operator in operators:
            WorkflowRun.objects.create(
                name="data-sync",
                operator=operator,
                summary=summary,
                counts=counts,
            )
