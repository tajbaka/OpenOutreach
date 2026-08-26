from __future__ import annotations

import logging
import json
import os
import tempfile
from contextlib import contextmanager

from google.auth.exceptions import GoogleAuthError
from googleapiclient.errors import HttpError
from httplib2 import HttpLib2Error
from django.core.management.base import BaseCommand, CommandError

from gmail.auth import GMAIL_ACCOUNTS, GMAIL_DATA_DIR, GMAIL_OPERATOR_MAPPING
from gmail.client import GmailClient
from gmail.data_sync import (
    candidate_leads,
    combine_results,
    self_emails_for_client,
    sync_gmail_note_emails,
    sync_gmail_threads,
)
from linkedin.exceptions import EnrichmentError
from linkedin.models import WorkflowRun
from linkedin.operators import resolve_sales_owner_handle


DEFAULT_OPERATOR_FOR_ACCOUNT = {
    "arian_boundera": "Arian",
    "eddy_boundera": "Athena",
}


_RECOVERABLE_GMAIL_SYNC_ERRORS = (
    EnrichmentError,
    GoogleAuthError,
    HttpError,
    HttpLib2Error,
    TimeoutError,
    ConnectionError,
)
_SAFE_SYNC_ERROR = "Gmail context sync is temporarily unavailable."


@contextmanager
def _suppress_google_api_request_logging():
    """Prevent Gmail query URLs and response bodies from reaching logs."""
    loggers = [
        logging.getLogger("googleapiclient.discovery"),
        logging.getLogger("googleapiclient.http"),
    ]
    previous_levels = [logger.level for logger in loggers]
    for logger in loggers:
        logger.setLevel(logging.CRITICAL)
    try:
        yield
    finally:
        for logger, previous_level in zip(loggers, previous_levels):
            logger.setLevel(previous_level)


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
            help=(
                "Deprecated no-op. Every email-bearing Lead is now considered; "
                "Deal state and do-not-outreach status never gate Gmail context."
            ),
        )
        parser.add_argument(
            "--skip-unmapped-discovery",
            action="store_true",
            help="Skip the bounded recent-mailbox scan for email-first relationships.",
        )
        parser.add_argument(
            "--discovery-since-days",
            type=int,
            default=90,
            help="Recent window for unmapped human-participant discovery (default: 90).",
        )
        parser.add_argument(
            "--discovery-max-messages",
            type=int,
            default=500,
            help="Maximum recent search hits inspected per mailbox (default: 500).",
        )
        parser.add_argument(
            "--discovery-max-threads",
            type=int,
            default=500,
            help="Maximum recent unique thread candidates inspected (default: 500).",
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
            default=0,
            help="Print up to N sanitized unmatched note dates/reasons (default: 0).",
        )

    def handle(self, *args, **options):
        with _suppress_google_api_request_logging():
            try:
                return self._sync_context(options)
            except _RECOVERABLE_GMAIL_SYNC_ERRORS:
                # Gmail API errors can contain request URLs, mailbox search
                # terms, or credential-provider detail. Give refresh_crm the
                # typed signal it treats as a stored-context fallback.
                raise EnrichmentError(_SAFE_SYNC_ERROR) from None

    def _sync_context(self, options):
        self._validate_options(options)
        if options["skip_threads"] and options["skip_notes"]:
            raise CommandError("Nothing to do: both --skip-threads and --skip-notes were set.")

        account_ops = self._resolve_account_operators(options)
        total = None

        for account_key, operator in account_ops:
            self.stdout.write(f"Account {account_key} via operator {operator}")
            client = GmailClient(operator=operator)
            self_emails = self_emails_for_client(client)
            self.stdout.write(f"  mailbox identities resolved: {len(self_emails)}")

            leads = list(self._lead_queryset(options))
            if options["limit"] is not None:
                leads = leads[: options["limit"]]
            self.stdout.write(f"  candidate leads: {len(leads)}")

            results = []
            if not options["skip_threads"]:
                discover_unmapped = (
                    not options["skip_unmapped_discovery"]
                    and options["campaign"] is None
                    and not options["lead_id"]
                    and options["limit"] is None
                )
                prior_state = (
                    self._gmail_sync_state(account_key)
                    if not options["dry_run"] and discover_unmapped
                    else {}
                )
                thread_result = sync_gmail_threads(
                    client=client,
                    leads=leads,
                    self_emails=self_emails,
                    since_days=options["since_days"],
                    dry_run=options["dry_run"],
                    discover_unmapped=discover_unmapped,
                    discovery_since_days=options["discovery_since_days"],
                    discovery_max_messages=options["discovery_max_messages"],
                    discovery_max_threads=options["discovery_max_threads"],
                    operator_by_self_email=self._operator_by_self_email(
                        account_key
                    ),
                    processed_thread_versions=prior_state.get(
                        "gmail_processed_thread_versions", {}
                    ),
                    prior_unmapped_external_participants=prior_state.get(
                        "gmail_unmapped_external_participants", []
                    ),
                )
                results.append(thread_result)
                self.stdout.write(
                    "  Gmail threads: "
                    f"leads_with_threads={thread_result.leads_with_email_threads} "
                    f"search_queries={thread_result.gmail_search_queries} "
                    f"search_caps={thread_result.gmail_search_queries_at_cap} "
                    f"search_splits={thread_result.gmail_search_batches_split} "
                    f"threads_fetched={thread_result.gmail_threads_fetched} "
                    f"threads_matched={thread_result.gmail_threads_matched} "
                    f"ambiguous={thread_result.gmail_threads_ambiguous} "
                    f"deferred={thread_result.gmail_threads_deferred} "
                    f"automated_skipped={thread_result.gmail_automated_messages_skipped} "
                    f"unsent_skipped={thread_result.gmail_unsent_messages_skipped} "
                    f"human_inbound={thread_result.gmail_human_inbound_messages} "
                    f"messages_created={thread_result.gmail_messages_created}"
                )
                if discover_unmapped:
                    self.stdout.write(
                        "  Email-first discovery: "
                        f"messages_scanned={thread_result.discovery_messages_scanned} "
                        f"threads_selected={thread_result.discovery_threads_selected} "
                        "bidirectional_unmapped_participants="
                        f"{len(thread_result.unmapped_external_participants)}"
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
                        f"{item['date'][:10]} | {item['reason']}"
                    )

            account_result = combine_results(*results)
            total = account_result if total is None else combine_results(total, account_result)
            if not options["dry_run"]:
                if not options["skip_threads"] and discover_unmapped:
                    self._write_gmail_sync_state(account_key, account_result)
                self._record_workflow_runs(account_key, account_result)

        if total is None:
            return
        self.stdout.write(
            self.style.SUCCESS(
                "Done. "
                f"leads_considered={total.leads_considered} "
                f"gmail_threads_fetched={total.gmail_threads_fetched} "
                f"gmail_threads_matched={total.gmail_threads_matched} "
                f"gmail_messages_created={total.gmail_messages_created} "
                "unmapped_external_participants="
                f"{len(total.unmapped_external_participants)} "
                f"note_emails_seen={total.note_emails_seen} "
                f"note_emails_matched={total.note_emails_matched} "
                f"note_emails_unmatched={total.note_emails_unmatched}"
            )
        )

    @staticmethod
    def _validate_options(options) -> None:
        for name in (
            "since_days",
            "discovery_since_days",
            "discovery_max_messages",
            "discovery_max_threads",
        ):
            if options[name] <= 0:
                raise CommandError(f"--{name.replace('_', '-')} must be positive.")
        if options["limit"] is not None and options["limit"] <= 0:
            raise CommandError("--limit must be positive.")
        if options["show_unmatched"] < 0:
            raise CommandError("--show-unmatched cannot be negative.")

    @staticmethod
    def _gmail_sync_state(account_key: str) -> dict:
        path = GMAIL_DATA_DIR / f"{account_key}-context-state.json"
        try:
            with path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, TypeError) as exc:
            raise CommandError(
                f"Private Gmail context state is invalid for {account_key}."
            ) from exc
        if not isinstance(payload, dict) or payload.get("schema") != 1:
            raise CommandError(
                f"Private Gmail context state is invalid for {account_key}."
            )
        return payload

    @staticmethod
    def _write_gmail_sync_state(account_key: str, result) -> None:
        GMAIL_DATA_DIR.mkdir(parents=True, exist_ok=True)
        path = GMAIL_DATA_DIR / f"{account_key}-context-state.json"
        payload = {
            "schema": 1,
            "account_key": account_key,
            "gmail_processed_thread_versions": (
                result.gmail_processed_thread_versions
            ),
            "gmail_unmapped_external_participants": (
                result.unmapped_external_participants
            ),
        }
        descriptor, temporary_path = tempfile.mkstemp(
            prefix=f".{account_key}-context-state-",
            suffix=".tmp",
            dir=GMAIL_DATA_DIR,
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.replace(temporary_path, path)
            os.chmod(path, 0o600)
        finally:
            if os.path.exists(temporary_path):
                os.unlink(temporary_path)

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
        qs = candidate_leads(campaign_id=options["campaign"])
        if options["lead_id"]:
            qs = qs.filter(id__in=options["lead_id"])
        return qs

    @staticmethod
    def _operator_by_self_email(account_key: str) -> dict[str, str]:
        operators = {}
        for operator, mapping in GMAIL_OPERATOR_MAPPING.items():
            if mapping["gmail_account"] != account_key:
                continue
            owner = resolve_sales_owner_handle(operator)
            if owner:
                operators[mapping["send_as"].strip().lower()] = owner
        return operators

    def _record_workflow_runs(self, account_key: str, result) -> None:
        operators = [
            op for op, mapping in GMAIL_OPERATOR_MAPPING.items()
            if mapping["gmail_account"] == account_key
        ]
        counts = {
            "leads_considered": result.leads_considered,
            "leads_with_email_threads": result.leads_with_email_threads,
            "gmail_search_queries": result.gmail_search_queries,
            "gmail_search_queries_at_cap": result.gmail_search_queries_at_cap,
            "gmail_search_batches_split": result.gmail_search_batches_split,
            "gmail_search_messages_seen": result.gmail_search_messages_seen,
            "gmail_threads_fetched": result.gmail_threads_fetched,
            "gmail_threads_matched": result.gmail_threads_matched,
            "gmail_threads_ambiguous": result.gmail_threads_ambiguous,
            "gmail_threads_deferred": result.gmail_threads_deferred,
            "gmail_automated_messages_skipped": result.gmail_automated_messages_skipped,
            "gmail_unsent_messages_skipped": result.gmail_unsent_messages_skipped,
            "gmail_human_inbound_messages": result.gmail_human_inbound_messages,
            "gmail_messages_created": result.gmail_messages_created,
            "discovery_messages_scanned": result.discovery_messages_scanned,
            "discovery_threads_selected": result.discovery_threads_selected,
            "unmapped_external_participants": len(
                result.unmapped_external_participants
            ),
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
            f"gmail_threads_matched={result.gmail_threads_matched} "
            f"gmail_messages_created={result.gmail_messages_created} "
            "unmapped_external_participants="
            f"{len(result.unmapped_external_participants)} "
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
