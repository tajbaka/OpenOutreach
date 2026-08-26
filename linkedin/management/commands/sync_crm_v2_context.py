"""Refresh the primary Gmail/Gemini context used by the account-first CRM.

This command is deliberately separate from Sheet publication.  It never sends
mail.  Apply mode refreshes stored Gmail threads/meeting-note emails, creates
only strictly validated corporate email-first Leads from private discovery
state, then re-reads Gmail once only when new Leads need their exact threads
linked.  Routine output is aggregate-only.
"""
from __future__ import annotations

import io
import json

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from gmail.auth import GMAIL_ACCOUNTS
from linkedin.crm_v2_email_first import (
    apply_email_first_leads,
    dry_run_email_first_leads,
)
from linkedin.exceptions import EnrichmentError
from linkedin.management.commands.sync_gmail_context import (
    Command as GmailContextCommand,
)


class Command(BaseCommand):
    help = (
        "Refresh Gmail/Gemini context and strictly reconcile email-first "
        "contacts for CRM v2. Defaults to no-write dry-run; never sends."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Persist context and safe email-first Leads.",
        )
        parser.add_argument(
            "--since-days",
            type=int,
            default=365,
            help="Known-thread and Gemini-note lookback (default: 365).",
        )
        parser.add_argument(
            "--skip-gmail-refresh",
            action="store_true",
            help="Use existing private discovery state and stored DB context.",
        )
        parser.add_argument(
            "--skip-granola",
            action="store_true",
            help="Retain stored Granola/Gemini meeting context without API refresh.",
        )
        parser.add_argument(
            "--granola-max-notes",
            type=int,
            default=None,
            help="Optional Granola detail-request budget.",
        )

    def handle(self, *args, **options):
        if options["since_days"] <= 0:
            raise CommandError("--since-days must be positive")
        if (
            options["granola_max_notes"] is not None
            and options["granola_max_notes"] <= 0
        ):
            raise CommandError("--granola-max-notes must be positive")
        apply = bool(options["apply"])
        report = {
            "mode": "apply" if apply else "dry-run",
            "gmail": {"status": "skipped"},
            "email_first": {},
            "gmail_relink": {"status": "not_needed"},
            "granola": {"status": "skipped"},
        }

        if not options["skip_gmail_refresh"]:
            report["gmail"] = _run_gmail_context(
                apply=apply,
                since_days=options["since_days"],
                skip_unmapped_discovery=False,
                skip_notes=False,
            )

        candidates = _private_discovery_candidates()
        observed_at = timezone.now()
        email_first = (
            apply_email_first_leads(candidates, evaluated_at=observed_at)
            if apply
            else dry_run_email_first_leads(candidates, evaluated_at=observed_at)
        )
        report["email_first"] = {
            "input_candidates": email_first.input_candidates,
            "counts": email_first.counts(),
            "issue_counts": email_first.issue_counts(),
        }

        if apply and email_first.created_lead_ids:
            report["gmail_relink"] = _run_gmail_context(
                apply=True,
                since_days=options["since_days"],
                skip_unmapped_discovery=True,
                skip_notes=True,
            )

        if not options["skip_granola"]:
            report["granola"] = _sync_granola(
                apply=apply,
                max_notes=options["granola_max_notes"],
                now=observed_at,
            )

        self.stdout.write(json.dumps(report, sort_keys=True))


def _run_gmail_context(
    *,
    apply: bool,
    since_days: int,
    skip_unmapped_discovery: bool,
    skip_notes: bool,
) -> dict[str, object]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        call_command(
            "sync_gmail_context",
            dry_run=not apply,
            since_days=since_days,
            skip_unmapped_discovery=skip_unmapped_discovery,
            skip_notes=skip_notes,
            stdout=stdout,
            stderr=stderr,
        )
    except EnrichmentError:
        return {"status": "unavailable", "warnings": 1}
    if apply and not skip_notes:
        _mark_gemini_scan_success()
    return {
        "status": "refreshed" if apply else "checked_no_write",
        "stdout_lines_suppressed": len(stdout.getvalue().splitlines()),
        "warning_lines_suppressed": len(stderr.getvalue().splitlines()),
    }


def _mark_gemini_scan_success() -> None:
    """Record a completed Gemini-note mailbox scan for freshness checks."""
    from crm.models import MeetingNote, MeetingNoteSyncState

    observed_at = timezone.now()
    MeetingNoteSyncState.objects.update_or_create(
        source=MeetingNote.Source.GEMINI,
        defaults={
            "last_attempt_at": observed_at,
            "last_success_at": observed_at,
            "status": MeetingNoteSyncState.Status.SUCCESS,
            "last_error_kind": "",
            "last_error_message": "",
        },
    )


def _private_discovery_candidates() -> list[dict]:
    candidates: list[dict] = []
    for account_key in sorted(GMAIL_ACCOUNTS):
        state = GmailContextCommand._gmail_sync_state(account_key)
        rows = state.get("gmail_unmapped_external_participants", ())
        if not isinstance(rows, list):
            raise CommandError(
                "Private Gmail discovery state has an invalid candidate list"
            )
        candidates.extend(rows)
    return candidates


def _sync_granola(*, apply: bool, max_notes: int | None, now) -> dict[str, object]:
    from crm.models import Opportunity
    from linkedin import conf
    from linkedin.granola import build_granola_client
    from linkedin.granola_sync import sync_granola_meeting_notes

    setup = build_granola_client(
        api_key=conf.GRANOLA_API_KEY,
        base_url=conf.GRANOLA_API_BASE,
        timeout=conf.GRANOLA_HTTP_TIMEOUT_SECONDS,
    )
    active_ids = Opportunity.objects.exclude(
        stage__in=(
            Opportunity.Stage.CLOSED_WON,
            Opportunity.Stage.CLOSED_LOST,
        ),
    ).values_list("id", flat=True)
    result = sync_granola_meeting_notes(
        client=setup.client,
        client_error=setup.error,
        now=now,
        max_notes=max_notes,
        active_opportunity_ids=active_ids,
        dry_run=not apply,
    )
    counts = dict(result.counts())
    counts["raw_warnings_suppressed"] = len(result.warnings)
    return counts
