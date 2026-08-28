#!/usr/bin/env python3
"""Safely clone and verify account sales-motion tabs from the live Template."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

try:
    import gspread
    from google.oauth2.service_account import Credentials
except ModuleNotFoundError as exc:  # pragma: no cover - environment guidance
    raise SystemExit(
        "Run this script from OpenOutreach with .venv/bin/python"
    ) from exc


DEFAULT_SPREADSHEET_ID = "15di85z9AWwXPoShg1MNgezjcIMV4OivRihDFpikPLaQ"
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CREDENTIALS = REPO_ROOT / "secrets" / "sheets-service-account.json"
DEFAULT_TEMPLATE = "Template"
SCOPE = "https://www.googleapis.com/auth/spreadsheets"
USED_RANGE = "A1:F153"
ALLOWED_STATUSES = ("Open", "Planned", "Optional", "Complete")
EXPECTED_WIDTHS = [90, 220, 130, 390, 100, 480]
INVALID_TITLE = re.compile(r"[\\/:?*\[\]]")
TASK_ID = re.compile(r"\d+[a-z]+")


def abort(message: str, *, code: int = 2) -> None:
    print(json.dumps({"ok": False, "error": message}, ensure_ascii=False), file=sys.stderr)
    raise SystemExit(code)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Clone an account sales-motion tab from the canonical Template."
    )
    parser.add_argument("account", help="Exact account/tab name to create or verify")
    parser.add_argument("--spreadsheet-id", default=DEFAULT_SPREADSHEET_ID)
    parser.add_argument("--credentials", type=Path, default=DEFAULT_CREDENTIALS)
    parser.add_argument("--template", default=DEFAULT_TEMPLATE)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Check access, template integrity, and name availability without writing",
    )
    mode.add_argument(
        "--verify-only",
        action="store_true",
        help="Verify an existing account tab without writing",
    )
    return parser.parse_args()


def validate_title(raw_title: str) -> str:
    title = raw_title.strip()
    if not title:
        abort("Account name cannot be blank.")
    if len(title) > 100:
        abort("Account name exceeds Google Sheets' 100-character tab-name limit.")
    if INVALID_TITLE.search(title):
        abort("Account name contains a character Google Sheets does not allow in tab names.")
    if title.casefold() == DEFAULT_TEMPLATE.casefold():
        abort("The canonical Template name is reserved.")
    return title


def connect(credentials_path: Path, spreadsheet_id: str) -> gspread.Spreadsheet:
    if not credentials_path.is_file():
        abort(f"Credentials file not found: {credentials_path}")
    credentials = Credentials.from_service_account_file(
        str(credentials_path), scopes=[SCOPE]
    )
    return gspread.authorize(credentials).open_by_key(spreadsheet_id)


def pad(row: list[str]) -> list[str]:
    return row[:6] + [""] * max(0, 6 - len(row))


def read_values(worksheet: gspread.Worksheet) -> list[list[str]]:
    values = [pad(list(row)) for row in worksheet.get(USED_RANGE)]
    return values + [[""] * 6 for _ in range(max(0, 153 - len(values)))]


def classify(values: list[list[str]]) -> dict[str, list[int]]:
    result = {"steps": [], "contexts": [], "guidance": [], "tasks": []}
    for row_number, row in enumerate(values[:153], start=1):
        item, block, kind, _detail, _status, _account_detail = pad(row)
        if item.isdigit() and block.startswith(f"{item} —"):
            result["steps"].append(row_number)
        elif block == "Account status" and kind == "Context":
            result["contexts"].append(row_number)
        elif block == "Operating guidance" and kind == "Guidance":
            result["guidance"].append(row_number)
        elif TASK_ID.fullmatch(item) and kind == "Task":
            result["tasks"].append(row_number)
    return result


def quote_sheet(title: str) -> str:
    return "'" + title.replace("'", "''") + "'"


def sheet_metadata(
    spreadsheet: gspread.Spreadsheet, title: str
) -> dict[str, Any]:
    fields = (
        "sheets(properties,merges,conditionalFormats,"
        "data.rowData.values.dataValidation,data.rowMetadata,data.columnMetadata)"
    )
    metadata = spreadsheet.fetch_sheet_metadata(
        params={
            "includeGridData": "true",
            "ranges": f"{quote_sheet(title)}!{USED_RANGE}",
            "fields": fields,
        }
    )
    sheets = metadata.get("sheets", [])
    if len(sheets) != 1:
        abort(f"Could not retrieve unambiguous metadata for tab {title!r}.")
    return sheets[0]


def row_height(metadata: dict[str, Any], zero_based_row: int) -> int | None:
    rows = metadata.get("data", [{}])[0].get("rowMetadata", [])
    if zero_based_row >= len(rows):
        return None
    return rows[zero_based_row].get("pixelSize")


def column_widths(metadata: dict[str, Any]) -> list[int | None]:
    columns = metadata.get("data", [{}])[0].get("columnMetadata", [])
    return [column.get("pixelSize") for column in columns[:6]]


def validation_values(metadata: dict[str, Any], row_number: int) -> tuple[str, ...]:
    rows = metadata.get("data", [{}])[0].get("rowData", [])
    if row_number - 1 >= len(rows):
        return ()
    cells = rows[row_number - 1].get("values", [])
    if len(cells) <= 4:
        return ()
    validation = cells[4].get("dataValidation", {})
    condition = validation.get("condition", {})
    return tuple(
        value.get("userEnteredValue", "") for value in condition.get("values", [])
    )


def without_sheet_ids(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: without_sheet_ids(item)
            for key, item in value.items()
            if key != "sheetId"
        }
    if isinstance(value, list):
        return [without_sheet_ids(item) for item in value]
    return value


def verify_template(
    worksheet: gspread.Worksheet, metadata: dict[str, Any]
) -> dict[str, Any]:
    values = read_values(worksheet)
    groups = classify(values)
    errors: list[str] = []

    if metadata.get("properties", {}).get("index") != 0:
        errors.append("Template is not the first tab")
    if values[0][0] != "Sales Motion Template":
        errors.append("Template title cell A1 is not canonical")
    expected_counts = {"steps": 15, "contexts": 15, "guidance": 15, "tasks": 85}
    for key, expected in expected_counts.items():
        if len(groups[key]) != expected:
            errors.append(f"Expected {expected} {key}, found {len(groups[key])}")
    if len(metadata.get("merges", [])) != 49:
        errors.append("Expected 49 merged ranges")
    if len(metadata.get("conditionalFormats", [])) != 4:
        errors.append("Expected four conditional-format rules")
    if column_widths(metadata) != EXPECTED_WIDTHS:
        errors.append(f"Column widths differ from {EXPECTED_WIDTHS}")
    if row_height(metadata, 4) != 680:
        errors.append("Row 5 is not 680 pixels high")

    for guidance_row in groups["guidance"]:
        spacer_row = guidance_row + 1
        if any(values[spacer_row - 1]):
            errors.append(f"Expected blank spacer at row {spacer_row}")
        if row_height(metadata, spacer_row - 1) != 78:
            errors.append(f"Spacer row {spacer_row} is not 78 pixels high")

    for task_row in groups["tasks"]:
        if values[task_row - 1][4] != "Open":
            errors.append(f"Template task row {task_row} is not Open")
        if validation_values(metadata, task_row) != ALLOWED_STATUSES:
            errors.append(f"Task row {task_row} has an invalid status dropdown")

    if errors:
        abort("Template contract failed: " + "; ".join(errors[:12]))

    return {
        "steps": len(groups["steps"]),
        "contexts": len(groups["contexts"]),
        "guidance_blocks": len(groups["guidance"]),
        "tasks": len(groups["tasks"]),
        "merges": len(metadata.get("merges", [])),
        "conditional_rules": len(metadata.get("conditionalFormats", [])),
        "spacers": len(groups["guidance"]),
    }


def protected_rows(values: list[list[str]]) -> dict[int, tuple[str, ...]]:
    groups = classify(values)
    protected: dict[int, tuple[str, ...]] = {}
    for row_number in groups["steps"]:
        protected[row_number] = tuple(values[row_number - 1][:2])
    for row_number in groups["guidance"]:
        protected[row_number] = tuple(values[row_number - 1][1:4])
    for row_number in groups["tasks"]:
        protected[row_number] = tuple(
            values[row_number - 1][index] for index in (0, 2, 3)
        )
    return protected


def verify_account(
    account: str,
    template_values: list[list[str]],
    template_metadata: dict[str, Any],
    worksheet: gspread.Worksheet,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    values = read_values(worksheet)
    groups = classify(values)
    errors: list[str] = []

    if values[0][0] != f"{account} Sales Motion":
        errors.append(f"A1 must be {account!r} plus ' Sales Motion'")
    if "[Account]" in "\n".join(cell for row in values for cell in row):
        errors.append("Literal [Account] placeholder remains")
    if protected_rows(values) != protected_rows(template_values):
        errors.append("Protected step, guidance, or task wording differs from Template")
    expected_counts = {"steps": 15, "contexts": 15, "guidance": 15, "tasks": 85}
    for key, expected in expected_counts.items():
        if len(groups[key]) != expected:
            errors.append(f"Expected {expected} {key}, found {len(groups[key])}")

    statuses = [values[row - 1][4] for row in groups["tasks"]]
    invalid_statuses = sorted({status for status in statuses if status not in ALLOWED_STATUSES})
    if invalid_statuses:
        errors.append(f"Invalid task statuses: {invalid_statuses}")

    if without_sheet_ids(metadata.get("merges", [])) != without_sheet_ids(
        template_metadata.get("merges", [])
    ):
        errors.append("Merged ranges differ from Template")
    if without_sheet_ids(metadata.get("conditionalFormats", [])) != without_sheet_ids(
        template_metadata.get("conditionalFormats", [])
    ):
        errors.append("Conditional-format rules differ from Template")
    if column_widths(metadata) != column_widths(template_metadata):
        errors.append("Column widths differ from Template")

    for zero_based_row in range(153):
        if row_height(metadata, zero_based_row) != row_height(
            template_metadata, zero_based_row
        ):
            errors.append(f"Row {zero_based_row + 1} height differs from Template")
            break
    for task_row in groups["tasks"]:
        if validation_values(metadata, task_row) != ALLOWED_STATUSES:
            errors.append(f"Task row {task_row} has an invalid status dropdown")
            break

    if errors:
        abort("Account-tab verification failed: " + "; ".join(errors[:12]))

    return {
        "sheet_id": metadata.get("properties", {}).get("sheetId"),
        "steps": len(groups["steps"]),
        "tasks": len(groups["tasks"]),
        "status_counts": {status: statuses.count(status) for status in ALLOWED_STATUSES},
        "merges": len(metadata.get("merges", [])),
        "conditional_rules": len(metadata.get("conditionalFormats", [])),
    }


def find_case_insensitive(
    worksheets: list[gspread.Worksheet], title: str
) -> gspread.Worksheet | None:
    wanted = title.casefold()
    return next((sheet for sheet in worksheets if sheet.title.casefold() == wanted), None)


def create_account_tab(
    spreadsheet: gspread.Spreadsheet,
    template: gspread.Worksheet,
    account: str,
) -> gspread.Worksheet:
    response = spreadsheet.batch_update(
        {
            "requests": [
                {
                    "duplicateSheet": {
                        "sourceSheetId": template.id,
                        "insertSheetIndex": len(spreadsheet.worksheets()),
                        "newSheetName": account,
                    }
                }
            ]
        }
    )
    try:
        new_sheet_id = response["replies"][0]["duplicateSheet"]["properties"]["sheetId"]
    except (KeyError, IndexError, TypeError) as exc:
        abort(f"Google Sheets did not return the duplicated tab ID: {exc}")
    worksheet = spreadsheet.get_worksheet_by_id(new_sheet_id)
    if worksheet is None:
        abort("The duplicated account tab could not be reopened by ID.")

    values = read_values(worksheet)
    call_block = values[4][0].replace("[Account]", account)
    worksheet.batch_update(
        [
            {"range": "A1", "values": [[f"{account} Sales Motion"]]},
            {
                "range": "A2",
                "values": [[
                    f"Working sales motion for {account}. Update Account status and "
                    "Account-specific detail as facts emerge. Keep operating guidance "
                    "unchanged and assign task statuses only when evidence supports them."
                ]],
            },
            {"range": "A5", "values": [[call_block]]},
        ],
        value_input_option="RAW",
    )
    return worksheet


def main() -> None:
    args = parse_args()
    account = validate_title(args.account)
    spreadsheet = connect(args.credentials, args.spreadsheet_id)
    worksheets = spreadsheet.worksheets()
    template = find_case_insensitive(worksheets, args.template)
    if template is None:
        abort(f"Canonical template tab {args.template!r} was not found.")

    template_metadata = sheet_metadata(spreadsheet, template.title)
    template_summary = verify_template(template, template_metadata)
    template_values = read_values(template)
    existing = find_case_insensitive(worksheets, account)

    if args.verify_only:
        if existing is None:
            abort(f"Account tab {account!r} does not exist.")
        account_metadata = sheet_metadata(spreadsheet, existing.title)
        account_summary = verify_account(
            existing.title,
            template_values,
            template_metadata,
            existing,
            account_metadata,
        )
        print(
            json.dumps(
                {
                    "ok": True,
                    "mode": "verify-only",
                    "account": existing.title,
                    "template": template_summary,
                    "account_tab": account_summary,
                },
                ensure_ascii=False,
            )
        )
        return

    if existing is not None:
        abort(f"Account tab {existing.title!r} already exists; it was not overwritten.")

    if args.dry_run:
        print(
            json.dumps(
                {
                    "ok": True,
                    "mode": "dry-run",
                    "would_create": account,
                    "append_index": len(worksheets),
                    "template": template_summary,
                },
                ensure_ascii=False,
            )
        )
        return

    account_sheet = create_account_tab(spreadsheet, template, account)
    account_metadata = sheet_metadata(spreadsheet, account_sheet.title)
    account_summary = verify_account(
        account,
        template_values,
        template_metadata,
        account_sheet,
        account_metadata,
    )
    print(
        json.dumps(
            {
                "ok": True,
                "mode": "created",
                "account": account,
                "spreadsheet_id": args.spreadsheet_id,
                "account_tab": account_summary,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
