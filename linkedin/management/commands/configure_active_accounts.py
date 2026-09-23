"""One-time conversion of Active Accounts into a manual Attio-style ledger."""
from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from django.core.management.base import BaseCommand, CommandError

from linkedin.exceptions import SheetsError


class Command(BaseCommand):
    help = (
        "Preview or apply the one-time Attio-style Active Accounts conversion. "
        "The daily CRM workflow never invokes this command."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")
        parser.add_argument(
            "--backup-dir",
            default="artifacts/crm-backups",
            help="Gitignored directory for the private full-workbook backup.",
        )

    def handle(self, *args, **options):
        from crm.models import Opportunity
        from linkedin import conf
        from linkedin.notifications import (
            crm_sheets,
            crm_v2_layout,
            crm_v2_sheets,
            manual_active_accounts as manual,
            sheets,
        )

        try:
            spreadsheet = sheets._gspread_client()
            if str(getattr(spreadsheet, "id", "")) != str(conf.GOOGLE_SHEETS_ID):
                raise SheetsError("opened an unexpected CRM workbook")
            worksheet = spreadsheet.worksheet(manual.TAB_NAME)
            source = worksheet.get_all_values()
        except Exception as exc:
            if isinstance(exc, SheetsError):
                raise CommandError(str(exc)) from exc
            raise CommandError("Active Accounts could not be opened") from exc

        if not source:
            raise CommandError("Active Accounts is empty")
        source_headers = tuple(str(value).strip() for value in source[0])
        old_headers = tuple(crm_v2_sheets.ACTIVE_ACCOUNT_HEADERS)
        if source_headers == manual.HEADERS:
            output = [list(row[: len(manual.HEADERS)]) for row in source]
            status = "already_configured"
        elif source_headers == old_headers:
            output = [list(manual.HEADERS)]
            indexes = {header: source_headers.index(header) for header in source_headers}
            stage_labels = dict(Opportunity.Stage.choices)
            for row in source[1:]:
                values = {
                    header: str(row[index] if index < len(row) else "").strip()
                    for header, index in indexes.items()
                }
                if not any(values.values()):
                    continue
                waiting = values.get(crm_v2_sheets.COL_WAITING_UNTIL, "")
                who_owes = values.get(crm_v2_sheets.COL_WHO_OWES, "")
                note_parts = []
                if waiting:
                    note_parts.append(f"Waiting until {waiting}")
                if who_owes:
                    note_parts.append(f"Who owes: {who_owes}")
                raw_stage = values.get(crm_v2_sheets.COL_STAGE, "")
                output.append([
                    values.get(crm_v2_sheets.COL_ACCOUNT, ""),
                    values.get(crm_v2_sheets.COL_OWNER, ""),
                    stage_labels.get(raw_stage, raw_stage),
                    values.get(crm_v2_sheets.COL_KEY_CONTACTS, ""),
                    values.get(crm_v2_sheets.COL_NEXT_ACTION, ""),
                    values.get(crm_v2_sheets.COL_NEXT_ACTION_DUE, ""),
                    "; ".join(note_parts),
                    values.get(crm_v2_sheets.COL_OPPORTUNITY_ID, ""),
                    values.get(crm_v2_sheets.COL_ACCOUNT_ID, ""),
                ])
            status = "planned"
        else:
            raise CommandError(
                "Active Accounts has an unsupported heading schema; no cells were changed"
            )

        report = {
            "schema": "openoutreach.manual-active-accounts.v1",
            "status": status if not options["apply"] else "applied",
            "rows": max(0, len(output) - 1),
            "headings": list(manual.HEADERS),
            "daily_workflow_writes": False,
            "daily_workflow_reads": list(manual.READABLE_DATABASE_FIELDS),
        }
        if not options["apply"] or status == "already_configured":
            self.stdout.write(json.dumps(report, sort_keys=True))
            return

        formulas = crm_sheets._formula_values(worksheet)
        if any(
            isinstance(cell, str) and cell.startswith("=")
            for row in formulas
            for cell in row
        ):
            raise CommandError("Active Accounts contains formulas; no cells were changed")

        crm_sheets.backup_spreadsheet(
            spreadsheet,
            Path(options["backup_dir"]),
            prefix="active-accounts-before-manual-conversion",
        )
        token = uuid4().hex[:12]
        staging_title = f"_Active Accounts staging {token}"
        archive_title = f"_Active Accounts archived {token}"
        try:
            staged = spreadsheet.add_worksheet(
                title=staging_title,
                rows=max(1000, len(output) + 10),
                cols=len(manual.HEADERS),
            )
            end_row = len(output)
            staged.update(
                range_name=f"A1:I{end_row}",
                values=output,
            )
            crm_v2_layout.apply_layout(
                spreadsheet,
                staged,
                headers=manual.HEADERS,
                technical_fields=manual.TECHNICAL_FIELDS,
                owner_values=("Arian", "Athena", "Chuka", "Leili"),
                validation_values={
                    manual.COL_STAGE: tuple(label for _value, label in Opportunity.Stage.choices),
                },
            )
            readback = staged.get_all_values()
            expected = [list(map(str, row)) for row in output]
            actual = [
                [
                    str(row[index] if index < len(row) else "")
                    for index in range(len(manual.HEADERS))
                ]
                for row in readback[: len(output)]
            ]
            if actual != expected:
                raise SheetsError("manual Active Accounts readback did not match")
            spreadsheet.batch_update({"requests": [
                _rename_request(worksheet.id, archive_title),
                _rename_request(staged.id, manual.TAB_NAME, index=0),
            ]})
            spreadsheet.batch_update({"requests": [
                {"deleteSheet": {"sheetId": worksheet.id}},
            ]})
        except Exception as exc:
            if isinstance(exc, SheetsError):
                raise CommandError(str(exc)) from exc
            raise CommandError("manual Active Accounts conversion failed") from exc
        self.stdout.write(json.dumps(report, sort_keys=True))


def _rename_request(sheet_id: int, title: str, *, index: int | None = None):
    properties = {"sheetId": sheet_id, "title": title}
    fields = ["title"]
    if index is not None:
        properties["index"] = index
        fields.append("index")
    return {
        "updateSheetProperties": {
            "properties": properties,
            "fields": ",".join(fields),
        }
    }
