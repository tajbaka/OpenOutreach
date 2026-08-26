"""Canonical implementation behind ``manage.py generate_followups``."""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import CommandError


def run_canonical_followup_command(command, opts) -> None:
    limit = opts.get("limit")
    if limit is not None and limit <= 0:
        raise CommandError("--limit must be positive.")
    if opts.get("output") and opts.get("apply_json"):
        raise CommandError("Choose either --output or --apply-json, not both.")

    incompatible = [
        flag
        for flag, enabled in (
            ("--campaign", opts.get("campaign") is not None),
            ("--no-active", opts.get("no_active")),
            ("--no-sheet-read", opts.get("no_sheet_read")),
            ("--full-review", opts.get("full_review")),
        )
        if enabled
    ]
    if incompatible:
        raise CommandError(
            f"{', '.join(incompatible)} is legacy-only; add --legacy or remove it."
        )

    # Old convenience flags route through the one canonical orchestrator so
    # they cannot reintroduce a second eligibility path.
    if (
        opts.get("refresh_crm")
        or opts.get("sync_gmail_context")
        or opts.get("sync_sheets")
    ):
        call_command("refresh_crm", apply=True)

    queue = _filter_queue(
        _canonical_queue(),
        operators=opts.get("operator") or (),
        limit=limit,
    )
    if opts.get("apply_json"):
        from linkedin.crm_followup_decisions import (
            CrmFollowupDecisionError,
            apply_crm_followup_decisions,
            load_crm_followup_decisions,
        )

        try:
            decisions = load_crm_followup_decisions(opts["apply_json"])
            result = apply_crm_followup_decisions(
                decisions,
                canonical_queue=queue,
                record_workflow=not opts["no_record_workflow"],
            )
        except CrmFollowupDecisionError as exc:
            raise CommandError(str(exc)) from exc
        command.stdout.write(
            command.style.SUCCESS(
                "Validated canonical follow-up decisions: "
                + json.dumps(result.counts(), sort_keys=True)
            )
        )
        if result.drafts_applied and not opts.get("no_publish"):
            call_command(
                "refresh_crm",
                apply=True,
                skip_gmail_context=True,
                skip_granola=True,
                skip_people=True,
            )
        return

    if opts.get("output"):
        from linkedin.followup_analysis import write_review_queue

        path = Path(opts["output"])
        write_review_queue(path, queue)
        command.stdout.write(
            f"Wrote canonical Codex follow-up queue to {path} "
            f"({queue['candidate_count']} candidates)."
        )
    else:
        command.stdout.write(
            json.dumps(queue, indent=2, ensure_ascii=False, default=str)
        )


def _canonical_queue() -> dict[str, object]:
    from crm.models import MeetingNote, MeetingNoteSyncState
    from linkedin.conf import GOOGLE_SHEETS_ID
    from linkedin.crm_followup_analysis import serialize_crm_followup_queue
    from linkedin.crm_sheet_import import read_people_dont_send_lead_ids
    from linkedin.management.commands.refresh_crm import _verify_workbook_identity
    from linkedin.notifications import sheets

    spreadsheet = sheets._gspread_client()
    _verify_workbook_identity(spreadsheet, configured_id=GOOGLE_SHEETS_ID)
    dont_send_ids = read_people_dont_send_lead_ids(spreadsheet)
    state = MeetingNoteSyncState.objects.filter(
        source=MeetingNote.Source.GRANOLA,
    ).first()
    granola_available = bool(
        state is not None
        and state.status in {
            MeetingNoteSyncState.Status.SUCCESS,
            MeetingNoteSyncState.Status.PARTIAL,
        }
    )
    return serialize_crm_followup_queue(
        dont_send_lead_ids=dont_send_ids,
        granola_available=granola_available,
    )


def _filter_queue(
    queue: dict[str, object],
    *,
    operators,
    limit: int | None,
) -> dict[str, object]:
    from linkedin.operators import resolve_sales_owner_handle

    handles = set()
    for raw in operators:
        handle = resolve_sales_owner_handle(raw)
        if not handle:
            raise CommandError(f"Unknown canonical sales owner: {raw!r}.")
        handles.add(handle)
    candidates = list(queue.get("candidates") or [])
    if handles:
        candidates = [
            row
            for row in candidates
            if row.get("owner", {}).get("handle") in handles
        ]
    if limit is not None:
        candidates = candidates[:limit]
    counts = Counter(row["owner"]["handle"] for row in candidates)
    return {
        **queue,
        "candidate_count": len(candidates),
        "counts_by_owner": dict(sorted(counts.items())),
        "candidates": candidates,
    }
