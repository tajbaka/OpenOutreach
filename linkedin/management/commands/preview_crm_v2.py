"""Build a private, read-only preview of the concise account-first CRM."""
from __future__ import annotations

import json
import os
from collections import Counter
from dataclasses import asdict
from datetime import datetime
from enum import Enum
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from linkedin import conf
from linkedin.crm_v2_evidence import collect_account_evidence


_IGNORED_SALES_MOTION_TABS = frozenset({
    "archive",
    "instructions",
    "template",
})


class Command(BaseCommand):
    help = "Write a no-mutation CRM v2 active-account preview to private JSON."

    def add_arguments(self, parser):
        parser.add_argument("--output", default="")
        parser.add_argument(
            "--sales-motion-account",
            action="append",
            default=[],
            help="Explicit Sales Motion account name; repeatable.",
        )
        parser.add_argument(
            "--manual-pin",
            action="append",
            default=[],
            help="Explicit human account pin for this preview; repeatable.",
        )
        parser.add_argument(
            "--owner-override",
            action="append",
            default=[],
            metavar="ACCOUNT=OWNER",
            help="Assign an exact owner to one account in the preview; repeatable.",
        )
        parser.add_argument(
            "--skip-sales-motion",
            action="store_true",
            help="Do not read account names from the configured Sales Motion workbook.",
        )

    def handle(self, *args, **options):
        generated_at = timezone.now()
        sales_motion_accounts = set(options["sales_motion_account"])
        if not options["skip_sales_motion"]:
            sales_motion_accounts.update(_configured_sales_motion_accounts())

        owner_overrides = _parse_owner_overrides(options["owner_override"])
        dont_send_lead_ids = _configured_people_dont_send_lead_ids()
        rows = collect_account_evidence(
            sales_motion_accounts=sorted(sales_motion_accounts),
            manual_account_pins=options["manual_pin"],
            owner_overrides=owner_overrides,
            dont_send_lead_ids=dont_send_lead_ids,
            now=generated_at,
        )
        active = [row for row in rows if row.decision.admitted]
        active.sort(key=_active_sort_key)
        payload = {
            "schema": "openoutreach.crm-v2-preview.v1",
            "generated_at": generated_at.isoformat(),
            "summary": {
                "account_groups_evaluated": len(rows),
                "active_accounts": len(active),
                "people_only_accounts": len(rows) - len(active),
                "admission_reasons": dict(sorted(Counter(
                    row.decision.primary_reason_code.value for row in active
                ).items())),
                "evidence_tiers": dict(sorted(Counter(
                    row.decision.evidence_tier.value for row in active
                ).items())),
                "reminder_states": dict(sorted(Counter(
                    row.decision.reminder.state.value for row in active
                ).items())),
                "actionable_reminders": sum(
                    row.decision.reminder.should_create_reminder for row in active
                ),
                "do_not_outreach_active_accounts": sum(
                    row.facts.do_not_outreach for row in active
                ),
                "unowned_active_accounts": sum(not row.owner for row in active),
                "people_dont_send_leads": len(dont_send_lead_ids),
            },
            "inputs": {
                "sales_motion_accounts": sorted(sales_motion_accounts),
                "manual_pins": sorted(set(options["manual_pin"])),
                "owner_overrides": dict(sorted(owner_overrides.items())),
            },
            "active_accounts": [_serialize_row(row) for row in active],
        }
        output = Path(options["output"] or _default_output_path(generated_at))
        _write_private_json(output, payload)
        self.stdout.write(json.dumps({
            "status": "preview_written",
            "active_accounts": len(active),
            "people_only_accounts": len(rows) - len(active),
            "admission_reasons": payload["summary"]["admission_reasons"],
            "output": str(output),
        }, sort_keys=True))


def _configured_sales_motion_accounts() -> tuple[str, ...]:
    spreadsheet_id = conf.SALES_MOTION_VERSIONS_GOOGLE_SHEETS_ID.strip()
    if not spreadsheet_id:
        return ()
    from linkedin.notifications import crm_sheets, sheets

    try:
        client = sheets._gspread_authorized_client()
        sales_workbook = crm_sheets.retry_sheet_read(
            lambda: client.open_by_key(spreadsheet_id),
            context="open Sales Motion workbook",
        )
        worksheets = crm_sheets.retry_sheet_read(
            sales_workbook.worksheets,
            context="list Sales Motion tabs",
        )
    except Exception as exc:
        # This command is an explicit preview: silently dropping authoritative
        # pins would produce a deceptively clean but incomplete report.
        raise CommandError(
            f"Could not read the configured Sales Motion workbook: {type(exc).__name__}"
        ) from exc
    return tuple(sorted({
        worksheet.title.strip()
        for worksheet in worksheets
        if worksheet.title.strip()
        and worksheet.title.strip().casefold() not in _IGNORED_SALES_MOTION_TABS
    }, key=str.casefold))


def _configured_people_dont_send_lead_ids() -> set[int]:
    """Read the exact People safety ledger when a CRM workbook is configured."""
    spreadsheet_id = conf.GOOGLE_SHEETS_ID.strip()
    if not spreadsheet_id:
        # Offline/unit-test previews may intentionally have no CRM workbook.
        return set()
    from linkedin.crm_sheet_import import read_people_dont_send_lead_ids
    from linkedin.notifications import sheets

    try:
        spreadsheet = sheets._gspread_client()
        if str(getattr(spreadsheet, "id", "")) != spreadsheet_id:
            raise ValueError("unexpected workbook")
        return read_people_dont_send_lead_ids(spreadsheet)
    except Exception as exc:
        # Provider details and People cell values are intentionally suppressed.
        raise CommandError(
            f"Could not read People Don't send safety state: {type(exc).__name__}"
        ) from exc


def _parse_owner_overrides(values) -> dict[str, str]:
    result = {}
    for raw in values or ():
        account, separator, owner = str(raw or "").partition("=")
        account = account.strip()
        owner = owner.strip()
        if not separator or not account or not owner:
            raise CommandError("--owner-override must use ACCOUNT=OWNER")
        previous = result.get(account)
        if previous is not None and previous.casefold() != owner.casefold():
            raise CommandError(f"Conflicting owner overrides for {account!r}")
        result[account] = owner
    return result


def _serialize_row(row) -> dict:
    return {
        "account_key": row.account_key,
        "account_name": row.account_name,
        "lead_ids": list(row.lead_ids),
        "opportunity_id": row.opportunity_id,
        "owner": row.owner,
        "owner_is_override": row.owner_is_override,
        "key_contacts": list(row.key_contacts),
        "last_meaningful_touch": (
            row.last_meaningful_touch.isoformat()
            if row.last_meaningful_touch else ""
        ),
        "reminder_target_lead_id": row.reminder_target_lead_id,
        "trigger_message_id": row.trigger_message_id,
        "trigger_meeting_id": row.trigger_meeting_id,
        "do_not_outreach": row.facts.do_not_outreach,
        "reminder_do_not_outreach": row.reminder_do_not_outreach,
        "decision": _json_safe(asdict(row.decision)),
    }


def _json_safe(value):
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime,)):
        return value.isoformat()
    if hasattr(value, "isoformat") and not isinstance(value, str):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _active_sort_key(row):
    priority_order = {"urgent": 0, "high": 1, "normal": 2, "low": 3, "none": 4}
    tier_order = {"authoritative": 0, "primary": 1, "secondary": 2, "weak": 3, "none": 4}
    return (
        priority_order[row.decision.priority.value],
        tier_order[row.decision.evidence_tier.value],
        row.account_name.casefold(),
        row.account_key,
    )


def _default_output_path(generated_at: datetime) -> str:
    stamp = generated_at.strftime("%Y%m%dT%H%M%S")
    return f"artifacts/crm-audits/crm-v2-preview-{stamp}.json"


def _write_private_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.chmod(path, 0o600)
